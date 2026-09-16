# Proposed Plan: NL→SQL Eval Tool for yDyL

## Goal

A standalone script (`eval_llms.py`, run from the repo root the same way `export_state.py` is today — no `scripts/` folder exists yet, so this follows the existing flat convention) that:

1. Feeds a curated set of natural-language prompts through the same translation pipeline the app uses today.
2. Runs each generated query through the real per-dialect `Backend` classes against a disposable test database.
3. Repeats that across a matrix of LLM providers/models (e.g. `gemini-2.5-flash`, `gemini-2.5-pro`, `claude-sonnet-5`, and any others you want to add).
4. Scores each result and produces a comparison report — pass rate, latency, token cost, and failure examples, broken down per model.

This is a research/design document, not code yet — nothing below has been built.

## Why build this custom, rather than adopt promptfoo/DeepEval/Braintrust/LangSmith

I looked at the current eval-framework landscape before proposing a custom build. Promptfoo is the most solo-developer-friendly (YAML+CLI, no Python required, and it natively supports "compare many models and prompts in one matrix" — the multi-model comparison you asked for is literally its headline feature). DeepEval is pytest-native but needs manual parametrization for multi-model runs. Braintrust and LangSmith are strong but are commercial, dashboard-first products aimed at teams, not a quick solo-dev script.

None of them, however, natively do the part that actually matters most for a NL→SQL app: running the generated SQL against a real database and comparing *result sets* using your own `Backend` classes and dialect quirks. That logic (`get_backend`, `Backend.execute`, `SqlExecutionError`, schema fetching, dialect prompt intros) already exists in your codebase and is well-tested — wrapping it in a promptfoo "custom provider" would end up being about as much code as just writing a plain Python script, minus the flexibility. So the recommendation is: **write a small custom Python harness that directly imports and reuses `server/translate_routes.py` and `server/backends/*` internals**, rather than adopting an external framework. If down the road you want promptfoo's nicer report UI for prompt-only (no-DB) comparisons, it's easy to add later as a second, complementary tool — it wouldn't replace the execution-accuracy core.

