#!/usr/bin/env python3
"""
stress_test.py - a small, dependency-light load generator for exercising
Datalect's DEPLOYED Cloud Run instance's concurrency guard
(concurrency_guard.py: MAX_CONCURRENT_TRANSLATE_REQUESTS/
MAX_CONCURRENT_EXECUTE_REQUESTS) and per-user rate limiter
(rate_limiter.py: RATE_LIMIT_TRANSLATE/RATE_LIMIT_EXECUTE), driven from
your laptop against the real service URL.

IMPORTANT - this hits the REAL deployed instance:
  * every /api/translate or /api/summarize-result call is a real LLM call
    (real provider cost/quota)
  * every /api/execute call opens a real connection to your Cloud SQL
    instance
Start with small numbers (the defaults below are deliberately modest) and
watch the output before scaling up. Pass --model to point at your
cheapest/fastest configured model to keep the LLM-calling modes cheap.

Requires: pip install requests

WHY A CUSTOM SCRIPT INSTEAD OF hey/k6/vegeta
Those tools are great for raw throughput, but this app's rate limiter and
concurrency guard are keyed off this app's own notion of "user" - a
crbot_session_id cookie, persisted across requests via a normal
requests.Session(). Generic load-testing tools generally don't carry a
cookie jar across repeated requests as "the same simulated user" the way
this script does, so they can't cleanly exercise the per-user rate limit
(each request would look like a brand-new anonymous visitor) or reliably
land a burst on one Cloud Run instance (--session-affinity is
cookie-based). This script exists specifically to control that variable.

MODES

  whoami     One request to /api/auth/me - confirms the URL/service is
             reachable and shows what identity you'll be resolved as.

  burst      Fires --n requests to one endpoint SIMULTANEOUSLY, all from
             ONE simulated user (one shared session cookie, so
             --session-affinity routes them to the same Cloud Run
             instance). Exercises the CONCURRENCY GUARD: expect the first
             MAX_CONCURRENT_*_REQUESTS to succeed and the rest to come
             back 503 with a Retry-After header, all within roughly one
             round trip's time (not spread out).

  sustained  Fires one request every --interval seconds, from ONE
             simulated user, for --duration seconds. Exercises the RATE
             LIMITER: expect 200s until that user's per-minute budget is
             spent, then 429s with Retry-After, then 200s resuming once
             the rolling window clears.

  fairness   Runs --users independent simulated users concurrently (each
             its own session cookie/identity), each doing its own
             --calls-per-user sustained run. Confirms one user tripping
             their own rate limit never affects another user's budget -
             the whole point of keying the limiter off user identity
             rather than IP.

  soak       Runs --users independent simulated users concurrently,
             SUSTAINED over wall-clock --duration, each user pinned to
             its OWN real dataset (a real admin preset, via POST
             /api/config {"preset_id": ...} - see prime_and_pin_user())
             so N users genuinely spread load across N different real
             connections/schemas rather than all hammering one. Every
             cycle each user: (1) runs a REAL, substantial /api/execute
             query against its own pinned dataset (from --datasets-file,
             not the trivial CHEAP_SQL the other modes use), (2) folds
             that call's REAL result rows into the user's own growing
             `history` array, and (3) sends a /api/translate call
             carrying that growing, real-result-based history. Optional
             --soak-summarize also fires /api/summarize-result each
             cycle using that same real result. This is the mode built
             for "5 concurrent users, each with substantial turn history,
             against many different real datasets, sustained, to watch
             memory grow" - see the SOAK MODE section below for the
             --datasets-file format and why translate/execute are
             deliberately decoupled (not chained SQL-from-translate ->
             execute) in this mode.

  conversation  Runs --users independent simulated users concurrently,
             SUSTAINED over wall-clock --duration, each running a REAL
             CLOSED-LOOP conversation: pick a dataset from --datasets-file,
             ask an opening question, let /api/translate actually generate
             the SQL, run THAT real SQL via /api/execute, summarize the
             real result via /api/summarize-result, ask a followup
             question carrying the growing real history, and so on for
             --turns-per-conversation turns - then start a brand-new
             conversation (fresh history) against the NEXT dataset in the
             pool, cycling through every dataset you list over the course
             of the run. This is the "user connects, picks a dataset,
             asks a question, gets real SQL/results/summary, asks a
             follow-up, ..." scenario end to end - see CONVERSATION MODE
             below for the prompt pools, the dataset-rotation scheme, and
             an important warning about running real LLM-generated SQL
             against your real data.

HISTORY PAYLOAD (--history-turns, translate endpoint only)

  Every mode above defaults to an EMPTY `history` on every /api/translate
  call - the best case for per-request memory, and not what a real,
  already-chatting user's request looks like. server/translate_routes.py
  reads `history = data.get('history', [])[-(HISTORY_MAX_TURNS * 2):]`
  straight from the request body on every single call (conversation
  history is client-held, not server-side session state - see that
  module's own comment) - so a client mid-conversation resends its whole
  history array every time, and the server has to receive, JSON-parse,
  and hold all of it in memory for that request's lifetime before ever
  slicing it down.

  --history-turns N attaches a synthetic history array of that shape (see
  build_synthetic_history()) to every translate call this run makes,
  growing turn-by-turn up to N in sustained/fairness (mimicking a real
  conversation lengthening over time) or fixed at N for every concurrent
  request in burst (mimicking N users who are all already mid-conversation
  at once). --history-rows/--history-cols/--history-cell-bytes control
  each turn's synthetic result-set size - default to a realistic shape,
  raise them to probe a heavier or malformed-client payload. Has no effect
  on --endpoint execute/summarize - neither route reads `history` at all.

SOAK MODE (--datasets-file, memory-growth testing under sustained multi-
user, multi-dataset load)

  --datasets-file points at a JSON file: a non-empty list of objects,
  each {"preset_id": "<a real admin-configured preset id>", "sql": "<a
  real, substantial SELECT against that preset's data>", "name":
  "<optional label, used only in this script's own output>"}. See
  soak_datasets.example.json next to this script for the exact shape -
  copy it and fill in real preset ids (from your DATABASE_PRESETS_FILE)
  and real, substantial queries against each (LIMIT to a few thousand
  rows rather than an unbounded SELECT * on a huge table, unless
  deliberately probing an even heavier per-call payload).

  --users simulated users are assigned datasets round-robin
  (user u gets datasets[u % len(datasets)]) - use --users equal to your
  --datasets-file's length to give every user a genuinely distinct
  dataset (the scenario this mode was built for), or fewer to have
  multiple users share one.

  Each user's session is pinned to its dataset ONCE at the start (POST
  /api/config {"preset_id": ..., "is_custom": false} - the same
  server-side session-active-connection mechanism the real webClient
  uses when someone picks a preset from the DB dropdown), so every
  subsequent execute/translate call for that user can omit database_url
  entirely and still resolve against the right real connection/schema,
  same as a real user who picked a dataset once and kept chatting.

  WHY EXECUTE AND TRANSLATE ARE DECOUPLED HERE (deliberate design
  choice, not an oversight): a fully "closed-loop" simulation would have
  translate PROPOSE the SQL that execute then runs. Doing that for real
  would mean parsing translate's streamed NDJSON response to extract a
  SQL statement and hoping the LLM proposes something valid against an
  arbitrary real schema on every cycle of a long sustained run against a
  REAL, BILLED service - one bad/unparseable response would stall or
  corrupt that user's whole run. Instead, --datasets-file's own "sql" is
  always what actually runs against that dataset (deterministic,
  reviewable, cheap to reason about), while translate is still exercised
  every cycle with a REAL, growing, execute-result-shaped history
  attached - so you still get "substantial real results feeding a
  growing real history on every translate call," just without betting a
  long paid run on the LLM always proposing runnable SQL.

  --history-turns here means something slightly different than in the
  other modes: 0 (the default) means UNCAPPED growth - since watching
  memory grow as history keeps lengthening over the whole --duration is
  the actual point of this mode - while a positive N caps it at N turns
  (oldest dropped first), letting you instead test "steady-state memory
  at a fixed history size, sustained" if that's what you want to probe.
  --history-rows caps how many rows of each cycle's REAL execute result
  get carried forward into history (independent of how many rows the
  real query itself returns/executes) - mirrors the server's own
  HISTORY_RESULT_MAX_ROWS trimming, and keeps the CLIENT-SIDE history
  payload from growing unboundedly in lockstep with an uncapped
  --history-turns run against a query that returns many rows.

  --soak-summarize additionally fires /api/summarize-result each cycle
  using that same cycle's real execute result, to also exercise
  summarize's memory pressure (it shares the same
  MAX_CONCURRENT_TRANSLATE_REQUESTS pool as translate - see
  concurrency_guard.py).

  --dry-run (soak mode only) prints the resolved plan (users, their
  assigned dataset/preset, duration, interval, roughly how many cycles
  and total real calls this run will make) and exits WITHOUT making any
  network calls - use it to sanity-check a run's real cost/shape before
  firing it at a real, billed deployment.

CONVERSATION MODE (--mode conversation - real closed-loop translate ->
execute -> summarize -> translate chains, across ALL your datasets)

  --datasets-file here only needs {"preset_id": "...", "name": "<optional
  label>"} per entry - no "sql" required (conversation mode ignores it if
  present, so the SAME file soak mode uses also works here, or write a
  separate one). See conversation_datasets.json next to this script for a
  ready-to-use pool built from this deployment's own real, currently
  ACTIVE presets (i.e. every entry in presets.json whose id/type isn't
  prefixed "_paused_").

  Each simulated user runs a SEQUENCE of conversations back to back for
  the whole --duration, not one fixed dataset the whole time: conversation
  number c (0, 1, 2, ...) for user u picks datasets[(u + c) % len(datasets)]
  - staggered so users don't all start on the same dataset, and cycling
  every user through the FULL pool as their run goes on (with enough
  --duration/--turns-per-conversation, "each user does this against all
  available preset datasets" - your own framing for this mode). Each new
  conversation pins that dataset (POST /api/config) and starts with a
  completely FRESH `history` - a real user picking a different dataset
  starts a new train of thought, not one that drags in a previous,
  unrelated schema's turns.

  Per turn: prompts rotate through a pool - by default QUICK_PROMPTS
  (QUICK_PROMPTS[turn % len(QUICK_PROMPTS)]), the app's own real,
  already-tested "quick prompt" chips from webClient/index.html's
  #examplePrompts, not made-up questions, so results reflect prompts this
  app is actually designed to handle well. Two of the four are, by the
  app's own system prompt rules, sometimes legitimately answered with a
  "*** NO SQL ***"-prefixed prose reply instead of real SQL (a schema/
  "what's in here" question, or "what should I ask" - see
  translate_routes.py's _COMMON_FORMAT_RULES) - _extract_sql() recognizes
  and skips these exactly like any other "no real SQL this turn" case
  (see _NO_SQL_PREFIX_RE), rather than trying to execute the prose as SQL.
  Pass --prompts-file to use your own pool instead (one prompt per line,
  '#' comments allowed - see conversation_prompts.example.txt, seeded
  with QUICK_PROMPTS, for a ready-to-edit starting point for your own,
  e.g. deliberately worst-case, prompts). --prompt-order controls how the
  pool is walked: "roundrobin" (default, in order, same as QUICK_PROMPTS'
  own behavior) or "random" (a fresh pick each turn).

  Each turn: /api/translate runs for real with that prompt + the
  conversation's growing history; if it returns SQL (the terminal NDJSON
  line's "sql" field - see _extract_sql()), THAT SQL is what actually runs
  via /api/execute (not a canned query); if that execute succeeds,
  /api/summarize-result summarizes the real result; the real result then
  gets folded into history for the next turn. UNLIKE soak mode's
  --history-rows-capped _append_real_turn_to_history(), conversation mode
  never trims rows itself - it forwards the REAL, uncapped execute result
  (see _extract_capped_results(), called with max_rows=0 here) to both
  the summarize request and the next turn's history, exactly like the
  real client (webClient/client.js's summarizeResultForHistory() doesn't
  trim rows either - the server already capped what execute returned via
  EXECUTE_RESULTS_MAX_ROWS, and translate_routes.py does its own
  HISTORY_RESULT_MAX_ROWS trim when it builds the LLM prompt). This
  matters for memory-pressure testing specifically: capping here made
  every conversation-mode request payload smaller than a real user's ever
  would be.

  A failed translate (guard rejection, LLM error, no parseable SQL) skips
  execute/summarize for that turn and leaves history unchanged - nothing
  real happened yet to record. A failed EXECUTE, though, still gets
  folded into history, matching the real client's own rule ("a concluded
  turn's results or errors must be added to history" - see
  webClient/client.js): a multi-statement script (semicolon-separated
  SQL) that fails partway through still summarizes and records whatever
  statements succeeded before the failure, PLUS the failure itself, as
  one turn with multiple result-set entries (see
  _statement_results_for_history()) - exactly the "multiple queries, each
  its own tab, all fed back into history" case a real multi-statement
  query produces. A bare execute failure with nothing partial to show
  (an EXECUTE_GUARD 503, a bad first/only statement) still summarizes and
  records a single {"error": ...} entry the same way. Only a genuine
  request-level failure (client timeout, dropped connection - no HTTP
  response at all) has nothing to build a turn from and is skipped, same
  as the real client's own fetch()-catch path. Either way, one bad turn
  never aborts that user's whole run.

  IMPORTANT - this is real, LLM-proposed SQL running against your real
  data, not a reviewed, fixed query: the model could occasionally propose
  something expensive (e.g. a full-table scan with no LIMIT) on a dataset
  it hasn't seen before. This deployment's own TRANSLATION_TIMEOUT_SECONDS
  and SQL_EXECUTE_TIMEOUT_SECONDS (env.yaml, currently 60s each) already
  bound how long any single call can run server-side, but per-call cost
  (LLM tokens, DB compute) is otherwise whatever the model actually
  proposes each turn - start with --users/--duration modest here even
  more than with soak mode, and watch the output before scaling up.

  --turns-per-conversation (default 4) caps how many prompt/translate/
  execute/summarize cycles happen before that user starts a fresh
  conversation on the next dataset - lower it to rotate through your
  dataset pool faster, raise it for longer, more realistic single
  conversations. This is independent of the outgoing `history` array's own
  turn count, though: at run start, this mode does one GET /api/config -
  the same call the real client's own fetchBackendConfig() makes on page
  load - and uses its `history_max_turns` field to cap `history` the same
  way client.js's own chatStore.pushTurn() does (`history.slice(
  -maxEntries)` after every push - see fetch_history_max_turns()'s own
  docstring). That client-side cap, not just the server's own defensive
  HISTORY_MAX_TURNS slice in translate_routes.py, is what limits how many
  turns ever show up in the real app's UI too - a real browser's
  `history` payload never actually holds more turns than that in the
  first place. So --turns-per-conversation CAN be set higher than the
  server's real cap - older turns just roll off the front as the
  conversation goes on, exactly like a real long conversation would,
  rather than growing an ever-larger, unrealistic payload. If the GET
  /api/config probe fails, this run falls back to uncapped growth (a
  printed warning says so) - the server's own defensive slice still
  protects the actual LLM prompt either way, so this can't cause a bad
  call, only a less realistic outgoing payload shape.

EXAMPLES

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode whoami

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode burst \
      --endpoint translate --n 8 --model gemini-3.5-flash-lite

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode sustained \
      --endpoint translate --interval 2 --duration 90 --model gemini-3.5-flash-lite

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode fairness \
      --endpoint translate --users 3 --calls-per-user 15 --interval 2 \
      --model gemini-3.5-flash-lite

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode soak \
      --datasets-file soak_datasets.example.json --users 5 --duration 1800 \
      --interval 3 --model gemini-3.5-flash-lite --soak-summarize --dry-run

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode conversation \
      --datasets-file conversation_datasets.json --users 3 --duration 900 \
      --interval 5 --turns-per-conversation 4 --model gemini-3.5-flash-lite --dry-run

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode conversation \
      --datasets-file conversation_datasets.json --prompts-file conversation_prompts.example.txt \
      --prompt-order random --users 20 --duration 900 --interval 5 \
      --turns-per-conversation 4 --model gemini-3.5-flash-lite
"""

