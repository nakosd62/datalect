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

EXAMPLES

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode whoami

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode burst \
      --endpoint translate --n 8 --model gemini-3.5-flash-lite

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode sustained \
      --endpoint translate --interval 2 --duration 90 --model gemini-3.5-flash-lite

  python3 stress_test.py --url https://ydyl-xxxxx.a.run.app --mode fairness \
      --endpoint translate --users 3 --calls-per-user 15 --interval 2 \
      --model gemini-3.5-flash-lite
"""

import argparse
import statistics
import threading
import time

import requests

# Deliberately trivial/cheap: a short prompt against a one-row query, so
# every mode's per-call cost (LLM tokens in, DB work) stays minimal
# regardless of how many calls a run ends up making.
CHEAP_PROMPT = "how many rows are in the smallest table"
CHEAP_SQL = "SELECT 1"


def build_payload(endpoint, model, database_url):
    if endpoint == "translate":
        payload = {"prompt": CHEAP_PROMPT}
    elif endpoint == "execute":
        payload = {"sql": CHEAP_SQL}
    elif endpoint == "summarize":
        payload = {
            "prompt": CHEAP_PROMPT,
            "sql": CHEAP_SQL + ";",
            "results": [{"columns": ["1"], "rows": [[1]], "rowCount": 1}],
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


def fire_one(session, base_url, endpoint, model, database_url, timeout):
    path = ENDPOINT_PATHS[endpoint]
    payload = build_payload(endpoint, model, database_url)
    is_streamed = endpoint in STREAMED_ENDPOINTS
    started = time.monotonic()
    try:
        resp = session.post(
            base_url.rstrip("/") + path,
            json=payload,
            timeout=timeout,
            stream=is_streamed,
        )
        if is_streamed:
            # Drain fully so elapsed time reflects the whole response and
            # the connection is released cleanly back to the pool.
            for _ in resp.iter_lines():
                pass
        return {
            "status": resp.status_code,
            "elapsed": time.monotonic() - started,
            "retry_after": resp.headers.get("Retry-After"),
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

    results = [None] * args.n
    barrier = threading.Barrier(args.n)

    def worker(i):
        barrier.wait()  # line everyone up so the burst is genuinely simultaneous
        results[i] = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout)

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

    results = []
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        r = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout)
        results.append(r)
        print_result(f"request {len(results)}", r)
        time.sleep(args.interval)
    summarize_results(results)


def cmd_fairness(args):
    print(
        f"Running {args.users} independent simulated users, "
        f"{args.calls_per_user} calls each, {args.interval}s apart..."
    )
    per_user_results = {}
    lock = threading.Lock()

    def run_user(u):
        session = requests.Session()
        who = prime_session(session, args.url)
        results = []
        for _ in range(args.calls_per_user):
            r = fire_one(session, args.url, args.endpoint, args.model, args.database_url, args.timeout)
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


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True, help="Deployed Cloud Run service base URL")
    p.add_argument("--mode", required=True, choices=["whoami", "burst", "sustained", "fairness"])
    p.add_argument("--endpoint", default="translate", choices=["translate", "execute", "summarize"])
    p.add_argument("--model", default=None, help="Override model - use your cheapest configured one")
    p.add_argument("--database-url", default=None, help="Override target DB preset (default: server's DATABASE_DEFAULT)")
    p.add_argument("--timeout", type=float, default=90.0, help="Per-request client timeout in seconds")
    p.add_argument("--n", type=int, default=6, help="[burst] number of simultaneous requests")
    p.add_argument("--interval", type=float, default=2.0, help="[sustained/fairness] seconds between each user's requests")
    p.add_argument("--duration", type=float, default=60.0, help="[sustained] seconds to keep firing")
    p.add_argument("--users", type=int, default=3, help="[fairness] number of independent simulated users")
    p.add_argument("--calls-per-user", type=int, default=15, help="[fairness] calls each simulated user makes")
    args = p.parse_args()

    {
        "whoami": cmd_whoami,
        "burst": cmd_burst,
        "sustained": cmd_sustained,
        "fairness": cmd_fairness,
    }[args.mode](args)


if __name__ == "__main__":
    main()