# Datalect: getting ready for a big invite wave

Grounded in your actual Cloud Run setup (`gcp_deploy.sh`, `Dockerfile`, `server/server.py`, `env.yaml`) rather than generic advice. Three sections: what's actually risky about *this* app, how to configure Cloud Run, and how to test before you send the invites.

## The real risk isn't Cloud Run running out of instances

Cloud Run itself autoscales fine and is very unlikely to be your bottleneck. Two things upstream of it are much more likely to be your "success disaster," and neither shows up if you only think about Cloud Run flags:

**1. The production process is Flask's development server, not a real WSGI server.** `server/server.py` calls `app.run(..., threaded=True)`, and `Dockerfile` runs `python server/server.py` directly — there's no gunicorn/uWSGI/gevent in front of it. `threaded=True` means Werkzeug spins up a Python thread per request, but the dev server was never built or tested to hold up under real concurrent production load — Flask's own docs say as much. With Cloud Run's default concurrency of up to 80 requests routed to a single container instance, you could easily hand this server far more concurrent threads than it can healthily handle, especially since each request also does blocking network I/O (LLM calls, DB calls). This is worth fixing before you touch any Cloud Run flag, because a `--concurrency` setting only controls how many requests Cloud Run *hands* to the container — it does nothing to make the process inside handle them well.

**2. One user's question can fan out to 15 simultaneous DB connections, with no pooling.** In "all databases" mode, `execute_routes.py`/`db.py` open a fresh, unpooled connection per database via a `ThreadPoolExecutor`, up to `MAX_IN_SCOPE_CONNECTIONS=15`. That's 15 *new* connections for one user's one question. If your `gcp_deploy.sh` MySQL instance (`free-trial-first-project`) is a free-tier Cloud SQL tier, it likely has a low `max_connections` ceiling — a burst of even 5-10 concurrent "all databases" questions could exhaust it and start throwing connection errors for everyone, including totally unrelated single-database questions.

A secondary, quieter risk: `env.yaml` has your real LLM provider keys (Anthropic, OpenAI, a 6-key Gemini pool) sitting in plaintext, with no rate-limiting library anywhere in `requirements.txt`. The Gemini key-pool rotation gives you *some* headroom, but nothing stops a traffic spike from burning through API quota or racking up real dollar cost quickly. This isn't about invite-day risk specifically, but it's the same "success disaster" shape and worth 15 minutes: move `env.yaml`'s secrets into Secret Manager and reference them with `--set-secrets` instead of `--set-env-vars`, so a compromised config file (or an accidental commit) doesn't leak live keys.

None of this means don't proceed — it means the highest-leverage fixes are upstream of Cloud Run's autoscaler, and cheap to make.

## Cloud Run configuration

Your `gcp_deploy.sh` currently sets none of the scaling flags, so everything is running on defaults. Concretely, add these to your `gcloud run deploy`:

**`--concurrency`**: Defaults to 80 requests per instance if unset. That's too high for this app as it stands — 80 concurrent requests each potentially opening up to 15 DB connections is exactly the flood scenario you're worried about. Set it explicitly and low, e.g. `--concurrency=8` to start. This forces Cloud Run to spin up more instances sooner under load instead of stacking dozens of requests onto one instance's thread pool, and it directly bounds the worst case (instances × concurrency × 15 = max possible simultaneous DB connections) to something you can reason about against your DB's connection limit.

**`--min-instances`**: Defaults to 0 (scale to zero). With min-instances at 0, the *first* wave of your invited users all hit a cold start together, and Cloud Run's on-demand scaler queues incoming requests for up to 10 seconds (or 3.5× the predicted cold-start time, whichever is longer) before spinning up a new instance — so a synchronized launch (e.g., you post the link at 6pm) can look like a pile of slow/stuck requests even though nothing is actually broken. Set `--min-instances=1` or `2` so there's always a warm instance ready, especially for the specific evening/window you're inviting people.

**`--max-instances`**: Also unset today, meaning Cloud Run falls back to whatever your project's regional CPU/memory quota allows — that's a much bigger number than you probably want as your first real ceiling, and it's really a cost/DB-load cap dressed up as a Cloud Run setting. Work backward from your DB's actual `max_connections` (`SHOW VARIABLES LIKE 'max_connections'` on the MySQL instance — free-tier Cloud SQL tiers are often well under 100) and pick `--concurrency` and `--max-instances` so `instances × concurrency × 15` (the worst case, if every in-flight request happens to be an "all databases" question) stays comfortably under that number. With `--concurrency=8` and a `max_connections` of, say, 50, that points to `--max-instances` in the low single digits until you've confirmed with real testing that the DB can take more — this is the single most important number to get right before invite day.

**`--cpu` / `--memory`**: Also unset (defaulting to a small instance). Given the per-request thread pool fan-out plus blocking I/O, bumping to `--cpu=2 --memory=1Gi` (or more) gives the threaded dev server actual room to interleave work instead of contending on a single vCPU.

**`--timeout`**: Unset, so it's at the platform default (300s), which is probably fine today, but worth setting explicitly (e.g. `--timeout=300`) and checking it comfortably covers your worst case: `MAX_TRANSLATION_ATTEMPTS=3` × `TRANSLATION_TIMEOUT_SECONDS=60` (180s worst case) plus `SQL_EXECUTE_TIMEOUT_SECONDS=60` could approach 240s for one unlucky request.

