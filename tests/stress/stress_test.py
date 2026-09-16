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
"""

import argparse
import json
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


def build_payload(endpoint, model, database_url, history=None, sql=None, exec_results=None):
    if endpoint == "translate":
        payload = {"prompt": CHEAP_PROMPT}
        # history is client-held conversation state (see
        # build_synthetic_history()'s docstring) - only ever meaningful
        # for /api/translate, which is the only route that reads it.
        if history:
            payload["history"] = history
    elif endpoint == "execute":
        # sql lets soak mode run a real, substantial, dataset-specific
        # query (from --datasets-file) instead of every other mode's
        # trivial CHEAP_SQL - see cmd_soak().
        payload = {"sql": sql or CHEAP_SQL}
    elif endpoint == "summarize":
        # exec_results lets soak mode summarize a REAL execute result
        # (already shaped like /api/execute's own `results` array - see
        # _extract_capped_results()) instead of the trivial 1-row
        # payload every other mode uses.
        payload = {
            "prompt": CHEAP_PROMPT,
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


def fire_one(session, base_url, endpoint, model, database_url, timeout, history=None, sql=None, exec_results=None):
    path = ENDPOINT_PATHS[endpoint]
    payload = build_payload(endpoint, model, database_url, history=history, sql=sql, exec_results=exec_results)
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
            # Drain fully so elapsed time reflects the whole response and
            # the connection is released cleanly back to the pool.
            for _ in resp.iter_lines():
                pass
        else:
            # execute returns one plain JSON body (see execute_routes.py:
            # {'success', 'results', 'rowCount', 'executionTimeMs'}) -
            # capture it so soak mode can fold REAL results into its
            # growing history (see _extract_capped_results()). Unused by
            # burst/sustained/fairness, which never read this key.
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
        bits.append(f"error={r['error']}")
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


def _load_datasets(path):
    """Loads --datasets-file: a non-empty JSON list of
    {"preset_id": "...", "sql": "...", "name": "<optional>"} objects, one
    per real dataset a soak-mode user gets pinned to. A JSON file rather
    than a CLI flag - a genuinely substantial SQL query easily contains
    commas/quotes/newlines that would be painful and error-prone to pack
    into a comma-separated CLI argument. See soak_datasets.example.json
    next to this script for the exact shape."""
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
        if not isinstance(d, dict) or not d.get("preset_id") or not d.get("sql"):
            raise SystemExit(f"--datasets-file entry {i} must be an object with at least 'preset_id' and 'sql'")
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


def _extract_capped_results(exec_result, max_rows):
    """Pulls the `results` array out of a fire_one(endpoint="execute")
    return value's captured JSON body (see fire_one()'s own comment),
    trimming each result set's rows to at most max_rows (0/None = no
    cap) - mirrors the server's own HISTORY_RESULT_MAX_ROWS trimming.
    Returns None if the call failed or came back in an unexpected shape
    (e.g. a non-200, or a partial-failure body with no `results` key) -
    callers decide their own fallback."""
    body = exec_result.get("body") if isinstance(exec_result, dict) else None
    if not isinstance(body, dict):
        return None
    raw = body.get("results")
    if not isinstance(raw, list):
        return None
    capped = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        rows = r.get("rows") or []
        capped.append({
            "columns": r.get("columns"),
            "rowCount": r.get("rowCount", len(rows)),
            "rows": rows[:max_rows] if max_rows else rows,
        })
    return capped


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
    datasets = _load_datasets(args.datasets_file)

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


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True, help="Deployed Cloud Run service base URL")
    p.add_argument("--mode", required=True, choices=["whoami", "burst", "sustained", "fairness", "soak"])
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
    p.add_argument("--history-rows", type=int, default=10, help="Rows per synthetic history turn's result set")
    p.add_argument("--history-cols", type=int, default=8, help="Columns per synthetic history turn's result row")
    p.add_argument(
        "--history-cell-bytes", type=int, default=24,
        help="Approx size in bytes of each synthetic cell value - raise to simulate wider/heavier result data",
    )
    p.add_argument(
        "--datasets-file", default=None,
        help="[soak] JSON file listing real {preset_id, sql, name?} datasets - see soak_datasets.example.json "
             "and this script's own SOAK MODE docstring section",
    )
    p.add_argument(
        "--soak-summarize", action="store_true",
        help="[soak] also fire /api/summarize-result each cycle using that cycle's real execute result",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="[soak] print the resolved plan and exit without making any network calls",
    )
    args = p.parse_args()

    {
        "whoami": cmd_whoami,
        "burst": cmd_burst,
        "sustained": cmd_sustained,
        "fairness": cmd_fairness,
        "soak": cmd_soak,
    }[args.mode](args)


if __name__ == "__main__":
    main()