"""
chart_helpers.py

Chart/visualization eligibility and validation helpers, extracted out of
translate_routes.py (see that module's own docstring for what's left there
and llm_providers.py's docstring for the first piece extracted the same
way). Nothing here is new behavior relative to that extraction, but the
eligibility/response shape itself changed since: this used to allow AT MOST
ONE chartable result per summarization turn, across the whole call - a
single-connection multi-statement script or an "all databases" turn with
two independently-chartable result sets got ZERO charts, not one, because a
single unindexed "visualization" field in the response had no way to say
which result it was even describing. Both summarization prompts now key
their chart decisions by the same per-entry index they already use for
everything else (single-connection mode's "Query Result N" labels, "all
databases" mode's "[i]"/"per_database" indices - see summarize_routes.py's
_build_single_summary_prompt/_build_summary_prompt), so there's no ambiguity
left to guard against: every entry that independently qualifies gets its own
chart, exactly like every entry already gets its own summary paragraph.

These names are a single, self-contained concern: deciding, from the REAL
executed query results (never from the model's own say-so), which entries in
a summarization turn are even eligible to be charted at all
(_pick_chartable_results, using _entry_is_chartable/_column_looks_numeric/
_CHART_MIN_ROWS), describing that eligibility to the model in its prompt,
one line per qualifying entry (_describe_chartable_results), and validating
the model's own per-entry chart choices against the real data afterward
(_clean_visualizations, which validates each entry via _clean_visualization
- kept as the single-entry validator both because it's simpler to test in
isolation and because it's still exactly the right shape for "validate one
candidate visualization against one real result set", now just called once
per qualifying index instead of once per turn). See _pick_chartable_results'
docstring for the full "never trust the model's own judgment about
eligibility" rationale that ties all of these together.

Deliberately NOT included here: _build_single_summary_prompt/_build_summary_
prompt (call _describe_chartable_results, but are otherwise about rendering
the whole summarization prompt, not charting specifically) and
_clean_single_summary_response/_clean_summary_response (call
_clean_visualizations, but are about parsing/validating each call's whole
JSON envelope, "summary"/"per_database" included, not just the
"visualizations" piece of it) - all four remain in summarize_routes.py as
part of the two summarization pipelines, a separate extraction step of its
own.

translate_routes.py re-imports every name below back into its own
namespace, so they remain reachable as translate_routes.<name> - in
particular for every existing test's `app_env.translate_routes.<name>` /
`env.translate_routes.<name>` attribute access (_CHART_MIN_ROWS,
_pick_chartable_results, _clean_visualization, _clean_visualizations,
_column_looks_numeric, _extract_chart_link_captions are all referenced
this way in tests/server/test_translate_routes.py). No other module
imports any of these names directly today.

Entirely self-contained: pure functions over plain dicts/lists, no I/O, no
LLM calls, and no imports of their own beyond what's already in scope from
Python's builtins (plus `re`, used only by _extract_chart_link_captions
below).
"""

import re


_CHART_MIN_ROWS = 2


def _column_looks_numeric(rows, column, sample_size=200):
    """True when a solid majority of `column`'s own non-null sampled values
    (across up to `sample_size` of `rows`) are real numbers. Excludes bool
    (a Python bool is technically an int subclass, but a true/false column
    is categorical, not something to plot on a value axis) and excludes
    numeric-LOOKING strings on purpose - this app never asks the client to
    stringify numbers before sending results here (see
    _build_single_summary_prompt's docstring on `statement_results`' own
    shape: real JSON values, not pre-stringified), so a string value here
    is real text, not a number rendered as text.

    Used twice: to build the prompt's own "Chartable result sets" hints
    (see _describe_chartable_results) and, independently, to re-validate
    the model's actual y_columns choice against the real data rather than
    trusting its guess from the column name alone (e.g. a column named
    "id" is numeric but rarely a sensible y-axis choice on its own - still
    allowed here, since "sensible" is a judgment call left to the model,
    but a column named "amount" that's actually stored as text is not a
    valid choice at all, and this catches that).

    Empty/all-null sampled data is treated as NOT numeric (there's nothing
    to plot), not as a vacuous pass. A solid-majority (not unanimous)
    threshold tolerates the occasional stray null/outlier without
    disqualifying an otherwise-numeric column."""
    seen = 0
    numeric = 0
    for row in (rows or [])[:sample_size]:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None:
            continue
        seen += 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric += 1
    if seen == 0:
        return False
    return (numeric / seen) >= 0.9