import argparse
import json
import random
import re
import statistics
import threading
import time

import requests

# Deliberately trivial/cheap: a short prompt against a one-row query, so
# every mode's per-call cost (LLM tokens in, DB work) stays minimal
# regardless of how many calls a run ends up making.
CHEAP_PROMPT = "how many rows are in the smallest table"
CHEAP_SQL = "SELECT 1"


def build_synthetic_history(turns, rows_per_turn, cols_per_row, cell_bytes):
    """Builds a `history` array in the exact shape webClient/client.js
    sends to /api/translate (see createChatHistoryStore()/pushTurn() and
    summarizeResultForHistory() there): alternating
    {"role": "user", "text": ...} and {"role": "model", "text": <sql>,
    "results": [{"columns": [...], "rowCount": N, "rows": [...]}]}
    entries, one pair per turn - so the server's history-handling code
    (translate_routes.py's `history = data.get('history', [])[...]`, plus
    however many rows of it get rendered into the LLM prompt - see
    HISTORY_RESULT_MAX_ROWS) is exercised with a payload shaped like a
    real, already-chatting user's request rather than every stress-test
    call starting from empty history (every mode's default today).

    Deterministic, not random: every cell is the same `cell_bytes`-long
    filler string, since payload SIZE is what a memory stress test cares
    about, not realistic-looking content - this also makes repeated runs
    directly comparable to each other."""
    filler = "x" * max(cell_bytes, 1)
    columns = [f"col_{c}" for c in range(cols_per_row)]
    row = [filler for _ in range(cols_per_row)]
    rows = [list(row) for _ in range(rows_per_turn)]
    history = []
    for t in range(turns):
        history.append({"role": "user", "text": f"synthetic stress-test question #{t}"})
        history.append({
            "role": "model",
            "text": f"SELECT * FROM synthetic_table_{t}",
            "results": [{"columns": columns, "rowCount": rows_per_turn, "rows": rows}],
        })
    return history


