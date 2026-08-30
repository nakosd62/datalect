# Multi-database question answering: how it actually works

This documents the algorithm behind the "in scope" connection picker — what
happens when a session has 2+ databases checked, from the moment a question
comes in to the moment results land in the UI. It describes the code as
implemented (`connection_router.py`, `translate_routes.py`,
`execute_routes.py`, `db.py`, `state_store.py`, `config_routes.py`,
`client.js`), not just the original design.

The single most important fact to keep in mind while reading this: **almost
none of it runs when only one connection is in scope.** Every stage below
starts with a guard that skips straight to today's original, pre-feature
behavior in that case. That guarantee is enforced by tests that fail loudly
if it's ever violated (see the very end of this doc).

## 1. Session state: "in scope" vs. "pinned" vs. "active"

Three related but distinct concepts, easy to conflate:

- **In scope** (`in_scope_preset_ids` / `in_scope_custom_connection_keys`,
  stored server-side in the session) — the full set of connections the user
  has checked in the config modal. This is the candidate pool a question
  could ever be routed to. Changed only by explicitly saving the config
  modal.
- **Pinned** (`PINNED_CONNECTIONS`, client-side only, per conversation) — the
  subset of the in-scope set that THIS conversation has actually settled on
  using, echoed back to the server on every subsequent call as
  `pinned_connections: [{kind, id}, ...]`. Reset whenever the conversation
  resets (new chat, sign-in/out, primary connection switch, or a pinned
  connection falling out of scope).
- **Active/primary** (`connection_id` / `is_custom`, the pre-existing single-
  connection fields) — unchanged by this feature. Reinterpreted as "the
  first entry, in stable order, of the in-scope set" so an existing
  session's current connection becomes the sole initially-checked box for
  free.

The server never trusts a pin blindly — `pinned_connections` is only ever
`{kind, id}` references, and every request re-resolves them fresh via
`resolve_descriptor_by_reference()`, the same trust boundary session-based
resolution already uses everywhere else.

## 2. `/api/translate`: Phase A (routing) and Phase B (generation)

`translate_query()` computes `in_scope_entries` (the resolved in-scope set)
and a single boolean, `multi_db = len(in_scope_entries) > 1 and no explicit
database_url override`. Everything downstream branches on that one flag.

### `multi_db == False` (0 or 1 connection in scope)

Exactly the code path that existed before this feature: one schema fetch,
one dialect intro, one generation call, no `-- database:` comments, no
`connection_selection` field in the response. Nothing described below runs
at all.

### `multi_db == True`

**Step 1 — resolve or route.**
If the client echoed a `pinned_connections` list from a prior turn in this
conversation, and every referenced connection still resolves and is still
in scope, that set is reused as-is and **Phase A does not run for this
turn.** This is the pinning behavior: once a conversation has picked its
database(s), follow-up questions don't re-decide from scratch.

Otherwise, Phase A runs:

1. `build_router_candidate_summaries()` builds one cheap summary per
   in-scope connection — `{name, dialect, table_names}` — fetching each via
   the same TTL-cached schema lookup every other code path uses, reduced to
   just the table/tab names (never column-level detail), fetched in
   parallel across connections.
2. `select_relevant_connections()` sends those summaries plus the user's
   raw question to the LLM with a routing-only system prompt: "which of
   these connections is this question about?" The model returns a JSON
   array of candidate indices, most-relevant first. The prompt explicitly
   tells it most questions are about exactly one connection, and to only
   include more than one when the question genuinely needs data from more
   than one (there's no cross-database join — each connection is queried
   independently).
3. The response is parsed defensively (tolerates a markdown fence, a
   `{"indices": [...]}` wrapper, out-of-range or duplicate indices are
   dropped) and clamped server-side to `MAX_IN_SCOPE_CONNECTIONS` (default
   20 — the same single cap used everywhere "how many databases" comes up,
   see below) regardless of what the model returned.
4. On any failure — unparseable response, or the LLM call itself raising —
   one bounded retry, then a hard fallback to `[0]` (just the first
   in-scope connection). Phase A never fails the request outright.

This reuses the *same* LLM provider/client/model the session already
selected — there's no separate "cheap router model" setting. Only the
prompt is small; latency for a slow top-tier model is a known, accepted
tradeoff.

**Step 2 — generate, schema-aware, per selected connection.**
For each connection Phase A (or the pin) selected, in order: fetch its
full (TTL-cached) schema and its dialect intro, and label it `DB1`, `DB2`,
... in prompt order. The system prompt becomes N dialect intros + N full
schemas instead of one, with an instruction that every SQL statement must
be prefixed with a line like:

```sql
-- database: DB1
SELECT ...;
```

using the correct dialect for whichever connection that statement targets.
When N=1 this degenerates to exactly today's single-schema prompt.

**Step 3 — rewrite ephemeral labels into stable references.**
Immediately after generation, before the SQL is returned, logged, or stored
anywhere, `_rewrite_database_labels()` does one regex substitution pass:
every `-- database: DB<N>` becomes `-- database: preset:<id> (<name>)` or
`-- database: custom:<key> (<name>)`. This is the key robustness move — the
`DB<N>` labels only ever exist inside one generation call's prompt/response
and never persist; a later `/api/execute` call (possibly on hand-edited SQL,
possibly minutes or a history-replay later) only ever needs to parse this
fixed, permanent `preset:`/`custom:` format, never reconstruct a stale
per-call mapping. A `DB<N>` referencing an index the model wasn't given is
left untouched — it simply won't match anything at execute time, same
graceful degradation as no marker at all.