def _entry_is_chartable(entry):
    """True when `entry` (one statement_results/database_results entry -
    see _pick_chartable_results' own docstring for the exact shape) is, on
    its own, eligible to be charted this turn:
      - It must have real tabular columns/rows (not a note, not an error).
      - It must have at least _CHART_MIN_ROWS rows - a single-row result
        has nothing to compare/trend, so a chart adds nothing over a
        table.
      - At least one of its columns must look numeric (_column_looks_
        numeric) - with no numeric column at all there is nothing to plot
        on a value axis.
    Pulled out of the old _pick_chartable_result so _pick_chartable_results
    (below) can apply these same three checks to EVERY entry in a results
    list independently, rather than to the list as a whole."""
    if not (isinstance(entry, dict) and not entry.get("error") and not entry.get("note") and entry.get("columns")):
        return False
    rows = entry.get("rows") or []
    if len(rows) < _CHART_MIN_ROWS:
        return False
    columns = entry.get("columns") or []
    return any(_column_looks_numeric(rows, col) for col in columns)


def _pick_chartable_results(entries):
    """Returns {index: entry} for every entry in `entries` that
    independently qualifies to be charted this turn (see
    _entry_is_chartable for the three eligibility checks) - keyed by
    0-based position in `entries`, the same indexing both summarization
    prompts already use elsewhere (single-connection mode's "Query Result
    N" labels are this index + 1; "all databases" mode's "[i]"/
    "per_database" keys are this index directly - see summarize_routes.py's
    _build_single_summary_prompt/_build_summary_prompt).

    Deliberately conservative in the SAME way the old single-result
    _pick_chartable_result always was - this decides eligibility server-
    side from the real executed results, never leaving it to the model's
    own judgment - but no longer requires exactly one qualifying entry in
    the whole list: a multi-statement script (or an "all databases" turn)
    with two or more independently-chartable result sets now offers a
    chart for EACH of them, since the response format keys every decision
    by this same index (see _describe_chartable_results/
    _clean_visualizations below) - there's no ambiguity left about which
    result a given chart decision belongs to, the same reasoning
    "per_database" already relies on for disambiguating several summary
    paragraphs in one response.

    Returns {} (never None) when nothing qualifies - every caller below
    already treats an empty dict and "nothing chartable" identically, so
    there's no separate None case to handle."""
    return {i: entry for i, entry in enumerate(entries or []) if _entry_is_chartable(entry)}


def _describe_chartable_results(chartable_by_index, label_for_index):
    """Renders every entry in `chartable_by_index` (see
    _pick_chartable_results) into the "Chartable result sets" prompt
    section both _SINGLE_SUMMARY_SYSTEM_INSTRUCTION's and _SUMMARY_SYSTEM_
    INSTRUCTION's own "visualizations" paragraphs reference - one line per
    qualifying entry, showing both a human-readable label
    (`label_for_index(index)` - e.g. "Query Result 2" for single-connection
    mode, "[1] Sales DB" for "all databases" mode) and the EXACT string key
    the model must use for that entry in its own "visualizations" response
    object, so it never has to guess or renumber anything - it can just
    copy the key shown here. Every column tagged (numeric) or (text) via
    _column_looks_numeric, same as the old single-entry _describe_
    chartable_columns always did.

    An empty `chartable_by_index` (nothing chartable ANYWHERE this turn)
    renders the explicit "none available" sentence instead, so the prompt
    never leaves the model to guess why "visualizations" must come back
    empty."""
    if not chartable_by_index:
        return "Chartable result sets: none available this turn - \"visualizations\" MUST be an empty object {}.\n"
    lines = [
        "Chartable result sets (ONLY these may appear in \"visualizations\", each keyed EXACTLY by the "
        "string shown in quotes - never invent, renumber, or omit the quotes from a key):"
    ]
    for index in sorted(chartable_by_index):
        entry = chartable_by_index[index]
        rows = entry.get("rows") or []
        columns = entry.get("columns") or []
        described = ", ".join(
            f"{col} ({'numeric' if _column_looks_numeric(rows, col) else 'text'})" for col in columns
        )
        lines.append(f"  \"{index}\" ({label_for_index(index)}) - columns: {described}")
    return "\n".join(lines) + "\n"


_CAPTION_MAX_CHARS = 100

# Matches the model's own inline "[<short phrase>](chart:<index>)" link -
# see both summarization prompts' "visualizations" paragraph - wherever it
# appears in a piece of summary/per-database/cross-database text. Group 1
# is the bracketed caption phrase; group 2 is everything after "chart:" up
# to the closing paren, re-validated as a real int index by
# _extract_chart_link_captions below rather than trusted as-is here (a
# regex alone can't tell "chart:1" from "chart:one" or "chart:1 "). The
# caption group is capped at _CAPTION_MAX_CHARS characters so a model that
# ignores the length guidance in prose can't make this pattern match
# across an entire paragraph by never closing its "]" until much later.
_CHART_LINK_PATTERN = re.compile(r"\[([^\[\]\n]{1,%d})\]\(chart:([^()\s]*)\)" % _CAPTION_MAX_CHARS)