**`--cpu-boost`**: Enabled by default on new services now, so you likely don't need to touch this — just confirm it's on, since it shortens the cold starts that `--min-instances` doesn't fully eliminate (e.g. during scale-up beyond your warm instances).

Sources: [gcloud run deploy reference](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy), [Cloud Run instance autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling), [Cloud Run max instances](https://docs.cloud.google.com/run/docs/configuring/max-instances-limits), [Cloud Run concurrency](https://docs.cloud.google.com/run/docs/about-concurrency).

## Admission control — simple options, roughly in order of effort

**Cap `--max-instances` (above).** This is already admission control, in the sense that it's a hard backstop: once every instance is saturated at its configured concurrency, Cloud Run itself starts queuing/rejecting rather than scaling infinitely. Free, no code.

**Stagger the invite.** The cheapest, zero-code mitigation for the exact scenario you described ("too many people show up at once") is to not let that happen — send the invite to a subset first, or across a couple of hours rather than all at once. Combined with `--min-instances`, this alone removes most of the "everyone cold-starts simultaneously" risk.

**An in-process concurrency guard on the expensive routes.** A small `threading.Semaphore` (or a simple counter) around `/api/translate` and `/api/execute` that returns `503` with a `Retry-After` header once N requests are already in flight per instance. This is the most direct match for "admission control" in your own words — it stops the app from *accepting* work it can't actually service (DB connections, LLM calls) once it's already at capacity, rather than accepting everything and degrading. A few lines of code, no new infrastructure, and it fails safely — the user gets "try again in a moment" instead of a hung request or a DB connection error.

**Flask-Limiter for per-IP/per-session rate limiting.** Nothing in `requirements.txt` does this today. Even a generous limit (e.g., 20 translate/execute calls per minute per session) blunts a single runaway script or an overeager user without affecting normal usage, and it's a well-trodden library rather than something to build yourself.

**Cloud Armor at the edge.** Heavier — it requires putting Cloud Run behind an external HTTPS load balancer with a serverless NEG, which is infrastructure you may not have today. Worth it if you expect actual abusive/bot traffic, probably overkill just for a friendly group you're inviting yourself. I'd treat this as a later step, not a first-pass one.

**A waiting-room/queue UX.** The heaviest option — only worth it if, after load testing, you learn your real ceiling is well below the group size you're inviting and you can't raise it in time. I wouldn't build this preemptively.

For a first pass I'd do: stagger the invite + `--min-instances`/`--max-instances` + the in-process semaphore on `/api/translate` and `/api/execute`. That combination is cheap and directly addresses the specific failure mode you're worried about (a burst of simultaneous heavy requests).

## Load/stress testing plan, since none has been done yet

Do this roughly in this order — each layer isolates a different bottleneck, so you'll know *which* fix mattered rather than changing five things and hoping.

**1. Component baseline.** Time one LLM call and one DB query in isolation (outside the app, e.g. a small script) so you know the floor cost per request before any app-level overhead. This tells you the theoretical best case.

**2. App-layer load test with LLM/DB calls stubbed out.** Your e2e tests already mock `/api/summarize-result`-style calls — reuse that pattern to stub the LLM and DB layers, then hammer the Flask app itself with a tool like `hey`, `k6`, or `locust`. This isolates whether the Werkzeug dev server (item 1 above) is your bottleneck *before* any real backend is involved. If it falls over well before you expect real users to, that confirms the WSGI-server gap is worth fixing first.

**3. Realistic mixed-load test against a staging deployment.** Point a separate Cloud Run service (same code, same `gcp_deploy.sh` flags you plan to use for real) at a disposable database — a scratch Cloud SQL instance or even a local Postgres/MySQL you don't mind stressing — and a real LLM key with a low, capped budget so a runaway test can't rack up real cost. Simulate realistic sessions: translate → execute → maybe a follow-up turn, not just hammering one endpoint.

**4. Ramp test.** Gradually increase concurrent virtual users (not a sudden step) and watch three things simultaneously: Cloud Run's own instance count (does it scale when you expect, given your `--concurrency`/`--max-instances`?), DB connection errors or slowdowns, and LLM key-pool rate-limit responses. This is how you find your *actual* ceiling rather than guessing at flag values.

**5. Soak test.** Sustained moderate load (not peak) for 30-60+ minutes. This is specifically to catch connection leaks — since every DB connection here is opened per-request with no pooling, a bug in an error path that skips closing a connection would show up as slow degradation over time, not an immediate failure.

**6. Deliberately break it.** Push load past whatever ceiling you found in step 4, and confirm the failure mode is graceful — clear 503s and error messages, not crashed instances, corrupted chat history, or silent data loss. This is really testing the admission-control changes above, not just raw capacity.

Do steps 2-4 once before making any of the changes above (to know your current baseline) and again after (to confirm the changes actually helped) — otherwise you won't know which lever mattered.

## Summary of concrete next steps

Put a real WSGI server (gunicorn is the standard choice) in front of the Flask app instead of running the dev server directly — this one change plausibly matters more than any Cloud Run flag. Work out your DB's real `max_connections` and pick `--concurrency`/`--max-instances` backward from that number, alongside `--min-instances=1-2` for cold-start protection. Add a simple in-process concurrency limit on `/api/translate` and `/api/execute` so the app fails fast and friendly instead of falling over. Stagger the invite rather than sending it to everyone at once. Move the plaintext secrets in `env.yaml` into Secret Manager. Then load-test in the layered order above, using your own e2e mocking conventions for the cheap early layers and a disposable staging DB + capped LLM key for the realistic ones.