def build_payload(endpoint, model, database_url, history=None, sql=None, exec_results=None, prompt=None):
    if endpoint == "translate":
        # prompt lets conversation mode rotate through the app's own real
        # quick prompts each turn (see QUICK_PROMPTS) instead of every
        # other mode's fixed CHEAP_PROMPT.
        payload = {"prompt": prompt or CHEAP_PROMPT}
        # history is client-held conversation state (see
        # build_synthetic_history()'s docstring) - only ever meaningful
        # for /api/translate, which is the only route that reads it.
        if history:
            payload["history"] = history
    elif endpoint == "execute":
        # sql lets soak mode run a real, substantial, dataset-specific
        # query (from --datasets-file), or conversation mode run the
        # REAL SQL translate just generated, instead of every other
        # mode's trivial CHEAP_SQL - see cmd_soak()/cmd_conversation().
        payload = {"sql": sql or CHEAP_SQL}
    elif endpoint == "summarize":
        # exec_results lets soak/conversation mode summarize a REAL
        # execute result (already shaped like /api/execute's own
        # `results` array - see _extract_capped_results()) instead of
        # the trivial 1-row payload every other mode uses; prompt mirrors
        # the same real question conversation mode's translate call used.
        payload = {
            "prompt": prompt or CHEAP_PROMPT,
            "sql": (sql or CHEAP_SQL) + ";",
            "results": exec_results if exec_results is not None else [{"columns": ["1"], "rows": [[1]], "rowCount": 1}],
        }
    else:
        raise ValueError(endpoint)
    if model:
        payload["model"] = model
    if database_url:
        payload["database_url"] = database_url
    return payload


ENDPOINT_PATHS = {
    "translate": "/api/translate",
    "execute": "/api/execute",
    "summarize": "/api/summarize-result",
}
# translate/summarize stream NDJSON; execute returns one plain JSON body.
STREAMED_ENDPOINTS = {"translate", "summarize"}


def fire_one(session, base_url, endpoint, model, database_url, timeout, history=None, sql=None, exec_results=None, prompt=None):
    path = ENDPOINT_PATHS[endpoint]
    payload = build_payload(endpoint, model, database_url, history=history, sql=sql, exec_results=exec_results, prompt=prompt)
    is_streamed = endpoint in STREAMED_ENDPOINTS
    started = time.monotonic()
    try:
        resp = session.post(
            base_url.rstrip("/") + path,
            json=payload,
            timeout=timeout,
            stream=is_streamed,
        )
        body = None
        if is_streamed:
            # translate/summarize stream NDJSON progress lines ending in
            # one terminal {"status": "done", "success": ..., ...} line
            # (see translate_routes.py's stream_translation()/
            # stream_summarize_result()) - or, for an early-validation/
            # guard-rejection response, a single plain JSON object with
            # no "status" key at all (same shape client.js's own
            # readNdjsonStream already handles uniformly - see that
            # route's own comments). Keep the LAST well-formed line as
            # `body`: conversation mode reads its "sql"/"summary" back out
            # (see _extract_sql()); other modes never read this key.
            last_line = None
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    last_line = json.loads(line)
                except ValueError:
                    continue
            body = last_line
        else:
            # execute returns one plain JSON body (see execute_routes.py:
            # {'success', 'results', 'rowCount', 'executionTimeMs'}) -
            # capture it so soak/conversation mode can fold REAL results
            # into their growing history (see _extract_capped_results()).
            # Unused by burst/sustained/fairness.
            try:
                body = resp.json()
            except ValueError:
                body = None
        return {
            "status": resp.status_code,
            "elapsed": time.monotonic() - started,
            "retry_after": resp.headers.get("Retry-After"),
            "body": body,
        }
    except requests.exceptions.RequestException as exc:
        return {"status": None, "elapsed": time.monotonic() - started, "error": str(exc)}


def print_result(label, r):
    bits = [f"status={r['status']}", f"{r['elapsed']:.2f}s"]
    if r.get("retry_after"):
        bits.append(f"Retry-After={r['retry_after']}")
    if r.get("error"):
        # A request-level failure (connection dropped, client timeout) -
        # never overlaps with body_error below (no HTTP response body
        # exists at all on this path).
        bits.append(f"error={r['error']}")
    else:
        body = r.get("body")
        if isinstance(body, dict) and body.get("error"):
            # The real, specific reason a non-2xx (or "success": false)
            # response failed - e.g. execute_routes.py deliberately
            # returns the RAW DATABASE error message (bad column, syntax
            # error, dialect-incompatible construct, ...) as HTTP 400 -
            # "this endpoint runs SQL the user themselves supplied, so
            # the backend's error IS the feedback they need" (see that
            # route's own comment). Equally, translate/summarize's
            # {"success": false, "error": ...} shape surfaces the real
            # LLM/provider error the same way. Without this, a 400/503
            # here shows only an opaque status code with no way to tell
            # "bad generated SQL" apart from "guard busy" apart from
            # "provider down."
            bits.append(f"body_error={body['error']!r}")
    print(f"  {label}: " + " ".join(bits))


def summarize_results(results):
    codes = {}
    for r in results:
        codes[r["status"]] = codes.get(r["status"], 0) + 1
    print("Result counts (status -> count):", codes)
    ok_times = [r["elapsed"] for r in results if r["status"] == 200]
    if ok_times:
        print(
            f"200 latency: min={min(ok_times):.2f}s "
            f"mean={statistics.mean(ok_times):.2f}s max={max(ok_times):.2f}s"
        )
    retry_afters = sorted({r["retry_after"] for r in results if r.get("retry_after")})
    if retry_afters:
        print("Retry-After values seen:", retry_afters)


def _history_for(args, turns):
    """None when --history-turns is unset/0, or the endpoint isn't
    translate (execute/summarize never read `history` - see
    build_payload()'s own comment) - both cases keep today's original
    empty-history behavior unchanged. Otherwise builds `turns` synthetic
    turn-pairs via build_synthetic_history() using args' own
    --history-rows/--history-cols/--history-cell-bytes knobs."""
    if not turns or args.endpoint != "translate":
        return None
    return build_synthetic_history(turns, args.history_rows, args.history_cols, args.history_cell_bytes)


def _history_payload_kb(args):
    """Approximate on-the-wire size (KB) of a FULL --history-turns-sized
    history array, for the "what am I actually sending" line each mode
    prints once up front when --history-turns is set - json.dumps here is
    just for sizing, not what's actually sent (requests does its own
    encoding of the payload dict)."""
    history = _history_for(args, args.history_turns)
    if not history:
        return 0.0
    return len(json.dumps(history)) / 1024.0


