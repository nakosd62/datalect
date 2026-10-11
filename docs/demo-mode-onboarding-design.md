# Live Demo Mode — design + implementation notes

Status: **implemented.** This is a design doc, now also recording what was actually built, two bugs found and fixed after a real run, and the pacing/visibility controls added on request.

## Goal

A new onboarding flow, separate from and never touching the existing "quick tour"
(`getTourSteps()`/`startGuidedTour()` in `webClient/client.js`), that drives the
**real app** — real dataset selection, real schema viewer, a real live LLM
translate call, a real SQL execution against a real Postgres dataset, and the
real post-execution summary/chart pipeline — instead of a scripted, static
walkthrough. Triggers automatically for new/anonymous visitors and is also
manually replayable.

## How the real result/chart mechanism actually works (investigated, not guessed)

This grounds the "click to see a real, chart-capable result" step. On every
single-connection turn, after SQL execution, the client automatically calls
`/api/summarize-result` (`requestSingleModeResultsSummary()` in `client.js`).
The response is prepended as a new, active **"Response" tab**
(`prependSingleModeSummaryTab()`) ahead of the real query-result tab(s).

That Response text is written by the LLM under
`server/prompts/summary_single_connection.txt` /
`_SINGLE_SUMMARY_SYSTEM_INSTRUCTION`, which can embed a special inline link
`[short phrase](chart:N)` (or `(result:N)`) *inline in its own prose*, at the
exact point it makes the claim a chart would illustrate. The client turns that
into a clickable element (`applyInlineChartLinks()` → `<button class="summary-chart-inline-link" data-view-chart-trigger="N">`),
wired to `jumpToChartableResultTab(N)`, which switches the active tab to the
real query-result tab at validated index `N` and forces it into **Chart view**.

Server-side, `chart_helpers.py`'s `_pick_chartable_results` independently
decides (≥2 rows, a genuinely numeric value column) whether a result is
*eligible* to be charted at all — the model only gets to choose for eligible
results, and only a chart the model actually links inline in its summary is
ever shown. This is not something demo-mode needs to fake or special-case:
it is the existing, always-on behavior for any single-connection question.

The demo question ("Which 10 countries had the highest GDP per capita in
2020?" against `postgres-world-stats`, real tables `countries`/
`wdi_indicators`/`wdi_observations`, indicator code `NY.GDP.PCAP.CD`) comfortably
clears the chart-eligibility bar (10 rows, one numeric column) — but whether
the model actually writes the inline link is still a live, real judgment
call each run, not a guarantee. The implementation handles this with a real,
honest fallback: if the rendered Response text has no inline chart-link
element when this step runs, it calls `jumpToChartableResultTab()` directly
(the exact same real function a click would call) with no index, which is
its own documented "first chartable tab" behavior — never a faked chart.

## No fallback SQL on a live-call failure (explicit decision)

Earlier drafts of this design proposed a silent fallback to a pre-validated
SQL string if the live translate call errored. **Dropped per explicit
instruction**: the live demo's single question still goes through three real
LLM calls end to end (triage, SQL generation, summarization), so protecting
against just one of them failing isn't meaningful protection — a failure
anywhere in that chain is a real failure either way. The implementation does
not catch or route around a translate/execute error: `runLiveDemoStep()`'s
`'run'` case calls the real `translatePrompt()` directly and lets whatever it
does stand, including its own real error UI (e.g. the "Translation Error"
tab a real user would see). `runLiveDemo()`'s own try/catch exists only to
stop the script early and hide the status strip if a step throws — it never
substitutes a canned query or retries. One clarification found while tuning
the e2e timeouts for the `start` step below: `translatePrompt()` itself
catches its own fetch/response errors internally and resolves normally
after rendering its error UI — it does not throw. So a live translate error
does not actually cut the script short via that catch; the remaining steps
(`wait_for_results`, `click_chart_link`, `end`) still run and hold for
their own `holdMs` afterwards, same as a successful run, just with no
result to show. The try/catch is a safety net for a genuinely unexpected
exception elsewhere in a step, not the normal path for a translate error.

