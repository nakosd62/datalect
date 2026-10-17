# Cross-dataset semi-join / filter pushdown — design sketch

## The problem this fixes

A composite (multi-connection) question like *"show me the GDP of each
country that hosted the Olympics, the year of the Olympics"* has a shape the
current pipeline handles badly. Today, for any multi-connection "sql"
outcome:

- **Phase A** (triage, `connection_router.py`) picks the relevant
  connections and, when the question needs rows *related* across them,
  sets `needs_combination: true` — but every picked connection is still
  "queried completely independently, in its own separate call" (the
  prompt's own wording).
- **Phase B** (`sql_generation.py::_run_phase_b_fanout`) generates SQL for
  every picked connection in parallel, each call seeing only its own
  schema and its own rewritten prompt — genuinely blind to the others.
- `/api/execute` runs every connection's SQL in parallel, each capped at
  `EXECUTE_RESULTS_MAX_ROWS` (500).
- Only *after* all of that, if `needs_combination` was set, the existing
  combine step (`combine_routes.py`, see
  `cross-dataset-join-union-design.md`) loads the already-fetched,
  already-capped result sets into DuckDB and relates them in memory.

For the Olympics/GDP question, the Olympics connection's own query
(host country + year, one row per Games) is small and exactly what's
needed. But the economic-indicators connection has no idea it should be
restricted to those specific (country, year) pairs — its own rewritten
prompt, read in isolation, is just "GDP by country and year," which
means every country × every year. That gets capped at 500 arbitrary
rows, and there's no guarantee any of the surviving rows are even the
right pairs. By the time the combine step runs, the damage is done: the
live database already returned the wrong 500 rows, and no amount of
in-memory joining recovers data that was never fetched.

This is a **semi-join** shape: one connection's result (small, a
well-defined set of keys) should become an explicit filter on another
connection's *query itself*, before it ever runs — not a relation
applied to two already-fetched result sets after the fact. The existing
combine step is the right tool when both sides are independently
bounded and the relating only matters for display; it's the wrong tool
when one side would be unboundedly large unless filtered first.

## Core idea: a two-wave plan instead of one parallel fan-out

Keep everything about single-connection SQL generation and execution
exactly as it is. What changes is *sequencing*, for exactly the one pair
of connections (if any) that triage identifies as having this
relationship:

1. **Wave 1**: every "independent" connection, plus the *driving*
   connection (the one whose result will become a filter), execute
   exactly as today — same parallel fan-out, same per-connection
   rewritten prompt.
2. Once the driving connection's real result is in hand, extract its
   actual rows (not a schema-level guess — the real fetched values).
3. **Wave 2**: generate SQL for the *dependent* connection (the one that
   needs filtering) using a new, optional addition to its own Phase B
   prompt input: a block of real rows from the driving connection's
   result, described as a filter the dependent query should apply.
   Then execute it exactly as any other connection's SQL executes
   today.

The dependent connection's live query now runs *already filtered* —
it never asks the real database for the unbounded cross product, so it
never needs the row cap to save it, and there's nothing for a
post-fetch combine step to repair.

## This is the same mechanism the app already has for general-knowledge filters — just with a second source

This isn't a new kind of thing for a single-connection SQL-generation
call to do. `sql_generation_format_rules.txt` (Phase B's own prompt,
used for every connection's independent SQL-writing call) already
documents exactly this move, for a different source of the filter
value:

> "If, in order to write this SQL, you had to resolve part of the
> request using your own general knowledge rather than something
> stated in the user's prompt or present in the schema below — e.g. the
> user asked for 'the current US president' or 'the capital of France'
> and you had to supply the actual name/value yourself before it could
> be used as a literal or filter in the query — add an OPTIONAL
> `knowledge_note` field..."

"GDP for all countries of South America" already works today for
exactly this reason: the economic-indicators connection's own Phase B
call resolves "South America" into the literal list of country names
itself, from its own training, and bakes that straight into the `WHERE`
clause — no second connection, no extra round trip, because the filter
value is something the model can just recite reliably (a stable,
well-known geographic fact).

The Olympics/GDP question needs the *identical* move — "resolve this
restrictive clause into a concrete literal filter list, then write SQL
against it" — except the filter value this time is not stable general
knowledge the model can safely recite. Which countries hosted the
Olympics, and in which exact year, is precisely the kind of
fact-precision problem `knowledge_note`'s own wording already singles
out as needing a caveat ("if the kind of fact you resolved this way
changes over time... say so plainly") — except here it's worse than
"changes over time," it's "a model reciting this from training risks
being subtly wrong or incomplete in a way nobody would notice," and
more importantly: **the real, authoritative answer already lives in
one of this turn's own connections.** The app's own existing bias,
already stated in triage's prompt (outcome 4: prefer a real query over
guessing from training whenever real data is available), argues for
sourcing the filter from that connection's actual rows instead of
letting the dependent connection's model recite it from memory.

So this design is best understood as **adding a second source
`knowledge_note`-style filter resolution can draw from**: not just "my
own general knowledge" (existing, unchanged, still the right call for
stable/well-known facts like "South America's countries" or "the
capital of France"), but "another connection's real, freshly-fetched
data" (new — for when the filtering concept is itself tracked as real
rows somewhere in scope, and reciting it from training would be
guessing at something the app could instead know for certain). This
also sharpens the detection question below: triage doesn't need to ask
"does this question have a restrictive clause that needs resolving
into concrete values" (Phase B already handles that case fine on its
own, for free, whenever general knowledge suffices) — it only needs to
ask the narrower question, "is the restrictive clause's resolution
itself something one of these connections holds as real, queryable
data, rather than something the model should just know?" That's a much
smaller, more specific judgment than my first pass at this design made
it sound.