def prime_session(session, base_url):
    """One cheap call so the shared session's identity/cookie is already
    set before the real burst/sustained calls begin - keeps every
    subsequent request unambiguously "the same user" from the first one."""
    resp = session.get(base_url.rstrip("/") + "/api/auth/me", timeout=15)
    return resp.json()


def cmd_whoami(args):
    session = requests.Session()
    info = prime_session(session, args.url)
    print("Response:", info)
    print("Cookies set:", session.cookies.get_dict())


def cmd_burst(args):
    session = requests.Session()
    who = prime_session(session, args.url)
    print(f"Simulated user identity: {who.get('user_id')}")
    print(f"Firing {args.n} concurrent /{args.endpoint} requests from this ONE user...")
    if args.history_turns and args.endpoint == "translate":
        print(
            f"Each request carries a full {args.history_turns}-turn synthetic history "
            f"(~{_history_payload_kb(args):.1f} KB) - simulating {args.n} users already mid-conversation."
        )

    # Fixed at the full --history-turns for every one of the n concurrent
    # requests (not grown per-request like sustained/fairness below) -
    # burst represents n DIFFERENT users hitting the service at the same
    # instant, each already mid-conversation, not one user's conversation
    # lengthening over time.
    history = _history_for(args, args.history_turns)

    results = [None] * args.n
    barrier = threading.Barrier(args.n)

    def worker(i):
        barrier.wait()  # line everyone up so the burst is genuinely simultaneous
        results[i] = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout, history=history)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i, r in enumerate(results):
        print_result(f"request {i}", r)
    summarize_results(results)


def cmd_sustained(args):
    session = requests.Session()
    who = prime_session(session, args.url)
    print(f"Simulated user identity: {who.get('user_id')}")
    print(f"Firing /{args.endpoint} every {args.interval}s for {args.duration}s from this ONE user...")
    if args.history_turns and args.endpoint == "translate":
        print(
            f"History grows 1 turn per call up to {args.history_turns} turns "
            f"(~{_history_payload_kb(args):.1f} KB once full) - simulating this one conversation lengthening over time."
        )

    results = []
    turn_count = 0
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        # Grows turn-by-turn, capped at --history-turns - mirrors
        # createChatHistoryStore().pushTurn() in webClient/client.js: a
        # real conversation's history lengthens one turn per exchange,
        # not all at once.
        turn_count = min(turn_count + 1, args.history_turns) if args.history_turns else 0
        history = _history_for(args, turn_count)
        r = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout, history=history)
        results.append(r)
        print_result(f"request {len(results)}", r)
        time.sleep(args.interval)
    summarize_results(results)