## Bug found on first real run: the Schema Viewer appeared to "not be included"

A real run surfaced that the dataset picker and Schema Viewer steps weren't
visibly happening. Two compounding causes, both fixed:

1. **A real bug, not just speed.** `open_schema_viewer`'s readiness check
   used to wait for "the entry list to have any children at all" before
   letting `browse_schema` run. But the Schema Viewer always renders its
   pinned Overview/ER-diagram rows immediately on open, before the real
   schema fetch finishes — so that wait resolved instantly regardless of
   whether the real "Tables" group (built by `SCHEMA_VIEWER_GROUPS`, which
   drops a group entirely while it has zero items) had loaded yet.
   `browse_schema` then ran before that group existed, found no
   `[data-group="tables"]` header, and silently did nothing — then
   `close_schema_viewer` closed it right after. It opened and closed with
   nothing populated, so it looked like it wasn't there at all. **Fixed**:
   the wait now specifically polls for the real `[data-group="tables"]`
   header, with a longer timeout (schema introspection + a cached-overview
   fetch can take a few seconds).
2. **No deliberate pacing.** Even the parts that worked correctly (opening
   the picker, selecting the preset) had no built-in pause, and since
   `postgres-world-stats` is the app's own default connection, selecting it
   is usually a no-op that completes in under a tick — too fast for a human
   to register. **Fixed** by the generic `holdMs` mechanism below.

## Pacing: the generic `holdMs` field (hand-editable, per step)

Every step in `demo-script.json` may carry an optional `"holdMs"`: after
that step's own action finishes, `runLiveDemoStep()` pauses for that many
milliseconds before the next step begins. This is the single knob behind
every pacing concern raised so far:

- **The intro banner, before anything else happens** — carried by the new
  `start` step's own `holdMs` (see "Step sequence" below). Its action is a
  genuine no-op — it exists purely to show an explanatory caption and hold
  it on screen before `open_connection_picker` starts actually driving the
  app.
- **Dataset picker / Schema Viewer visibility** — `open_connection_picker`,
  `select_connection`, `open_schema_viewer`, and `browse_schema` each carry
  a `holdMs` so their real, on-screen state changes are actually visible,
  not just technically correct.
- **Time between the summary appearing and the chart click** — carried by
  the `wait_for_results` step's own `holdMs`. That step's action is a
  genuine no-op (the real summary is already fully rendered by the time
  it runs), so its only job is pacing: the pause happens *before*
  `click_chart_link` runs, while the reader is still looking at the
  summary — not after, which is why this lives on `wait_for_results`
  rather than on `click_chart_link` itself.
- **How long the closing message stays up** — carried by the `end` step's
  own `holdMs`. `end` is the last step in the script, so its `holdMs` is
  the only thing standing between showing that caption and
  `runLiveDemo()`'s `finally` block hiding the strip immediately after.

Current values in `demo-script.json` (freely tunable, and already turned up
once from the original, much shorter defaults):

```json
{
  "id": "default-live-demo",
  "connectionId": "postgres-world-stats",
  "question": "Which 10 countries had the highest GDP per capita in 2020?",
  "schemaHighlights": ["countries", "wdi_indicators"],
  "steps": [
    { "type": "start", "caption": "This is a live walkthrough against a real database — not a recording. We'll pick a dataset, explore its schema, and ask it a real question.", "holdMs": 6000 },
    { "type": "open_connection_picker", "caption": "First, let's pick a dataset to work with.", "holdMs": 8000 },
    { "type": "select_connection", "caption": "We'll use World Stats — real GDP, population, and development data for every country.", "holdMs": 2000 },
    { "type": "open_schema_viewer", "caption": "Here's what's actually inside it.", "holdMs": 8000 },
    { "type": "browse_schema", "holdMs": 2000 },
    { "type": "close_schema_viewer" },
    { "type": "enter_prompt", "typingSpeedMs": 100 },
    { "type": "run", "caption": "Datalect translates this into real SQL and runs it against the live database." },
    { "type": "wait_for_results", "holdMs": 4000 },
    { "type": "click_chart_link", "caption": "And it tells you what's actually interesting about the result." },
    { "type": "end", "caption": "That was a live run against a real database. Try a question of your own!", "holdMs": 6000 }
  ]
}
```