def _clean_caption(raw_caption):
    """Validates one candidate caption string - the bracketed phrase from a
    single regex match of _CHART_LINK_PATTERN above, or (defensively) any
    other candidate a caller passes directly - independent of every other
    chart-shape field, so it's pulled out into its own tiny helper rather
    than inlined into its caller's body.

    Must be a non-empty (after stripping) string, and short: capped at
    _CAPTION_MAX_CHARS - a caption is meant to be a POINTER to a specific
    claim already made in the text (e.g. "Apple Pay's 20% completion
    rate"), not a restatement of it, so an overlong value here is treated
    as the model padding rather than pointing, and rejected the same as a
    missing/blank one. Returns the stripped string, or None on any
    failure - never raises."""
    if not isinstance(raw_caption, str):
        return None
    caption = raw_caption.strip()
    if not caption or len(caption) > _CAPTION_MAX_CHARS:
        return None
    return caption


def _extract_chart_link_captions(*texts):
    """Scans one or more pieces of real summary text - single-connection
    mode's "summary", or "all databases" mode's "per_database" paragraphs
    plus its optional "cross_database" one - for the model's own inline
    "[<short phrase>](chart:<index>)" link (see both summarization
    prompts' "visualizations" paragraph for the exact contract) and
    returns {int_index: caption} for every one found.

    THIS is now the sole source of a visualization's caption - there is no
    separate "caption" field in the model's JSON "visualizations" object
    any more (see _clean_visualization's own docstring for why: a caption
    that lives only in structured JSON can be filled in by the model
    without ever actually being integrated into the text a person reads,
    which is exactly the "chart and text stay disjoint" failure mode this
    whole feature exists to close). Extracting it from a real link found
    IN the text instead makes the connection unfakeable by construction -
    a caption only exists here because the exact same markup is also a
    live, clickable pointer sitting at that precise spot in the prose.

    A text argument that isn't a non-empty string (most commonly
    "cross_database", which is often None) is simply skipped, not an
    error - every caller passes its own full set of text fields as
    positional arguments regardless of which ones are populated this
    turn. When the SAME index is linked more than once across the given
    texts (an edge case - the model was never asked to do this), the LAST
    match encountered wins; single-connection mode only ever passes one
    text anyway, so this only matters for "all databases" mode's multiple
    per-database paragraphs.

    Returns {} (never None) when nothing matches - callers already treat
    an empty dict as "no captions available", the same as everywhere else
    in this module."""
    captions = {}
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for match in _CHART_LINK_PATTERN.finditer(text):
            try:
                index = int(match.group(2))
            except (TypeError, ValueError):
                continue
            caption = _clean_caption(match.group(1))
            if caption is not None:
                captions[index] = caption
    return captions