**Step 4 — response.**
The terminal NDJSON line gains `connection_selection: [{kind, id, name},
...]` — a list, even for a length-1 pick, present only when `multi_db` was
true. The client uses this to update its pin and show the disclosure
banner.

## 3. `/api/execute`: split, dispatch, reassemble

`execute_query()` scans the submitted SQL for `-- database: (preset|
custom):(\S+)` markers.

**No markers found** → exactly today's single-connection path, unchanged.
(This also covers every hand-typed query and every script from before this
feature existed.) The one addition: if the caller echoed a
`pinned_connections` reference and didn't pass an explicit `database_url`,
that pinned connection is used as the target instead of the session's
primary — this is what makes hand-edited SQL in a multi-database
conversation still run against the conversation's actual pin rather than
always falling back to the first in-scope connection. A stale/unresolvable
pin silently falls back to the primary connection.

**Markers found** → `_split_by_database_markers()` cuts the script into
chunks at each marker, then `_execute_multi_database()`:

1. Groups chunks by distinct `(kind, ref_id)`, concatenating each group's
   chunks (in original relative order) into one script.
2. Opens each referenced connection exactly once, and hands its whole
   concatenated script to that connection's own, completely unmodified
   `backend.execute()` — fully reusing existing per-connection multi-
   statement handling and partial-results-on-failure behavior, not
   reimplementing any of it.
3. One connection group failing does **not** stop the others — each
   group's success/failure is independent. A failed group's error and
   (if any) partial results are recorded in a `failures` list; a group
   whose reference no longer resolves at all (deleted since the SQL was
   generated) is recorded as its own failure the same way.
4. Results are reassembled ordered by each group's first appearance in the
   script (not perfectly interleaved statement-by-statement across
   groups — a deliberate, documented scope cut), each result tagged with
   `database: {kind, id, name}`.

Response: `success` is `false` only if at least one group failed, but still
carries every successful result from every group, plus `failures` when
non-empty. Both `failures` and per-result `database` are **absent entirely**
(not empty/null) in the marker-free case.

## 4. The client's role

`client.js` doesn't make routing decisions — it just carries state between
turns and renders what the server tells it:

- Renders the config modal's connection list as checkboxes (not radios), so
  2+ can be checked; blocks Save client-side if none are (mirrors the
  server's own validation).
- On a `connection_selection` in a translate response, updates
  `PINNED_CONNECTIONS` and shows a disclosure banner naming which
  database(s) were used — in addition to the `-- database:` comments
  already inline in the SQL, which are the durable, primary disclosure
  mechanism.
- Echoes `PINNED_CONNECTIONS` back as `pinned_connections` on every
  subsequent `/api/translate` / `/api/execute` call in the same
  conversation (a no-op whenever only one connection is in scope).
- Resets the pin — and clears prompt/SQL/results — whenever a currently-
  pinned connection gets unchecked from scope, since the pin no longer
  describes something the conversation is allowed to use.
- The connection badge shows the single connection's name when exactly one
  is in scope (unchanged from before this feature), or "N databases" (with
  a tooltip listing all names) when 2+ are in scope.

## 5. Config validation (`/api/config`)

Saving `in_scope_preset_ids` / `in_scope_custom_connection_keys` is
rejected (400) if the resulting set would be empty ("At least one database
connection must be in scope.") or would exceed `MAX_IN_SCOPE_CONNECTIONS`
(default 20) — the same cap that also bounds how many of those in-scope
connections a single question's Phase A routing may select (see section 2
above); one constant, used everywhere "how many databases" comes up, not
two independently-tuned ones. Unknown or since-deleted references are
silently dropped rather than rejected, matching how a stale single
`connection_id` has always been handled.

## 6. The backward-compatibility guarantee, and how it's enforced

Every stage above is gated behind "2+ connections in scope." The tests
that make this a guarantee rather than just an intention:

- `test_single_in_scope_never_calls_phase_a_and_response_has_no_connection_selection`
  monkeypatches `select_relevant_connections` to **raise** if it's ever
  called for a single-in-scope session, and asserts exactly one LLM call
  happens with no `connection_selection` key anywhere in the response.
- `test_marker_free_script_is_byte_identical_to_today` asserts a marker-
  free script never touches the multi-connection dispatch path: no
  `failures` key, no `database` field on any result.
- The client's badge/tooltip logic only changes behavior when
  `inScopeSummary.count > 1`; with one connection in scope it falls
  straight through to the original `Connected to: <name>` text, covered by
  its own e2e test.

If any of these ever starts failing, that's the signal that the single-
database path has stopped being untouched.

## Relevant environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MAX_IN_SCOPE_CONNECTIONS` | 20 | The one cap on "how many databases" anywhere in this feature: max connections a user may mark in scope at once (`/api/config` POST validation), and max connections a single question's Phase A may select (a server-side clamp regardless of what the model returns). Lives in `app_config.py` so both `config_routes.py` and `connection_router.py` can import it without a circular import. |
| `ROUTER_MAX_TABLE_NAMES_PER_CONNECTION` | 200 | Caps how many table/tab names go into Phase A's prompt per candidate connection. |