## Why triage has to be the one to detect this

Triage is the only call in the whole pipeline that ever sees the full,
original, cross-connection question before it gets split apart into
per-connection rewrites. Phase B's per-connection calls are
deliberately, architecturally blind to the existence of other
connections — that isolation is load-bearing elsewhere in the design
and shouldn't change. So, same as `needs_combination` today, the
*decision* that a semi-join shape exists has to be made at triage time,
even though triage only has shallow, schema-only visibility (table and
column names/types — no row counts, no data).

### Triage contract addition (implemented)

Additive and separate from `needs_combination` (mutually exclusive for
the same pair, by prompt convention). A new, optional field on the
"sql" outcome:

```
"semi_join": {
  "driving_index": <int, one of "indices">,
  "dependent_index": <int, one of "indices", != driving_index>,
  "filter_description": "<short phrase naming what's being filtered on,
                          e.g. 'the hosting country and year'>"
}
```

Scoped to **exactly one driving → one dependent pair per turn** for a
first version (see Scope). Deliberately *no* rigid column-name mapping
in this contract — triage's shallow schema means any column-name guess
here is unreliable, and the real matching problem (what do the driving
rows actually look like, how do they map onto the dependent
connection's own real columns) is much easier to solve later, once
real fetched values are available — this is the same realization the
existing combine-step design already leaned on ("by the time the
combine step runs, it's looking at real, already-fetched columns ... a
much smaller matching problem"). `filter_description` is just a plain-
language hint threaded into the dependent connection's prompt later,
not a mechanical contract the orchestration code parses.

Two triage prompt changes (`server/prompts/triage_multi_candidate.txt`):

- **Detection guidance, narrowed per the section above**: the question
  isn't "does this question need a filter resolved from somewhere
  outside the user's own prompt" (true of "South America" too, and
  Phase B's existing `knowledge_note` path already handles that case
  perfectly well on its own, with no extra round trip). The question is
  specifically: does the restrictive clause name a concept that one of
  the *other* in-scope connections tracks as real, queryable rows —
  such that reciting it from general knowledge would mean guessing at
  something the app could instead answer for certain? The prompt gives
  the Olympics/GDP case vs. "GDP of South American countries" as the
  contrasting pair.
- **`database_prompts` rewrite instruction for the driving connection**:
  same idea `needs_combination` already uses ("make sure every affected
  connection's own rewrite still asks for whatever shared column a
  later join/union will need") — make sure the driving connection's
  rewritten prompt explicitly asks for whatever column(s)
  `filter_description` refers to, even if its own slice of the question
  wouldn't otherwise think to select them.

Parsing/validation lives in `connection_router.py`'s `_clean_semi_join`
(mirrors `_clean_database_prompts`' own leniency: a malformed/missing
`semi_join` never fails the whole triage attempt, it's just treated as
"no semi-join this turn").

## Wave 2's actual mechanism: context, not generated SQL (implemented)

`generate_sql_for_connection` (`sql_generation.py`) has a new optional
`filter_context` parameter — a plain description plus a capped sample
of the driving connection's real rows, prepended ahead of "User
Request" in that call's own prompt content. `sql_generation_format_
rules.txt` (the shared system prompt) has a new paragraph telling the
model how to treat a "Filter context" block when one is present:
incorporate it into its own WHERE/JOIN/IN clause using whichever of its
own schema's columns actually correspond; treat an explicitly-empty
filter ("no matching rows") as a real, authoritative answer — write a
query that correctly returns nothing, rather than silently falling back
to the broader, unfiltered question; and disregard the filter
entirely, answering from its own schema as best it can, if nothing in
its own schema plausibly corresponds to the given values. This keeps
"how do I express this as a WHERE/IN/JOIN clause, in this connection's
own dialect, against this connection's own real column names" entirely
inside the existing, already-proven single-connection SQL-generation
call. No new SQL-writing prompt contract, no mechanical join-key
resolution in orchestration code — just one more context block appended
to a call that already knows how to turn instructions plus context into
correct SQL.

## New endpoint: `/api/generate-semi-join-sql` (implemented — `server/semi_join_routes.py`)

Mirrors `/api/combine-results`'s own shape and precedent (a new
endpoint, invoked by the client at the right point in the turn, rather
than folded into an existing route): Phase B's SQL generation and
`/api/execute`'s actual execution have always been two separate phases
in this app, so the dependent connection's own generation can only
happen once the driving connection's `/api/execute` has already
resolved — a separate HTTP round-trip, not something a single streaming
`/api/translate` call could do internally.

Request: `{prompt, kind, id, filter_description, driving_result:
{columns, rows}, history}`. Streams the same NDJSON contract shape as
`/api/translate`'s single-connection path and `/api/combine-results`:
zero or more `{"status": "retrying", ...}` lines, then one terminal
line — `{"status": "done", "success": true, "outcome": "sql"|"note",
"sql"|"text": ..., "knowledge_note", "filter_applied", "filter_row_count",
"filter_capped"}` on success, or `{"status": "done", "success": false,
"error": ...}` on an LLM-call failure. Never executes anything itself —
the client submits the returned (marker-prepended) SQL to the existing
`/api/execute` exactly like any other connection's SQL, since that
route already dispatches by its own `-- database: ...` marker comments.

`_build_filter_context` (the row-rendering helper) handles three cases,
not two: a well-formed, under-cap set of distinct driving rows (renders
them as the filter), zero driving rows (an explicit "nothing matched"
instruction — the dependent connection must correctly return nothing
too, not quietly answer the broader question), and an over-cap set
(returns no filter text at all — the caller falls back to generating
this connection's SQL exactly as if semi_join had never been detected,
surfacing `filter_applied: false` so the end user sees an honest
caveat rather than a silently-dropped or truncated filter).

## Orchestration wiring (implemented — `translate_routes.py`)

`stream_translation()`'s `router_only_group_mode` "sql" branch: when
triage's result carries `semi_join`, the dependent connection is
excluded from that turn's `_run_phase_b_fanout` call entirely (a
separate `phase_b_entries`/`phase_b_prompts`/`phase_b_histories` list,
built by filtering out the dependent connection's position — `indices`
itself is left untouched, since other things downstream still expect it
to reflect triage's whole pick). The dependent connection still gets a
`connection_selection` entry (so the client can render a placeholder
tab for it), flagged `"pending_semi_join": true`. A new `semi_join_
pending` field (alongside the existing `needs_combination`) carries
everything the client needs to call the new endpoint once it's ready:
the driving/dependent connections' `{kind, id, name}`, the dependent's
own rewritten prompt, `filter_description`, and `max_filter_rows`
(`SEMI_JOIN_MAX_FILTER_ROWS`). `None` when no semi-join was detected
this turn — purely additive, zero behavior change for the overwhelming
majority of multi-connection questions.

## Extracting the filter from the driving result

The cap lives in its own constant, `SEMI_JOIN_MAX_FILTER_ROWS`
(`app_config.py`, env-overridable, default 200) — deliberately **not**
a reuse of `EXECUTE_RESULTS_MAX_ROWS` or `COMBINE_RESULTS_MAX_ROWS`:
both of those bound a *result set's own size*, a materially different
concern from "how many distinct filter values is it still reasonable
to inline into one WHERE/IN clause in a prompt" — a filter list that's
actually useful in practice is typically much smaller (tens of values,
not hundreds), and tuning one shouldn't silently change the other.
Exceeding the cap doesn't fail the turn — see `_build_filter_context`
above.

## Failure / degradation modes (same posture as the existing combine step: never block the turn)

- **Driving connection fails, errors, or returns zero rows**: a failed/
  errored driving connection means the client simply never calls the
  new endpoint for the dependent connection at all (nothing real to
  filter on) — the dependent connection surfaces as an ordinary
  unexecuted/pending entry, same as any connection whose Phase B call
  never happened. A driving connection that returns real rows, just
  zero of them, is different and handled explicitly (see
  `_build_filter_context` above) — zero rows is itself an answer, not
  an absence of one.
- **Driving result too large** (cap exceeded): falls back to generating
  the dependent connection's SQL unfiltered, surfaced via
  `filter_applied: false`.
- **No plausible match on the dependent side**: the dependent
  connection's own Phase B call is explicitly told (via
  `sql_generation_format_rules.txt`) it may disregard the filter
  context if nothing in its own schema plausibly corresponds — mirrors
  the combine step's own "don't guess at a join key that doesn't really
  correspond... report this honestly" posture, just handled inline by
  the existing single-connection call rather than a new JSON failure
  shape.
- **No `semi_join` field at all**: zero behavior change — additive and
  opt-in per turn, same as `needs_combination` was.

## Why this is strictly better than teaching the existing combine step to "retry smarter"

An alternative would be: let the dependent connection's query run
unfiltered as today, detect in the combine step that it got truncated,
and re-run it with a derived filter. This is strictly worse than doing
it up front: by the time combine runs, the live database has *already*
paid for and returned the wrong 500 rows — there's no recovering rows
that were never fetched by joining in memory afterward. Any "retry
smarter" design still ends up re-running the same SQL-generation +
execute cycle a second time for that connection — which is exactly what
this design does, just proactively, from triage's upfront plan, instead
of reactively, after a wasted first attempt that already consumed a
live-database round trip and already produced a wrong or misleading
result if nobody noticed the truncation.

## Scope of this first version

- Exactly one driving → one dependent pair per turn, not a general
  dependency DAG. A three-connection semi-join chain is a genuinely
  unusual question; extend later only if real usage shows it's common.
- `semi_join` and `needs_combination` mutually exclusive by prompt
  convention, not re-validated mechanically — a response that somehow
  set both is passed through as-is; the orchestration code doesn't
  currently special-case that combination.
- No mechanical join-key contract between triage and orchestration —
  `filter_context` carries real rows plus a plain-language description,
  and the dependent connection's existing Phase B call does the actual
  matching, the same way the existing combine step already defers that
  same problem to an LLM looking at real values rather than resolving
  it from schema alone up front.

## Server-side implementation status

Implemented: `app_config.SEMI_JOIN_MAX_FILTER_ROWS`; triage prompt +
`connection_router.py`'s `_clean_semi_join`/parsing; `generate_sql_for_
connection`'s `filter_context` parameter + its system-prompt paragraph;
the new `/api/generate-semi-join-sql` endpoint (`semi_join_routes.py`,
registered in `server.py`); `translate_routes.py`'s Phase B exclusion +
`semi_join_pending` wiring. Covered by new tests in `tests/server/
test_connection_router.py` (parsing + end-to-end Phase B exclusion) and
`tests/server/test_semi_join_routes.py` (`_build_filter_context` +
HTTP-level route behavior, including the cap-exceeded and zero-row
cases).

**Now implemented: `webClient/client.js`.** `startAllModeStreaming()`
renders a distinct "Waiting for filter…" placeholder for a
`pending_semi_join` connection instead of a "Fetching…" one. A new
`runAllModeSemiJoinIfPending(state)` function runs inside
`translatePrompt()`'s `router_route` branch, after every auto-executed
connection's own `/api/execute` has resolved
(`await Promise.all(state.pendingExecutions)`) but before
`maybeFinalize()` — necessarily so, since the dependent connection was
excluded from Phase B's fan-out and so never fires a
`phase_b_connection_done` event, meaning nothing else would ever
advance `state.settledCount`/`state.generationSettledCount` to let
`maybeFinalize()`'s own guard pass. It looks up the driving
connection's real `{columns, rows}` from `state.executeResults`, calls
`/api/generate-semi-join-sql`, slots the returned SQL into the SQL
editor/turn history in original connection order, executes it via the
normal `/api/execute` call, and settles the dependent connection's tab
exactly like any other connection's result (or an honest note/error
tab on failure). A `filter_applied`/`filter_capped` caveat and any
`knowledge_note` are threaded into the existing "Thoughts & Notes"
drawer (`notes.semiJoinNote`, alongside the existing `combineNote`/
knowledge-note fields), including through history persistence/
restoration. GA fan-out tracking (`translate_submitted`/`sql_executed`)
fires for the dependent connection only once its real call actually
happens, rather than double-counting the one `startAllModeStreaming()`
already fires for every *other* connection at `phase_a_route` time.

Scoped explicitly to **auto-execute on** for this first pass: when
auto-execute is off, the driving connection's own result isn't
available either (it just sits in its own "Ready to execute"
placeholder until a manual Execute click), so there's no real data yet
to filter by — `runAllModeSemiJoinIfPending()` detects this and settles
the dependent connection as an honest "couldn't filter, turn on
auto-execute" note rather than guessing or blocking. The same honest
fallback covers the driving connection's own execute failing outright.
A full manual-execute integration (re-triggering this same sequence
once the user manually executes the driving connection) is left as a
future follow-up.

Covered by two new e2e specs in `tests/e2e/multi-database.spec.js`: the
success path (Phase B exclusion confirmed via tab count, the real
driving rows reaching `/api/generate-semi-join-sql`, exactly one
`/api/execute` call per connection in the right order, and real
results rendering in the dependent tab) and the driving-failure path
(the dependent connection settles as a note, and
`/api/generate-semi-join-sql` is never even called).