(`browse_schema` no longer carries its own caption, per a later hand-edit
to this file — `open_schema_viewer`'s caption, "Here's what's actually
inside it.", simply stays on screen through it instead.)

`typingSpeedMs` on `enter_prompt` is the other existing pacing knob
(milliseconds per character of the typing animation) — unrelated to
`holdMs`, but tunable the same way.

## Visibility: the status strip is a full-width top banner, not a corner pill

The status strip (`#liveDemoStrip`) started as a small bottom-center pill,
similar in weight to a toast notification. Per explicit request for a lot
more visibility, it was redesigned into a bold, full-width banner at the
top, using the app's own brand color pair — `background: var(--primary)` /
`color: var(--on-primary)` — with a larger, bolder caption
(`font-size: 1.1rem; font-weight: 600`).

The banner went through two layout approaches. It was first built as
`position: fixed; top/left/right: 0`, deliberately allowed to sit on top of
(and partially cover) `.crbot-header` — confirmed safe at the time since
`.crbot-header` has no `position: fixed`/`sticky` or `z-index` of its own.
Per a follow-up request, it was changed again so the banner **pushes the
app down instead of covering it**: `.live-demo-strip` is no longer
`position: fixed` at all. It's laid out in normal document flow as a flex
child of `<body>` (`body` is itself `display: flex; flex-direction:
column;` — see `style.css`'s "html, body" / "body" rules), and `order: -1`
makes it render before `.app-container` regardless of its actual position
in `index.html` (it still lives near the end of `<body>`, grouped with the
rest of this feature's markup, after the modals and `#tourOverlay` —
every one of which is its own `position: fixed` overlay and so doesn't
participate in body's normal flow at all; once those are excluded,
`#liveDemoStrip` and `.app-container` are body's only two flex children).
The other half of this: `.app-container` was changed from a fixed
`height: 100dvh` to `flex: 1; min-height: 0;`, so it simply shrinks to
fill whatever space is left under the banner instead of overflowing the
locked `html`/`body` `100dvh` and getting clipped by `body`'s own
`overflow: hidden`. No JavaScript was needed for this — it's pure flexbox,
and nothing in `client.js` references `.app-container`'s dimensions.

A second bug surfaced by the push-down change: once live, the banner
disappeared behind any open modal (`#configModal`, `#schemaViewerModal`,
etc.) — exactly the steps the live demo itself drives (`open_connection_picker`,
`open_schema_viewer`) — because `.modal-overlay` is `position: fixed`,
full-viewport, with its own `backdrop-filter: blur(8px)` scrim and
`z-index: 1000`, which painted over the banner's now-unset z-index.
**Fixed** by giving `.live-demo-strip` `z-index: 2100` — comfortably above
both `.modal-overlay`'s `1000` and `.tour-overlay`'s `2000`. This works
without also needing `position: relative` on the strip: per the flexbox
spec, `z-index` applies to flex items (which `#liveDemoStrip` is, as a
direct flex child of `<body>`) even while they're at their default
`position: static`. The banner now paints on top of a modal's blurred
scrim instead of being hidden under it, staying visible and legible
through the rest of the script.

One CSS pitfall hit and fixed along the way: the Cancel button's border was
initially written as a `var(--on-primary)`-derived `rgba()` tint, but no
`--on-primary-rgb` token exists anywhere in `style.css` to key off, and a
hardcoded fallback would have been wrong in one theme or the other (dark
theme's `--on-primary` is a near-black `#080b11`; light theme's flips to
`#ffffff`). Fixed to a fixed, theme-independent translucent white border
(`rgba(255, 255, 255, 0.4)`), justified by a code comment: `--primary` is a
medium-dark green in both the dark and light themes, so a white-based
border reads consistently against it either way.

## Trigger behavior (as implemented)

- Independent localStorage flag `ydylLiveDemoSeen` (`hasSeenLiveDemo()` /
  `markLiveDemoSeen()`), entirely separate from the tour's own
  `ydylOnboardingSeen`. The quick tour's gate and code are untouched.
- Auto-play sequencing: `finishTour()` now also checks a new
  `tourAutoStartedPendingLiveDemo` flag, set only by the real auto-start
  branch at page init (never by a manual "Replay guided tour" click) — a
  brand-new visitor sees the tour first, then the live demo auto-starts the
  moment it finishes or is skipped. A visitor who's already seen the tour
  but not the live demo (an existing user the first time this shipped, or
  anyone back later) gets the live demo on its own, 1.5s after load.
- Manual replay: a second help-modal link, `#replayLiveDemoBtn` ("Replay
  live demo"), right next to the existing `#replayTourBtn` in `help.html`,
  wired in `wireHelpModalLinks()`. Always runs on click, regardless of
  `hasSeenLiveDemo()`.
- A **Cancel** button on the status strip (`#liveDemoStripCancelBtn`, class
  `.live-demo-strip-cancel`, handler `cancelLiveDemo()`) sets an abort flag
  checked between every step and hides the strip immediately. Renamed from
  an original "Skip" per explicit request, since "Cancel" better describes
  stopping a live, in-progress run rather than skipping ahead in a scripted
  one.

## Step sequence (implemented in `runLiveDemoStep()`)

1. `start` — a no-op action, added on request: just shows its own intro
   caption and holds for its own `holdMs` before anything else happens, so
   a viewer knows what they're about to watch before the real app starts
   moving. An explicit `break` in the switch (not a fall-through to the
   next case, unlike `end`/default below), since the next case,
   `open_connection_picker`, is a real action.
2. `open_connection_picker` — clicks the real `#configTriggerBadge`.
3. `select_connection` — calls the real `switchActiveConnectionToPreset(connectionId)`
   (the same function a real suggested-question click already uses
   elsewhere in the app), then closes the config modal.
4. `open_schema_viewer` — clicks the real `#datasetSchemaViewerBtn`, then
   waits for the real `[data-group="tables"]` group header to exist (see
   the bugfix above).
5. `browse_schema` — expands the real Tables group and clicks the real rows
   for `schemaHighlights` (`countries`, `wdi_indicators`).
6. `close_schema_viewer` — calls the real `closeSchemaViewer()`.
7. `enter_prompt` — types the question into `#aiPrompt` character by
   character (animated, not pasted), dispatching real `input` events.
8. `run` — calls the real `translatePrompt()` directly (live translate →
   execute → the automatic summarize call, all real; errors are left to
   show, per the decision above and its clarification).
9. `wait_for_results` — a no-op action; its `holdMs` is the configurable
   pause before the chart click (see "Pacing" above).
10. `click_chart_link` — clicks the real inline chart link if the model
    wrote one this run, else calls `jumpToChartableResultTab()` directly as
    the real, non-faked fallback.
11. `end` — closing caption, held on screen for its own `holdMs`; the demo
    is marked seen the moment it starts (mirrors the tour's own
    `markOnboardingSeen()` precedent), not only on a particular exit path.

## Runner mechanism

Lives inside `webClient/client.js` itself, in a new "6.5. LIVE DEMO MODE"
section (right after the Help-button onboarding section, before "7. HISTORY
MODAL") — not a separate bundle, since it needs direct access to existing
internal functions/state (`switchActiveConnectionToPreset`, `closeSchemaViewer`,
`translatePrompt`, `jumpToChartableResultTab`, `chatStore`-adjacent globals
like `resultsBody`/`aiPrompt`). A small, non-blocking status strip
(`#liveDemoStrip`, new CSS in `style.css`) shows the current step's caption
— deliberately not a blocking modal/spotlight like the tour, since this
drives real network calls with real, variable latency. The strip is now a
bold, full-width banner that pushes `.app-container` down rather than
overlaying it (see "Visibility" above); it started as a small
bottom-center pill, then a `position: fixed` top overlay, before settling
on the current push-down flex layout.

## Files touched

- `webClient/demo-script.json` — new, the hand-editable script.
- `webClient/client.js` — new "6.5. LIVE DEMO MODE" section; `finishTour()`
  chains into it; `wireHelpModalLinks()` wires the new replay link; the
  init block's auto-start branch sets `tourAutoStartedPendingLiveDemo` /
  auto-triggers for a returning visitor; the generic `holdMs` pacing
  mechanism; the corrected schema-viewer readiness wait; the Cancel button
  (`liveDemoStripCancelBtn`/`cancelLiveDemo()`, renamed from Skip); a new
  `'start'` case in `runLiveDemoStep()`'s switch (a no-op with an explicit
  `break`, since it precedes a real action rather than `end`/default's
  empty one).
- `webClient/index.html` — `#liveDemoStrip` status-strip markup, sibling to
  `#tourOverlay`, never nested inside it; its button is `#liveDemoStripCancelBtn`
  with the label "Cancel".
- `webClient/help.html` — `#replayLiveDemoBtn` link, next to `#replayTourBtn`.
- `webClient/style.css` — `.live-demo-strip` and related rules, restyled
  from a bottom-center pill to a full-width top banner, then changed again
  from a `position: fixed` overlay to a normal-flow flex item (`order: -1`)
  that pushes `.app-container` down (see "Visibility" above), plus
  `z-index: 2100` so it stays visible above an open modal's scrim;
  `.app-container` itself changed from a fixed `height: 100dvh` to
  `flex: 1; min-height: 0;` so it shrinks to fit; `.live-demo-strip-cancel`/
  `.live-demo-strip-cancel:hover` (renamed from `.live-demo-strip-skip`).
- `tests/e2e/fixtures.js` — the default `test` fixture now also pre-seeds
  `ydylLiveDemoSeen`, so the rest of the e2e suite (which already relied on
  the tour being pre-skipped) isn't disrupted by the new auto-trigger.
- `tests/e2e/onboarding.spec.js` — a `beforeEach` pre-seeds `ydylLiveDemoSeen`
  so this file stays scoped to just the tour, exactly as before.
- `tests/e2e/live-demo.spec.js` — existed for a while (auto-chain-after-tour,
  auto-trigger for a tour-seen/demo-unseen visitor, no auto-trigger once
  both are seen, manual replay including the no-inline-link chart fallback,
  Cancel, and the translate-error path) but was **removed**: it's the only
  spec in this suite that needs this repo's real presets.json (for its
  hardcoded `postgres-world-stats` connection) rather than the single
  synthetic "Default DB" every other spec assumes, and every spec in
  `tests/e2e/` shares ONE webServer process - pinning `DATABASE_PRESETS_FILE`
  for this one file's sake (tried once) silently changed the default
  active connection for every other spec and broke 14 of them
  (config-modal.spec.js, chat-history-persistence.spec.js,
  schema-viewer.spec.js). Leaving it unset instead made this spec's own
  connection-switch step depend on whatever the invoking shell happened to
  have exported, which is exactly the kind of machine-dependent flake this
  suite's other env vars are deliberately blanked to avoid (see
  `playwright.config.js`'s own `DEFAULT_MODEL`/etc. comment) - there's no
  fix available that doesn't trade one of those two problems for the
  other, so e2e coverage for this feature was dropped entirely.
  `tests/e2e/onboarding.spec.js` still pre-seeds `ydylLiveDemoSeen` so the
  tour suite stays unaffected by this feature's auto-trigger; that's the
  only e2e trace of Live Demo Mode left.

## Verification

- `node --check` on every touched `.js` file, after every change (initial
  build, the pacing bugfix, the holdMs additions, the Cancel rename +
  top-banner restyle, the push-down/z-index fixes, and the `start` step),
  plus a Python `json.load()` round-trip on `demo-script.json` after every
  hand-edit to it.
- Full real `pytest` run (as of the initial build): 1806/1806 server tests
  passing (no server-side files have been touched by this feature at all,
  at any point).
- Live Demo Mode no longer has a dedicated e2e spec (see the
  `tests/e2e/` bullet above for why) - `tests/e2e/onboarding.spec.js`
  (unaffected) and a full `pytest` run are the remaining automated
  coverage for this area; everything else about this feature has been
  verified by hand on the user's own machine instead.