def cmd_fairness(args):
    print(
        f"Running {args.users} independent simulated users, "
        f"{args.calls_per_user} calls each, {args.interval}s apart..."
    )
    if args.history_turns and args.endpoint == "translate":
        print(
            f"Each user's own history grows 1 turn per call up to {args.history_turns} turns "
            f"(~{_history_payload_kb(args):.1f} KB once full) - {args.users} such conversations "
            f"can be in flight at once, which is the actual memory-pressure scenario this covers."
        )
    per_user_results = {}
    lock = threading.Lock()

    def run_user(u):
        session = requests.Session()
        who = prime_session(session, args.url)
        results = []
        turn_count = 0
        for _ in range(args.calls_per_user):
            turn_count = min(turn_count + 1, args.history_turns) if args.history_turns else 0
            history = _history_for(args, turn_count)
            r = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout, history=history)
            results.append(r)
            time.sleep(args.interval)
        with lock:
            per_user_results[u] = (who.get("user_id"), results)

    threads = [threading.Thread(target=run_user, args=(u,)) for u in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for u in sorted(per_user_results):
        identity, results = per_user_results[u]
        print(f"\n--- user {u} ({identity}) ---")
        for i, r in enumerate(results):
            print_result(f"request {i}", r)
        summarize_results(results)


def _load_datasets(path, require_sql=False):
    """Loads --datasets-file: a non-empty JSON list of
    {"preset_id": "...", "name": "<optional>", "sql": "<optional unless
    require_sql>"} objects, one per real dataset. A JSON file rather than
    a CLI flag - a genuinely substantial SQL query easily contains
    commas/quotes/newlines that would be painful and error-prone to pack
    into a comma-separated CLI argument. Only "preset_id" is universally
    required: soak mode passes require_sql=True (it always runs a fixed,
    reviewed query - see cmd_soak()), while conversation mode leaves it
    False (it runs whatever SQL translate itself generates each turn, so
    "sql" is unused even if present - see cmd_conversation()). See
    soak_datasets.example.json / conversation_datasets.json next to this
    script for the exact shape each mode expects."""
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as exc:
        raise SystemExit(f"--datasets-file {path!r} could not be opened: {exc}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--datasets-file {path!r} is not valid JSON: {exc}")
    if not isinstance(data, list) or not data:
        raise SystemExit(f"--datasets-file {path!r} must contain a non-empty JSON list")
    for i, d in enumerate(data):
        if not isinstance(d, dict) or not d.get("preset_id"):
            raise SystemExit(f"--datasets-file entry {i} must be an object with at least 'preset_id'")
        if require_sql and not d.get("sql"):
            raise SystemExit(f"--datasets-file entry {i} ({d['preset_id']!r}) is missing 'sql', required for --mode soak")
    return data


def pin_preset(session, base_url, preset_id, timeout):
    """Pins this simulated user's SESSION-level active connection to a
    real admin preset via POST /api/config {"preset_id": ...,
    "is_custom": false} - the same server-side mechanism
    (config_routes.py, keyed off the crbot_session_id cookie) the real
    webClient uses when someone picks a preset from the DB dropdown.
    Every subsequent execute/translate call for this user can then omit
    database_url entirely and still resolve against the right real
    connection/schema, exactly like a real user who picked a dataset
    once and kept chatting. Returns True/False rather than raising, so
    one bad preset id can't take the whole soak run down - the caller
    logs it and that user's cycles just run (and likely fail
    informatively) instead."""
    try:
        resp = session.post(
            base_url.rstrip("/") + "/api/config",
            json={"preset_id": preset_id, "is_custom": False},
            timeout=timeout,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def fetch_history_max_turns(base_url, timeout):
    """GET /api/config once - the exact same call the real client's own
    fetchBackendConfig() makes on page load - and returns its
    `history_max_turns` field (config_routes.py: 'history_max_turns':
    HISTORY_MAX_TURNS, this deployment's live env var value).

    This matters because the real client does NOT just send an
    ever-growing `history` array and trust the server to trim it: its own
    chatStore (createChatHistoryStore()/pushTurn()/setMaxTurns() in
    client.js) caps its in-memory history to this exact value BEFORE ever
    building a request - `history.slice(-maxEntries)` on every pushTurn(),
    re-applied via setMaxTurns() the moment /api/config's response is in.
    That client-side cap is also what limits how many turns ever show up
    in the UI - not merely the server's own defensive
    `history[-(HISTORY_MAX_TURNS*2):]` slice in translate_routes.py, which
    a real browser's request essentially never needs, since it never
    arrives holding more than HISTORY_MAX_TURNS turns in the first place.

    Returns None on any failure (unreachable, unexpected shape, missing/
    non-positive field) - callers should treat that as "couldn't confirm
    the real cap" and fall back to NOT capping turns client-side, same as
    this script's behavior before this fix existed. That fallback can't
    cause a bad LLM call either way: the server's own defensive slice
    still protects the actual /api/translate prompt regardless of what
    arrives - a failed fetch here only risks sending a larger, less
    realistic `history` payload than a real browser ever would, not a
    broken run."""
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/config", timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        n = data.get("history_max_turns") if isinstance(data, dict) else None
        return int(n) if isinstance(n, (int, float)) and n > 0 else None
    except (requests.exceptions.RequestException, ValueError):
        return None


def _cap_result_rows(raw, max_rows):
    """Shapes a plain list of statement-result dicts (whatever shape a
    fire_one(endpoint="execute") body's `results` array, or
    _statement_results_for_history()'s own synthesized list, uses) into
    the {"columns", "rowCount", "rows"} (or {"isError": True, "error",
    ...}) shape history/summarize calls expect, trimming each result
    set's rows to at most max_rows (0/None = no cap) - mirrors the
    server's own HISTORY_RESULT_MAX_ROWS trimming.

    A per-statement failure comes back shaped {"error": ..., "statement"?,
    "database"?} rather than {"columns", "rows", "rowCount"} - preserved
    as-is (as {"isError": True, "error": ..., ...}), mirroring
    webClient/client.js's own summarizeResultForHistory(), which carries
    this exact fix in its own comment: silently running this entry
    through the columns/rows/rowCount shape instead collapses a real
    error into a fake "0-row success" - the error text just vanishes
    instead of reaching history (or, for conversation mode, the next
    turn's translate call). Non-dict entries are skipped."""
    capped = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        if r.get("error") is not None:
            entry = {"isError": True, "error": r["error"]}
            if r.get("statement") is not None:
                entry["statement"] = r["statement"]
            if r.get("database") is not None:
                entry["database"] = r["database"]
            capped.append(entry)
            continue
        rows = r.get("rows") or []
        capped.append({
            "columns": r.get("columns"),
            "rowCount": r.get("rowCount", len(rows)),
            "rows": rows[:max_rows] if max_rows else rows,
        })
    return capped


def _extract_capped_results(exec_result, max_rows):
    """Pulls the `results` array out of a fire_one(endpoint="execute")
    return value's captured JSON body (see fire_one()'s own comment) and
    runs it through _cap_result_rows(). Soak mode's own helper - it only
    ever calls this on a successful (200) execute result (see
    _append_real_turn_to_history() and cmd_soak's own --soak-summarize
    branch), so it doesn't need to handle a failure response's shape -
    see _statement_results_for_history() for the more general version
    conversation mode uses, which folds a failed execute's partial/error
    results into history too, matching the real client.

    Returns None if the call failed or came back in an unexpected shape
    (e.g. a non-200, or a body with no `results` key) - callers decide
    their own fallback."""
    body = exec_result.get("body") if isinstance(exec_result, dict) else None
    if not isinstance(body, dict):
        return None
    raw = body.get("results")
    if not isinstance(raw, list):
        return None
    return _cap_result_rows(raw, max_rows)


def _statement_results_for_history(exec_r):
    """Given a fire_one(endpoint="execute") return value of ANY status
    (success, partial multi-statement failure, or a bare failure like an
    EXECUTE_GUARD 503 or a syntax error), returns the raw list of
    statement-result dicts conversation mode should feed to
    _cap_result_rows() for BOTH the /api/summarize-result call and the
    next turn's history entry - or None when there's truly nothing to
    build a turn from at all (a request-level failure with no HTTP
    response, e.g. a client timeout or dropped connection).

    This mirrors webClient/client.js's own execute-response handling
    (see its comment: "a concluded turn's 'results or errors' must be
    added to history") - the real client does NOT stop at "execute
    wasn't a 200": it still summarizes and persists a turn for a partial
    multi-statement failure (results = the statements that succeeded
    before the failure, PLUS one trailing {"error": ...} entry for the
    one that didn't - see execute_routes.py's SqlExecutionError shape:
    {"results": [...], "failedStatement": ..., "error": ...}), and for a
    bare failure with no partial results at all (e.g. EXECUTE_GUARD's
    503 body, {"success": False, "error": "..."})  it still summarizes
    and persists a single {"error": ...} entry. Before this function
    existed, this script treated ANY non-200 execute as "nothing to
    summarize or fold into history," silently dropping every completed
    statement's real data whenever the LAST statement in a script failed,
    and never recording a bare execute failure (guard-busy, bad SQL) into
    history at all - understating exactly the "multiple resultsets across
    several tabs" case conversation mode is supposed to exercise
    faithfully."""
    status = exec_r.get("status") if isinstance(exec_r, dict) else None
    if status is None:
        # Request-level failure (timeout/connection drop, see fire_one()'s
        # own except branch) - no HTTP response, and so no `data` object
        # for the real client to have built a turn from either.
        return None
    body = exec_r.get("body")
    if status == 200:
        raw = body.get("results") if isinstance(body, dict) else None
        return raw if isinstance(raw, list) else []
    if not isinstance(body, dict):
        # A non-200 with no parseable JSON body at all (shouldn't happen
        # against this server - every failure branch in execute_routes.py
        # returns JSON - but defensively still worth a turn, same as the
        # real client's bare-failure branch).
        return [{"error": f"HTTP {status} with no parseable response body"}]
    if isinstance(body.get("results"), list) or body.get("failedStatement") is not None:
        # SqlExecutionError's partial-failure shape (execute_routes.py:
        # {"results": [statements that succeeded before the failure],
        # "failedStatement": ..., "error": ...}) - client.js's
        # `statementResults = [...data.results, {error: errMsg}]`.
        return list(body.get("results") or []) + [{"error": body.get("error")}]
    # A bare failure with nothing else to show alongside it (EXECUTE_GUARD's
    # 503, a connect() failure, a single-statement script's own error) -
    # client.js's `[{error: errMsg}]`.
    return [{"error": body.get("error", f"HTTP {status} with no error message")}]


def _append_real_turn_to_history(history, sql, exec_result, max_turns, max_rows_per_turn):
    """Appends one (question, REAL-execute-result) turn pair to a
    growing `history` list and returns the (possibly trimmed) new list -
    the soak-mode counterpart to build_synthetic_history(), using an
    actual /api/execute response instead of synthetic filler (see this
    script's own SOAK MODE docstring section for why). If that cycle's
    execute call failed or returned an unexpected shape, still appends a
    text-only turn (empty results) rather than skipping it, so translate
    keeps seeing a growing history even through an occasional bad cycle.
    max_turns=0 means uncapped growth (soak mode's default - see the
    docstring); a positive value caps it at that many turns, oldest
    dropped first."""
    capped = _extract_capped_results(exec_result, max_rows_per_turn)
    history = history + [
        {"role": "user", "text": f"stress-soak question against: {sql[:80]}"},
        {"role": "model", "text": sql, "results": capped if capped is not None else []},
    ]
    if max_turns:
        history = history[-(max_turns * 2):]
    return history


def _print_soak_plan(args, datasets):
    approx_cycles = int(args.duration // args.interval) if args.interval else 0
    calls_per_cycle = 2 + (1 if args.soak_summarize else 0)
    print("Soak plan (--dry-run, no network calls made):")
    for u in range(args.users):
        dataset = datasets[u % len(datasets)]
        label = dataset.get("name") or dataset["preset_id"]
        print(f"  user {u}: dataset={label!r} preset_id={dataset['preset_id']!r} sql={dataset['sql']!r}")
    print(f"  duration={args.duration}s interval={args.interval}s -> ~{approx_cycles} cycles/user")
    print(
        f"  ~{calls_per_cycle} calls/cycle (execute + translate"
        f"{' + summarize' if args.soak_summarize else ''}) "
        f"-> ~{approx_cycles * calls_per_cycle} total calls/user, "
        f"~{approx_cycles * calls_per_cycle * args.users} total calls across all {args.users} users"
    )
    print(
        f"  history: {'uncapped growth' if not args.history_turns else f'capped at {args.history_turns} turns'}, "
        f"up to {args.history_rows} real rows/turn carried forward"
    )


def cmd_soak(args):
    if not args.datasets_file:
        raise SystemExit("--mode soak requires --datasets-file (see this script's SOAK MODE docstring section)")
    datasets = _load_datasets(args.datasets_file, require_sql=True)

    if args.dry_run:
        _print_soak_plan(args, datasets)
        return

    print(
        f"Soak test: {args.users} users across {len(datasets)} dataset(s), "
        f"sustained for {args.duration}s ({args.interval}s between each user's cycles)..."
    )
    per_user_results = {}
    lock = threading.Lock()

    def run_user(u):
        dataset = datasets[u % len(datasets)]
        label = dataset.get("name") or dataset["preset_id"]
        session = requests.Session()
        who = prime_session(session, args.url)
        pinned = pin_preset(session, args.url, dataset["preset_id"], args.timeout)
        if not pinned:
            print(f"  [user {u}] WARNING: failed to pin preset {dataset['preset_id']!r} - calls may hit the wrong dataset")

        history = []
        results = []
        cycle = 0
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            cycle += 1
            exec_r = fire_one(session, args.url, "execute", None, None, args.timeout, sql=dataset["sql"])
            results.append(("execute", exec_r))
            print_result(f"user {u} [{label}] cycle {cycle} execute", exec_r)

            history = _append_real_turn_to_history(history, dataset["sql"], exec_r, args.history_turns, args.history_rows)

            trans_r = fire_one(session, args.url, "translate", args.model, None, args.timeout, history=history)
            results.append(("translate", trans_r))
            print_result(f"user {u} [{label}] cycle {cycle} translate (history={len(history)} entries)", trans_r)

            if args.soak_summarize:
                capped = _extract_capped_results(exec_r, args.history_rows)
                summ_r = fire_one(
                    session, args.url, "summarize", args.model, None, args.timeout,
                    sql=dataset["sql"], exec_results=capped,
                )
                results.append(("summarize", summ_r))
                print_result(f"user {u} [{label}] cycle {cycle} summarize", summ_r)

            time.sleep(args.interval)

        with lock:
            per_user_results[u] = (who.get("user_id"), label, results)

    threads = [threading.Thread(target=run_user, args=(u,)) for u in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for u in sorted(per_user_results):
        identity, label, results = per_user_results[u]
        print(f"\n--- user {u} ({identity}) [{label}] ---")
        for endpoint in ("execute", "translate", "summarize"):
            subset = [r for (ep, r) in results if ep == endpoint]
            if subset:
                print(f"  {endpoint}:")
                summarize_results(subset)


# Pulled verbatim from webClient/index.html's own "#examplePrompts" quick-
# prompt chips (the single-connection "data-prompt" wording, not the
# "data-prompt-all" variant used only in all-databases mode - see that
# file's own comment on why the two are kept separate) - these are the
# ACTUAL, already-tested-by-hand prompts this app ships to real users,
# not ones this script made up. Deliberately excludes that same element's
# commented-out "Write Data" chip (create a table/insert a record) - it's
# disabled in the real UI for a reason, and running writes against real
# production data as part of a load test would be actively harmful, not
# just unrepresentative.
#
# Two of these four are, BY THE APP'S OWN DESIGN, sometimes answered with
# a "*** NO SQL ***"-prefixed prose reply instead of real SQL - see
# translate_routes.py's _COMMON_FORMAT_RULES: a schema/"what's in
# here"-type question is explicitly one of the cases the model is told to
# answer from its knowledge of the schema alone, no query needed. That's
# not a failure (see _extract_sql()'s own handling of this sentinel) - a
# real user clicking that same chip in the real app gets the same kind of
# answer. --turns-per-conversation worth of rotation through all four
# means some turns legitimately produce no execute/summarize call at all,
# by design, same as the real UI.
QUICK_PROMPTS = [
    "What tables are in this dataset and how are they related?",
    "Show me a few records from a couple of tables that you find most interesting.",
    "Analyze this dataset and give me a couple of insights from it with supporting data.",
    "What are a few interesting questions to ask about the data",
]


def _load_prompts(path):
    """Loads a --prompts-file for --mode conversation: one prompt per
    non-blank line, '#'-prefixed lines treated as comments and ignored -
    see conversation_prompts.example.txt next to this script, seeded
    with QUICK_PROMPTS above as a starting point to edit/extend with
    your own (e.g. deliberately worst-case) prompts. Plain text rather
    than JSON specifically so you can type/paste prompts containing
    quotes, apostrophes, etc. without needing to escape anything.
    Raises SystemExit if the file has no usable prompts left after
    stripping blanks/comments, or can't be read at all - a --mode
    conversation run with zero prompts would just hang doing nothing
    useful, so this fails fast instead."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        raise SystemExit(f"--prompts-file {path!r} could not be read: {exc}")
    prompts = [line.strip() for line in lines]
    prompts = [line for line in prompts if line and not line.startswith("#")]
    if not prompts:
        raise SystemExit(
            f"--prompts-file {path!r} contained no usable prompts "
            "(blank lines and lines starting with # are treated as comments and skipped)"
        )
    return prompts


# Mirrors translate_routes.py's own _NO_SQL_PREFIX_RE exactly: the app's
# system prompt explicitly tells the model to prepend this literal marker
# whenever it answers in prose instead of generating a real query - a
# from-training-knowledge answer, a schema/ER-diagram question, a
# question about the app itself, an ambiguous/out-of-scope request, or a
# translation error all use this same convention (see
# translate_routes.py's _COMMON_FORMAT_RULES). Critically, the terminal
# NDJSON line's own "success" is still `true` in every one of these cases
# - translate did its job correctly - so "success: true" alone does NOT
# mean the "sql" field is real, executable SQL. Blindly executing a
# "*** NO SQL *** <explanation>" string is exactly what produced the wall
# of syntax errors ('Unexpected "**"', 'syntax error near "***"', a MySQL
# 1064) across every dialect in an earlier run of this script.
_NO_SQL_PREFIX_RE = re.compile(r'^\*\*\*\s*NO\s*SQL\s*\*\*\*\s*', re.IGNORECASE)


def _extract_sql(trans_result):
    """Pulls the real, LLM-generated SQL out of a
    fire_one(endpoint="translate") return value's captured NDJSON body
    (see fire_one()'s own comment) - the terminal
    {"status": "done", "success": true, "sql": ...} line (see
    translate_routes.py's stream_translation()). Returns None (rather
    than raising) on a guard rejection, an LLM/translation failure, a
    legitimate "*** NO SQL ***" prose answer (see _NO_SQL_PREFIX_RE above),
    or any unexpected shape - cmd_conversation() treats all of these
    identically as "skip execute/summarize this turn," never as a reason
    to abort the run."""
    body = trans_result.get("body") if isinstance(trans_result, dict) else None
    if not isinstance(body, dict) or not body.get("success"):
        return None
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return None
    if _NO_SQL_PREFIX_RE.match(sql.strip()):
        return None
    return sql


def _no_sql_reason(trans_r):
    """Explains WHY _extract_sql(trans_r) came back None - for the
    diagnostic print in cmd_conversation()'s per-turn loop. Important
    distinction this exists to surface: fire_one()'s "status" is the
    STREAMED HTTP RESPONSE's transport status, not whether translation
    itself succeeded - translate_routes.py's stream_translation() sends
    HTTP 200 as soon as it starts streaming (before the LLM has even been
    called), so a real content-level failure (LLM error, timeout,
    provider quota) still shows up as "status=200" in fire_one()'s own
    result dict, with success/failure only visible in the parsed NDJSON
    body's own "success"/"error" fields (see that route's own terminal
    yield). Without this, a run can look like every translate call
    "succeeded" (200) while zero of them ever actually produced SQL."""
    body = trans_r.get("body") if isinstance(trans_r, dict) else None
    if isinstance(body, dict):
        if body.get("success") is False:
            return f"translate itself failed: {body.get('error')!r}"
        if body.get("success") is True:
            sql = body.get("sql")
            if isinstance(sql, str) and _NO_SQL_PREFIX_RE.match(sql.strip()):
                explanation = _NO_SQL_PREFIX_RE.sub("", sql.strip())
                return f"translate answered in prose instead of real SQL ('*** NO SQL ***'): {explanation[:150]!r}"
            return f"translate reported success but returned no usable 'sql' field (body keys: {sorted(body.keys())})"
        if "error" in body:
            # Early-validation/guard-rejection shape ({"error": ...}, no
            # "success" key at all) - see translate_query()'s own comment
            # on why that shape has no "success" key.
            return f"request rejected before streaming: {body.get('error')!r}"
        return f"unrecognized response shape (body keys: {sorted(body.keys())})"
    if trans_r.get("error"):
        return f"request-level failure (timeout/connection error): {trans_r['error']}"
    return "no parseable response body captured (empty or non-JSON body)"


def _print_conversation_plan(args, datasets, prompts):
    approx_conversations_per_user = max(int(args.duration // (args.interval * args.turns_per_conversation)), 1) if args.interval else 1
    print("Conversation plan (--dry-run, no network calls made):")
    print(f"  {args.users} users, {len(datasets)} dataset(s) in the pool, {args.turns_per_conversation} turns/conversation")
    for u in range(min(args.users, len(datasets))):
        dataset = datasets[u % len(datasets)]
        label = dataset.get("name") or dataset["preset_id"]
        print(f"  user {u} starts on: {label!r} (preset_id={dataset['preset_id']!r}), then rotates through the rest of the pool")
    prompts_source = f"--prompts-file {args.prompts_file!r}" if args.prompts_file else "the app's own QUICK_PROMPTS"
    print(f"  prompts: {len(prompts)} loaded from {prompts_source}, order={args.prompt_order!r}")
    for i, pr in enumerate(prompts):
        print(f"    [{i}] {pr!r}")
    print(
        f"  duration={args.duration}s, interval={args.interval}s between turns -> "
        f"~{approx_conversations_per_user} conversations/user over the run "
        f"(each conversation touches a new dataset, fresh history)"
    )
    print(
        f"  up to 3 calls/turn (translate, + execute/summarize only when translate/execute succeed) "
        f"x {args.turns_per_conversation} turns/conversation x ~{approx_conversations_per_user} conversations "
        f"x {args.users} users"
    )
    print(
        "  history/summarize payloads carry the REAL execute result uncapped (matching the real "
        "client - see _extract_capped_results()'s own docstring), not trimmed to --history-rows "
        "(that knob is soak-mode only)"
    )
    print(
        "  history TURN COUNT is capped to match the real client's own chatStore (see "
        "fetch_history_max_turns()'s own docstring) - fetched live via GET /api/config at run start, "
        "so it isn't known yet in --dry-run (no network calls made here)"
    )
    print("  WARNING: execute runs whatever SQL translate actually generates - see this script's CONVERSATION MODE docstring section")


def cmd_conversation(args):
    if not args.datasets_file:
        raise SystemExit("--mode conversation requires --datasets-file (see this script's CONVERSATION MODE docstring section)")
    datasets = _load_datasets(args.datasets_file, require_sql=False)
    prompts = _load_prompts(args.prompts_file) if args.prompts_file else QUICK_PROMPTS

    if args.dry_run:
        _print_conversation_plan(args, datasets, prompts)
        return

    print(
        f"Conversation test: {args.users} users cycling through {len(datasets)} dataset(s), "
        f"sustained for {args.duration}s ({args.turns_per_conversation} turns/conversation, "
        f"{args.interval}s between turns)..."
    )

    # One GET /api/config up front - same call the real client's own
    # fetchBackendConfig() makes on page load - so this run caps its own
    # outgoing `history` the same way a real browser's chatStore does
    # (see fetch_history_max_turns()'s own docstring for the full
    # reasoning). Fetched once here, not per-user: it's live server
    # config, not per-session state, so one probe speaks for the whole
    # run.
    history_max_turns = fetch_history_max_turns(args.url, args.timeout)
    if history_max_turns:
        print(
            f"  history cap: {history_max_turns} turns (live from GET /api/config's 'history_max_turns' "
            f"- matches the real client's own chatStore cap)"
        )
        if args.turns_per_conversation > history_max_turns:
            print(
                f"  note: --turns-per-conversation ({args.turns_per_conversation}) exceeds this cap - "
                f"conversations will run that long, but history will roll off older turns past "
                f"{history_max_turns}, exactly like a real long conversation would, rather than growing "
                f"past what a real browser client could ever actually send"
            )
    else:
        print(
            "  history cap: could not be confirmed via GET /api/config - history will grow uncapped per "
            f"conversation (up to --turns-per-conversation={args.turns_per_conversation} turns), which may "
            "exceed what a real browser client would ever actually send"
        )

    per_user_results = {}
    lock = threading.Lock()

    def run_user(u):
        # Every network call inside this loop (fire_one, pin_preset)
        # already catches requests.exceptions.RequestException itself and
        # returns an error dict rather than raising - see those functions'
        # own docstrings - so a busy/rate-limited/down LLM provider or
        # database, a guard 503, a client-side timeout, or a dropped
        # connection all show up as an ordinary per-turn result (status
        # None or 5xx) and just cause that ONE turn to be skipped (see the
        # `if sql is None`/`if exec_r.get("status") != 200` branches
        # below) - never an exception. This outer try/except exists only
        # for the one call this loop makes that DOESN'T already catch its
        # own errors - prime_session()'s initial /api/auth/me priming
        # call - plus any other genuinely unexpected failure, so a crash
        # there logs clearly and still reports whatever partial results
        # this user gathered, instead of that user silently vanishing
        # from the final report while the other users' threads carry on
        # unaffected.
        who = {}
        results = []
        try:
            session = requests.Session()
            who = prime_session(session, args.url)
            conversation_num = 0
            deadline = time.monotonic() + args.duration

            while time.monotonic() < deadline:
                dataset = datasets[(u + conversation_num) % len(datasets)]
                label = dataset.get("name") or dataset["preset_id"]
                pinned = pin_preset(session, args.url, dataset["preset_id"], args.timeout)
                if not pinned:
                    print(f"  [user {u}] WARNING: failed to pin preset {dataset['preset_id']!r} - this conversation may hit the wrong dataset")

                # Fresh history per conversation/dataset - a real user
                # picking a different dataset starts a new train of
                # thought, not one dragging in a previous, unrelated
                # schema's turns (see CONVERSATION MODE's own docstring
                # section).
                history = []
                for turn in range(args.turns_per_conversation):
                    if time.monotonic() >= deadline:
                        break
                    # Rotate through `prompts` (the app's own QUICK_PROMPTS,
                    # or your own --prompts-file pool). --prompt-order
                    # defaults to "roundrobin" (deterministic, in order -
                    # keeps runs comparable to each other); "random" picks
                    # a fresh one each turn instead, which is more likely
                    # to surface worst-case combinations if your prompts
                    # file has more entries than --turns-per-conversation.
                    if args.prompt_order == "random":
                        prompt_text = random.choice(prompts)
                    else:
                        prompt_text = prompts[turn % len(prompts)]

                    trans_r = fire_one(session, args.url, "translate", args.model, None, args.timeout, history=history, prompt=prompt_text)
                    results.append(("translate", trans_r))
                    print_result(f"user {u} [{label}] conv {conversation_num} turn {turn} translate ({prompt_text[:40]!r}...)", trans_r)

                    sql = _extract_sql(trans_r)
                    if sql is None:
                        # No usable SQL this turn - a busy/rate-limited
                        # LLM provider, a TRANSLATE_GUARD 503, a client
                        # timeout, or an outright LLM/parse failure all
                        # land here identically (see _extract_sql()'s own
                        # docstring) - skip execute/summarize and leave
                        # history untouched rather than polluting it with
                        # a fabricated turn. This turn still counts
                        # against --turns-per-conversation (it isn't
                        # retried), so a run with a lot of busy responses
                        # simply ends up with shorter real history per
                        # conversation, not a stuck or crashed user.
                        print(f"    -> skipping execute/summarize this turn: {_no_sql_reason(trans_r)}")
                        time.sleep(args.interval)
                        continue

                    exec_r = fire_one(session, args.url, "execute", None, None, args.timeout, sql=sql)
                    results.append(("execute", exec_r))
                    print_result(f"user {u} [{label}] conv {conversation_num} turn {turn} execute", exec_r)

                    # A multi-statement script (semicolon-separated SQL -
                    # see execute_routes.py's own docstring) can produce
                    # MULTIPLE result sets from ONE execute call - the
                    # real client shows each as its own tab. Whether the
                    # whole thing succeeded (HTTP 200, N/N result sets),
                    # failed partway through (HTTP 400 SqlExecutionError,
                    # the statements before the failure PLUS the failure
                    # itself), or failed outright with nothing partial to
                    # show (an EXECUTE_GUARD 503, a bad first statement),
                    # _statement_results_for_history() returns the exact
                    # list of statement-result dicts to carry forward -
                    # see its own docstring for why ALL of these, not just
                    # a clean 200, still need to reach history: the real
                    # client's own rule is "a concluded turn's results or
                    # errors must be added to history," and it summarizes/
                    # persists a failed execute too, not just a successful
                    # one. Only a genuine request-level failure (timeout,
                    # dropped connection - no HTTP response at all) has
                    # truly nothing to build a turn from, same as the real
                    # client's own fetch()-catch path.
                    raw_results = _statement_results_for_history(exec_r)
                    if raw_results is None:
                        print(f"    -> skipping summarize/history this turn: execute request-level failure: {exec_r.get('error')}")
                        time.sleep(args.interval)
                        continue
                    if exec_r.get("status") != 200:
                        print(f"    -> execute did not return 200, but still summarizing/folding {len(raw_results)} statement result(s) (including the failure) into history, matching the real client")

                    # Conversation mode's whole point is mirroring the real
                    # client end to end (see this script's own CONVERSATION
                    # MODE docstring section). The real client
                    # (webClient/client.js's summarizeResultForHistory())
                    # never trims rows itself before sending either the
                    # summarize request body or the next turn's history -
                    # /api/execute already capped what it returned
                    # (EXECUTE_RESULTS_MAX_ROWS), and translate_routes.py
                    # does its own HISTORY_RESULT_MAX_ROWS trim when it
                    # builds the LLM prompt from whatever history arrives.
                    # Capping here too (as this used to do, via
                    # --history-rows) made every conversation-mode request
                    # payload smaller than a real user's ever would be -
                    # understating exactly the memory pressure --mode soak
                    # exists to probe. --history-rows remains a deliberate,
                    # soak-mode-only knob (see _append_real_turn_to_history
                    # and cmd_soak's own --soak-summarize branch) -
                    # conversation mode always forwards the real, uncapped
                    # execute result(s), max_rows=0 meaning "no cap" (see
                    # _cap_result_rows()'s own docstring).
                    capped = _cap_result_rows(raw_results, max_rows=0)
                    summ_r = fire_one(
                        session, args.url, "summarize", args.model, None, args.timeout,
                        sql=sql, exec_results=capped, prompt=prompt_text,
                    )
                    results.append(("summarize", summ_r))
                    print_result(f"user {u} [{label}] conv {conversation_num} turn {turn} summarize", summ_r)

                    # A busy/failed SUMMARIZE call (summ_r not 200) does
                    # NOT block history growth - the history entry is
                    # built from the real, already-captured execute
                    # result(s) (success, partial, or bare failure - see
                    # raw_results above), not from summarize's own text
                    # output, so a momentarily-busy summarizer degrades
                    # gracefully exactly like it would for a real user
                    # still seeing their query results without a summary.
                    history = history + [
                        {"role": "user", "text": prompt_text},
                        {"role": "model", "text": sql, "results": capped},
                    ]
                    # Mirrors client.js's own chatStore.pushTurn(), which
                    # runs `history = history.slice(-maxEntries)` after
                    # EVERY push, not just occasionally - the real client
                    # never lets its outgoing `history` grow past
                    # history_max_turns turns in the first place (see
                    # fetch_history_max_turns()'s own docstring). Without
                    # this, --turns-per-conversation set higher than the
                    # server's real HISTORY_MAX_TURNS would send a bigger,
                    # ever-growing `history` payload than any real browser
                    # client could ever actually produce - not a faithful
                    # stress of a long real conversation, just an
                    # unrealistic shape. A None/0 history_max_turns (the
                    # GET /api/config probe at the top of this command
                    # failed) leaves growth uncapped, same as before this
                    # fix existed.
                    if history_max_turns:
                        history = history[-(history_max_turns * 2):]
                    time.sleep(args.interval)

                conversation_num += 1
        except Exception as exc:
            print(f"  [user {u}] CRASHED (unexpected, not a normal per-turn busy/failure): {exc!r} - this user's run stopped early; other users are unaffected.")
        finally:
            with lock:
                per_user_results[u] = (who.get("user_id"), results)

    threads = [threading.Thread(target=run_user, args=(u,)) for u in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for u in sorted(per_user_results):
        identity, results = per_user_results[u]
        print(f"\n--- user {u} ({identity}) ---")
        for endpoint in ("translate", "execute", "summarize"):
            subset = [r for (ep, r) in results if ep == endpoint]
            if subset:
                print(f"  {endpoint}:")
                summarize_results(subset)
                if endpoint == "translate":
                    # A translate call's HTTP status alone (what
                    # summarize_results() just printed) can't tell you
                    # whether execute ever ran - see _no_sql_reason()'s
                    # own docstring for why "status=200" doesn't imply
                    # usable SQL came back. This is the quantified answer:
                    # how many of these 200s actually had a real 'sql' to
                    # hand to execute.
                    with_sql = sum(1 for r in subset if _extract_sql(r) is not None)
                    print(f"    -> {with_sql}/{len(subset)} translate calls actually produced usable SQL "
                          f"(the rest skipped execute/summarize - see the '-> skipping...' lines above for why)")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True, help="Deployed Cloud Run service base URL")
    p.add_argument("--mode", required=True, choices=["whoami", "burst", "sustained", "fairness", "soak", "conversation"])
    p.add_argument("--endpoint", default="translate", choices=["translate", "execute", "summarize"])
    p.add_argument("--model", default=None, help="Override model - use your cheapest configured one")
    p.add_argument("--database-url", default=None, help="Override target DB preset (default: server's DATABASE_DEFAULT)")
    p.add_argument("--timeout", type=float, default=90.0, help="Per-request client timeout in seconds")
    p.add_argument("--n", type=int, default=6, help="[burst] number of simultaneous requests")
    p.add_argument("--interval", type=float, default=2.0, help="[sustained/fairness/soak] seconds between each user's requests/cycles")
    p.add_argument("--duration", type=float, default=60.0, help="[sustained/soak] seconds to keep firing")
    p.add_argument("--users", type=int, default=3, help="[fairness/soak] number of independent simulated users")
    p.add_argument("--calls-per-user", type=int, default=15, help="[fairness] calls each simulated user makes")
    p.add_argument(
        "--history-turns", type=int, default=0,
        help="[translate endpoint only] attach a multi-turn conversation history to every call, growing to "
             "this many turns (0 = no history for burst/sustained/fairness, but UNCAPPED growth for soak - "
             "see this script's own HISTORY PAYLOAD and SOAK MODE sections above)",
    )
    p.add_argument(
        "--history-rows", type=int, default=10,
        help="[soak only] Rows per synthetic/real history turn's result set (0 = uncapped). Not used by "
             "conversation mode, which always forwards the real, uncapped execute result - matching the "
             "real client - regardless of this flag.",
    )
    p.add_argument("--history-cols", type=int, default=8, help="Columns per synthetic history turn's result row")
    p.add_argument(
        "--history-cell-bytes", type=int, default=24,
        help="Approx size in bytes of each synthetic cell value - raise to simulate wider/heavier result data",
    )
    p.add_argument(
        "--datasets-file", default=None,
        help="[soak/conversation] JSON file listing real datasets - {preset_id, sql, name?} for soak "
             "(soak_datasets.example.json), {preset_id, name?} for conversation (conversation_datasets.json) - "
             "see this script's own SOAK MODE / CONVERSATION MODE docstring sections",
    )
    p.add_argument(
        "--soak-summarize", action="store_true",
        help="[soak] also fire /api/summarize-result each cycle using that cycle's real execute result",
    )
    p.add_argument(
        "--turns-per-conversation", type=int, default=4,
        help="[conversation] prompt/translate/execute/summarize cycles per conversation before that user "
             "moves on to a fresh conversation on the next dataset",
    )
    p.add_argument(
        "--prompts-file", default=None,
        help="[conversation] plain text file, one prompt per line ('#' lines are comments) - your own "
             "prompt pool instead of the app's QUICK_PROMPTS. See conversation_prompts.example.txt next "
             "to this script (seeded with QUICK_PROMPTS) to copy and extend with your own, e.g. "
             "deliberately worst-case, prompts.",
    )
    p.add_argument(
        "--prompt-order", default="roundrobin", choices=["roundrobin", "random"],
        help="[conversation] 'roundrobin' (default) rotates through the prompt pool in order, one per "
             "turn, same prompt pool position every run - deterministic and easy to compare across runs. "
             "'random' picks a fresh prompt each turn instead - more likely to surface worst-case "
             "combinations when your --prompts-file has more entries than --turns-per-conversation.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="[soak/conversation] print the resolved plan and exit without making any network calls",
    )
    args = p.parse_args()

    {
        "whoami": cmd_whoami,
        "burst": cmd_burst,
        "sustained": cmd_sustained,
        "fairness": cmd_fairness,
        "soak": cmd_soak,
        "conversation": cmd_conversation,
    }[args.mode](args)


if __name__ == "__main__":
    main()