def _clean_visualization(raw, chartable_entry, caption):
    """Validates the model's own single "visualization" choice against the
    REAL executed result it's supposed to describe - `chartable_entry`, one
    value out of _pick_chartable_results(...)'s own returned dict, the
    exact same entry _describe_chartable_results rendered into the prompt
    for this same index. Returns a cleaned
      {"chart_type": "bar"|"line"|"scatter", "x_column": <str>,
       "y_columns": [<str>, ...], "series_column": <str>|None,
       "caption": <str>}
    or None (meaning: show a table, not a chart, for this one entry) -
    never raises. Called once per qualifying index by _clean_visualizations
    below, which is what actually parses the model's "visualizations"
    object; this function itself has no notion of indices at all, only
    "one candidate decision, one real result set to check it against" -
    the same shape it always validated back when a turn could only ever
    have one chartable entry in the first place.

    `chartable_entry` being None forces None regardless of what `raw`
    says, the same "never trust the model's own judgment about
    eligibility" posture _pick_chartable_results' own docstring describes.

    Otherwise: `raw` must be a dict; "chart_type" must be one of the three
    values the prompt actually offers (deliberately NOT "pie" - see this
    feature's own design notes on why that was left out); "x_column" must
    name one of `chartable_entry`'s real columns; "y_columns" must be a
    non-empty list of real column names, each independently re-verified
    NUMERIC via _column_looks_numeric against the real row data (not just
    "a real column name" - the model was already told which columns are
    numeric, but its choice is re-checked here rather than trusted, same
    as every other LLM output this app validates before acting on it) and
    deduplicated, excluding x_column itself; a "series_column" is kept
    only when it's also a real column, distinct from x_column. Any
    structural problem with "x_column" or an empty "y_columns" after
    filtering invalidates this one entry's visualization (returns None,
    falls back to a table for just that entry) rather than partially
    rendering something the model didn't actually intend.

    `caption` is REQUIRED, not optional - a missing one (None) invalidates
    this whole visualization exactly like a hallucinated x_column does,
    rather than falling back to some generic "Query N" placeholder. Unlike
    every other field validated here, `caption` is NOT read off `raw` -
    the caller (_clean_visualizations below) already extracted and cleaned
    it from the model's own inline "[...](chart:N)" link, found in the
    real "summary"/"per_database"/"cross_database" TEXT rather than in
    this JSON object at all (see _extract_chart_link_captions' own
    docstring for why). This is deliberate, not an oversight: this
    feature's entire point (per its own explicit design goal - see the
    "visualizations" paragraph in prompts/summary_single_connection.txt and
    prompts/summary_all_databases.txt) is that a chart only earns its place
    in the summary BECAUSE the text already points to it, in a form the
    user can actually click - a visualization the model never linked from
    the text has, by construction, failed to demonstrate that connection,
    so it gets the same treatment as any other structurally invalid
    decision: dropped back to a plain table for that one entry, never a
    reason to fail the whole response."""
    if chartable_entry is None or not isinstance(raw, dict) or caption is None:
        return None
    chart_type = raw.get("chart_type")
    if chart_type not in ("bar", "line", "scatter"):
        return None
    columns = chartable_entry.get("columns") or []
    rows = chartable_entry.get("rows") or []
    column_set = set(columns)

    x_column = raw.get("x_column")
    if not (isinstance(x_column, str) and x_column in column_set):
        return None

    raw_y_columns = raw.get("y_columns")
    if not isinstance(raw_y_columns, list):
        return None
    y_columns = []
    for y in raw_y_columns:
        if (
            isinstance(y, str) and y in column_set and y != x_column
            and y not in y_columns and _column_looks_numeric(rows, y)
        ):
            y_columns.append(y)
    if not y_columns:
        return None

    series_column = raw.get("series_column")
    if not (isinstance(series_column, str) and series_column in column_set and series_column != x_column):
        series_column = None

    return {
        "chart_type": chart_type, "x_column": x_column,
        "y_columns": y_columns, "series_column": series_column,
        "caption": caption,
    }


def _clean_visualizations(raw, chartable_by_index, *link_texts):
    """Validates the model's own "visualizations" object (see
    _describe_chartable_results/{_SINGLE_SUMMARY_SYSTEM_INSTRUCTION,
    _SUMMARY_SYSTEM_INSTRUCTION}'s own paragraph on it) against the REAL
    executed entries in `chartable_by_index` (see _pick_chartable_results) -
    the same dict _describe_chartable_results rendered into the prompt the
    model actually saw. Returns {index: <_clean_visualization's own shape>}
    - only for indices that are ALL of: a real key of `chartable_by_index`,
    linked from `link_texts` (see below), AND pass _clean_visualization's
    own per-entry validation.

    `link_texts` is the caller's own real "summary" text (single-connection
    mode - one positional argument), or "per_database"'s paragraphs plus
    "cross_database" (all-databases mode - several positional arguments,
    "cross_database" often None) - passed straight to
    _extract_chart_link_captions to find each index's own caption, which is
    ALSO this function's proof that the text actually links to that index
    at all (a chartable, "visualizations"-listed index with no inline link
    anywhere in `link_texts` gets no caption, and _clean_visualization then
    invalidates it for exactly that reason - see its own docstring).

    Every other key the model might have written (a hallucinated index, an
    index that was never chartable, or a value that doesn't survive
    _clean_visualization) is silently dropped rather than invalidating the
    whole response - the same "a table is always an acceptable, valid
    outcome" leniency _clean_visualization's own docstring already
    established for the single-chart case, now applied per-entry instead
    of once for the whole turn. Unlike "summary"/"per_database", a missing
    or malformed "visualizations" NEVER fails the caller's bounded retry -
    it isn't required content, just an optional bonus on top of it.

    `raw` not being a dict (missing, wrong type, or nothing was even
    chartable this turn) returns {} - no charts, not a parse failure."""
    if not isinstance(raw, dict) or not chartable_by_index:
        return {}
    link_captions = _extract_chart_link_captions(*link_texts)
    cleaned = {}
    for key, value in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index not in chartable_by_index:
            continue
        viz = _clean_visualization(value, chartable_by_index[index], link_captions.get(index))
        if viz is not None:
            cleaned[index] = viz
    return cleaned