Sources: [Text-to-SQL Evaluation Techniques](https://harshchandekar10.medium.com/text-to-sql-evaluation-techniques-a-comprehensive-guide-4b243c82ab88) · [Why 90% Accuracy in Text-to-SQL is 100% Useless](https://towardsdatascience.com/why-90-accuracy-in-text-to-sql-is-100-useless/) · [5 Metrics to Test Text-to-SQL Accuracy (Querio)](https://querio.ai/articles/metrics-test-text-to-sql-accuracy) · [Text-to-SQL Leaderboard & Evaluation Metrics Guide](https://promethium.ai/guides/text-to-sql-evaluation-benchmarks-metrics/) · [LLM Evaluation Framework Benchmark 2026 (aiml.qa)](https://aiml.qa/llm-evaluation-framework-benchmark-2026/) · [Best Promptfoo alternatives 2026 (Braintrust)](https://www.braintrust.dev/articles/best-promptfoo-alternatives-2026)

## How this plugs into the existing code (confirmed by reading the source)

- **LLM calls, already factored into reusable one-shot helpers**: `translate_routes.py` has `_call_gemini(client, model, contents, system_instruction)` and `_call_claude(client, model, messages, system_instruction)`, each returning `(text, usage_dict)`. These bypass the NDJSON streaming layer entirely (that layer only exists for `/api/translate`'s retry-progress UI) — the eval script imports and calls them directly, no HTTP/Flask involved.
- **Model/provider selection**: today driven by `LLM_PROVIDER` (`"gemini"` default, `"claude"` the other option), `DEFAULT_MODEL` (`gemini-2.5-flash`), `PRESET_MODELS` (`["gemini-2.5-flash", "gemini-2.5-pro"]`), `DEFAULT_CLAUDE_MODEL` (`claude-sonnet-5`). The eval script's model matrix is just a list of `(provider, model_name)` pairs — no server-side change needed, since `_call_gemini`/`_call_claude` already take the model name as a parameter.
- **Schema + dialect context**: `db.get_database_schema(conn_str, user_id)` and the `_DIALECT_PROMPT_INTROS` dict (keyed by `Backend.dialect_name`) are both plain functions/data, callable outside a live request.
- **Execution**: `get_backend(descriptor)` → `backend.connect()` → `backend.execute(conn, sql)` → `list[{"statement","columns","rows","rowCount"}]`, with `SqlExecutionError` raised (carrying partial `results`) on a mid-script failure. The eval script reuses this exact path so "did it run" is measured identically to production, not reimplemented.

## Test database strategy

The existing test suite fakes DB drivers at the cursor-response level (`FakePgCursor`, etc.) — good for unit tests, useless for eval, since eval needs *real* result sets to compare against. Proposal: stand up one small, disposable, seeded Postgres database (a `docker run postgres` or a local install is enough) with a fixed schema + fixture data — start with just this one dialect. Multi-dialect coverage (BigQuery, Snowflake, etc.) can be phase 2, since each needs its own live disposable instance and most of your natural-language grading value comes from one representative dialect first.

Because some golden prompts are intentionally DML/DDL ("create a table, insert data, drop it"), the harness resets the test DB to a known snapshot before each prompt (e.g. `pg_restore` from a fixture dump, or wrap each prompt's execution in a transaction that's rolled back afterward when the SQL permits it).

## Golden dataset format

A YAML or JSON file, 30–50 hand-written prompts to start (per the "5 Metrics" research above — a small, curated set beats a large generic benchmark like Spider/WikiSQL, which "look nothing like a real production warehouse"). Each entry:

```yaml
- id: join_basic_1
  category: join
  prompt: "Show me the 10 most recent orders with the customer's name and email."
  expected_sql: "SELECT o.id, c.name, c.email FROM orders o JOIN customers c ON o.customer_id = c.id ORDER BY o.created_at DESC LIMIT 10;"
  grading: result_set     # result_set | exact_match | no_sql_expected | manual
- id: conversational_1
  category: no_sql
  prompt: "What's the weather like today?"
  grading: no_sql_expected
- id: ddl_roundtrip_1
  category: ddl
  prompt: "Create a table called demo with 2 columns, insert one row, then drop it."
  grading: manual   # multi-statement side-effect scripts are hard to auto-grade; flag for human review
```

Categories to cover, based on what the app already exercises in its own e2e suite: simple SELECT, filters, joins, aggregation, natural-language ambiguity (`*** NO SQL ***` responses), DML, DDL/multi-statement scripts, and at least a few prompts per SQL dialect once phase 2 adds more backends.

## Scoring

Combine metrics rather than picking one, per the research above, layered cheapest-to-most-expensive:

1. **Did it execute at all?** (via the real `Backend.execute()` / `SqlExecutionError` path) — the cheapest, always-computed signal.
2. **Execution accuracy** — for `grading: result_set` entries, run the model's SQL and a maintainer-authored reference SQL against the same freshly-reset DB, compare result sets (order-insensitive by default, since most natural-language prompts don't imply strict ordering unless asked for).
3. **Exact/normalized-SQL match** — secondary signal only (whitespace/case-normalized string compare against `expected_sql`), useful for catching regressions between runs of the *same* model, not for cross-model comparison (too brittle — multiple correct SQL phrasings exist).
4. **`no_sql_expected` entries** — pass if the model's response matches the `*** NO SQL ***` sentinel the app already uses for conversational replies.
5. **`manual` entries** (multi-statement DDL scripts, anything too side-effecting to auto-diff) — recorded but flagged for human review rather than auto-scored.

Also captured per call, straight from `_call_gemini`/`_call_claude`'s existing `usage_dict` return value: token counts, latency, and (via a small hardcoded per-model $/token table you maintain) an estimated cost — so the report can show "model X got 90% right but cost 4x more per query."

## Report output

Two outputs per run:
- A JSON results file (one row per prompt × model) for programmatic diffing between runs.
- A human-readable comparison table, either printed to the terminal or written as a static HTML report (per-model pass rate by category, avg latency, avg cost, and a list of failures with the prompt/expected/actual SQL side by side).

If you find yourself running this regularly and wanting to track pass-rate trends over time (rather than just one-off comparisons), that's a good candidate for a small persisted dashboard later — but the first version should just be a report you generate on demand.

## Safety notes

- Only ever run against a disposable/local test database — never against a real preset or a user's custom connection. The script should refuse to run if the configured target looks like a production URL (reuse the same `descriptor`/connection-string shape the app already validates).
- Reuse the app's existing key-rotation/retry constants (`MAX_TRANSLATION_ATTEMPTS`, `TRANSLATION_RETRY_DELAY_SECONDS`) so a flaky Gemini/Claude call doesn't get scored as a model failure.
- Set temperature to 0 (or whatever the app's default already is) for determinism across repeated runs of the same model.

## Suggested build order

1. **Phase 1** — single dialect (Postgres), one seeded test DB, 30–50 prompts, execution-accuracy + exact-match scoring, terminal report. Enough to compare `gemini-2.5-flash` vs `gemini-2.5-pro` vs `claude-sonnet-5` meaningfully.
2. **Phase 2** — add token/latency/cost tracking and the static HTML report.
3. **Phase 3** — extend the golden dataset and test DBs to additional dialects; consider wiring this into CI as a lightweight regression gate (e.g. "pass rate must not drop below X%") rather than just an ad hoc comparison tool.

## Open questions for you

- Which dialect should Phase 1 target — Postgres (most common/simplest to stand up locally) is my default assumption, but tell me if you'd rather start elsewhere.
- Do you want me to also draft the actual 30–50 prompt golden dataset, or would you rather author those yourself since they should reflect the kinds of questions your real users actually ask?
- Any other LLMs/providers beyond Gemini and Claude you want in the comparison matrix (the app is currently only wired for those two)?
