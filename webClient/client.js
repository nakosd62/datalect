// =============================================================================
// client.js - file map (in order; all sections share one closure/scope, see
// the top-level DOMContentLoaded listener below):
//   1. State & chat history store
//   2. DOM element references + small modal wiring (login-required, help)
//   3. Speech recognition (mic button)
//   4. Shared UI helpers (button state, SQL formatting/display, DB status)
//   5. Backend config sync + database connection config modal
//   6. Help button onboarding (auto-open once, pulsing ring)
//   7. History modal: tabs, stats charts, load/purge
//   8. Results rendering helpers
//   9. Translate (NL -> SQL) and Execute SQL
//  10. Input wiring: NL prompt box, translate/execute buttons
//  11. Quick prompts: dismiss / restore
//  12. History navigation (back/forward through turns), purge, final init
// =============================================================================
document.addEventListener('DOMContentLoaded', async () => {
  // ===========================================================================
  // 1. STATE & CHAT HISTORY STORE
  // ===========================================================================
  // Encapsulates the model's conversation memory: the turns sent to
  // /api/translate as `history`, the undo/redo stacks behind the back/forward
  // arrows, and the "SQL generated but not yet executed" pointer. Consolidating
  // this here (instead of three loose variables mutated from five different
  // places) means the turn cap, the "always push in pairs" rule, and the
  // undo/redo bookkeeping only need to be correct in one place.
  function createChatHistoryStore(maxTurns) {
    // Not a const: the real cap is the server's HISTORY_MAX_TURNS env var
    // (see setMaxTurns() below), which isn't known yet at this synchronous
    // creation point - fetchBackendConfig() hasn't made its first request
    // yet. maxTurns here is just a same-as-server-default fallback so the
    // store is usable immediately; setMaxTurns() reconciles it with the
    // real value as soon as /api/config's response is in, and again on
    // every subsequent fetchBackendConfig() call, so this can never drift
    // from what /api/translate actually replays to the LLM.
    let maxEntries = maxTurns * 2;
    let history = [];
    let future = [];
    let pending = null; // { entry, sql } - see setPending()

    return {
      // Appends one (user, model) turn, enforces the cap, and clears the
      // redo stack (a genuinely new turn invalidates any "future" branch).
      pushTurn(userText, modelEntry) {
        history.push({ role: 'user', text: userText });
        history.push(modelEntry);
        history = history.slice(-maxEntries);
        future = [];
      },
      // Applies a new turn cap (from /api/config's history_max_turns) and
      // immediately re-trims `history` if it's now over the new, smaller
      // limit - rather than waiting for the next pushTurn() to notice.
      // Trimming from the front (oldest turns) matches pushTurn()'s own
      // -maxEntries slice. `future` (the redo stack) is left alone: it's
      // turns the user already stepped back past, not part of what's
      // currently sent to the LLM, so it isn't bound by this cap.
      setMaxTurns(turns) {
        const n = Number(turns);
        if (!Number.isFinite(n) || n <= 0) return; // ignore a missing/invalid value - keep the current cap
        maxEntries = Math.floor(n) * 2;
        if (history.length > maxEntries) {
          history = history.slice(-maxEntries);
        }
      },
      // Marks a chatHistory entry as "SQL generated, awaiting first
      // execution" so executeSql() can fill in its results in place instead
      // of creating a duplicate turn.
      setPending(entry, sql) { pending = { entry, sql }; },
      clearPending() { pending = null; },
      getPending() { return pending; },
      // True only if the pending entry is still the most recent turn (guards
      // against a stale pointer left over from navigating away and back).
      isPendingCurrent() {
        return !!(pending && pending.entry && history.length >= 1 && history[history.length - 1] === pending.entry);
      },
      // Pops the latest turn onto the redo stack. Returns the popped turn, or
      // null if there's nothing to undo.
      undo() {
        if (history.length < 2) return null;
        const modelEntry = history.pop();
        const userEntry = history.pop();
        future.push(userEntry, modelEntry);
        return { userEntry, modelEntry };
      },
      redo() {
        if (future.length < 2) return null;
        const modelEntry = future.pop();
        const userEntry = future.pop();
        history.push(userEntry, modelEntry);
        return { userEntry, modelEntry };
      },
      clear() { history = []; future = []; pending = null; },
      // The turn currently shown in the editor (undefined if history is empty).
      lastTurn() {
        return history.length >= 2
          ? { userEntry: history[history.length - 2], modelEntry: history[history.length - 1] }
          : null;
      },
      turnCount() { return Math.floor(history.length / 2); },
      // Intentionally stricter than "undo() would succeed": with exactly one
      // turn left, going back would pop it and leave the UI blank, so the
      // nav button disables one step early even though undo() itself would
      // still technically work at history.length === 2.
      canUndo() { return history.length > 2; },
      canRedo() { return future.length >= 2; },
      // Which turn is currently shown in the editor, relative to the newest
      // one: 0 at the newest turn, -1 after stepping back once, -2 after
      // twice, etc. Only for the goBackBtn/goForwardBtn nav buttons'
      // history_nav_clicked analytics param (see their click handlers below)
      // - every undo() moves one whole (user, model) turn - 2 entries - onto
      // `future`, and every redo() moves one back off it, so the offset is
      // just -(future.length / 2).
      turnOffset() {
        // Guards against -0 (0 / 2 negated) reaching GA as a distinct value
        // from 0 - harmless in GA4 itself, but surprising to compare
        // against in any test/report that expects a plain 0.
        const stepsBack = future.length / 2;
        return stepsBack === 0 ? 0 : -stepsBack;
      },
      // What gets sent to /api/translate as `history`.
      toPayload() { return history; },
    };
  }

  // Same default as the server's HISTORY_MAX_TURNS (translate_routes.py) -
  // just a fallback until the first fetchBackendConfig() call reconciles
  // it via chatStore.setMaxTurns(), see createChatHistoryStore() above.
  const FALLBACK_HISTORY_TURNS = 10;
  const chatStore = createChatHistoryStore(FALLBACK_HISTORY_TURNS);

  let DEFAULT_DB_URL = "";
  let ACTIVE_DB_URL = "";
  // The active connection's dialect when it's a custom (user-supplied)
  // connection - sourced from /api/config's active_database_type field (see
  // fetchBackendConfig() below). The server only populates that field for
  // custom connections (config_routes.py deliberately leaves it "" for a
  // preset, since a preset's identity is never disclosed beyond its id/name
  // - see active_db_type_out's own comments there) - a preset's dialect is
  // looked up separately, from CONFIGURED_DBS, by getActiveDatabaseType()
  // below. Only used for analytics' database_type param (trackEvent() call
  // sites throughout this file) - never for any connection logic.
  let ACTIVE_DB_TYPE = "";
  // Whether the active connection was explicitly selected as a saved custom
  // connection, rather than a preset. Needed because a custom connection's
  // URL can collide with a preset's (same postgresql://... string) - in that
  // case matching by URL alone can't tell "the preset" from "my custom
  // connection that happens to point at the same database" apart. See its
  // use in renderDbRadioButtons()/renderCustomDbRows() (which radio actually
  // ends up checked) and updateConnectionDetails() (which name the badge
  // shows). Always trust the freshest /api/config response's
  // active_is_custom over recomputing this from URLs.
  let ACTIVE_IS_CUSTOM = false;
  // Which saved custom connection (see renderCustomDbRows()) is actually
  // active, keyed by its server-computed connection_key rather than URL -
  // two saved custom connections can themselves share a URL (e.g. two
  // BigQuery connections on the same project/dataset with different
  // service-account keys), so URL matching alone can't tell them apart
  // either. "" whenever the active connection isn't a custom one, or for a
  // session saved before this existed (renderCustomDbRows() falls back to
  // URL matching in that case).
  let ACTIVE_CUSTOM_CONNECTION_KEY = "";
  // Whether the active connection is authenticating with its own pasted
  // BigQuery service-account key, as opposed to this app's ambient
  // credentials (ADC) - the key itself is never sent to the frontend (see
  // state_store.get_db_connections' has_custom_credentials docstring), so
  // without this flag there was no way for the UI to show a saved custom
  // connection was actually using its own key rather than silently falling
  // back to ADC. Used by updateConnectionDetails() to label the badge.
  let ACTIVE_USES_CUSTOM_CREDENTIALS = false;
  // The active preset's stable, admin-assigned "id" (app_config.py's
  // DATABASE_PRESETS_FILE "id" field - see its doc-comment there), as
  // reported by the server's active_preset_id. Unlike the URL/array-index
  // matching this replaced, "id" is never a secret (safe to send to
  // anonymous Cloud Run visitors, who never receive a preset's real
  // connection string - see the redacted configured_databases below) and
  // survives the admin reordering/adding/removing presets between
  // deployments - so both anonymous and signed-in users now match presets
  // by this one field uniformly (see renderDbRadioButtons()). null when the
  // active connection isn't a preset at all (a custom connection instead).
  let ACTIVE_PRESET_ID = null;
  let CONFIGURED_DBS = [];
  // Multi-database question-answering (see server/translate_routes.py's
  // module docstring): the set of connections the user has marked "in
  // scope". Populated straight from /api/config's in_scope_preset_ids/
  // in_scope_custom_connection_keys (see fetchBackendConfig()). The
  // connection picker is a single-select radio group again (see
  // renderDbRadioButtons()) - EITHER one specific connection OR the "All
  // configured databases" option, and which one is checked is decided by
  // IN_SCOPE_MODE (below), not by how many entries these two arrays
  // happen to sum to - see isAllConnectionsSelected(). A single in-scope
  // connection behaves exactly as before any of this multi-database
  // feature existed - these two arrays existing/being non-empty is what
  // the rest of the client uses to decide whether any of the new
  // multi-database UI (the disclosure banner, per-tab database labels,
  // pinning) is even relevant for the current session.
  let IN_SCOPE_PRESET_IDS = [];
  let IN_SCOPE_CUSTOM_KEYS = [];
  // The server's persisted "single"|"all" choice (see state_store.py's
  // in_scope_mode docstring) - always one of those two strings once
  // /api/config has ever returned (the server itself defaults a
  // blank/never-set session to "single", never a raw null/undefined), so
  // isAllConnectionsSelected() can just check this directly instead of
  // inferring "all" from the in-scope arrays' combined length. That
  // length-based inference used to be the only signal available (before
  // the server persisted in_scope_mode at all) and gets two edge cases
  // wrong on its own: a legacy session with 2+ specific connections
  // in scope (in_scope_mode still "single") would misread as "All", and a
  // session in "all" mode with only ONE connection actually configured
  // (in_scope_preset_ids/in_scope_custom_connection_keys summing to 1)
  // would misread as that one specific connection instead of "All".
  let IN_SCOPE_MODE = 'single';
  let MAX_IN_SCOPE_CONNECTIONS = 20;
  // Which connection(s) THIS conversation has actually used, as
  // {kind: "preset"|"custom", id, name} references (never raw descriptors/
  // credentials - the server re-resolves fresh, credentialed descriptors
  // from these on every request). Set from a /api/translate response's
  // connection_selection field (only ever present when 2+ connections are
  // in scope) and echoed back on every subsequent /api/translate/
  // /api/execute call in the same conversation as `pinned_connections`, so
  // a follow-up question reuses the same connection(s) rather than
  // re-deciding from scratch. Reset by clearActiveQueryState() - the same
  // single reset point new-chat/logout/sign-in/connection-switch already
  // funnel through - so it never outlives the conversation it was set for.
  let PINNED_CONNECTIONS = [];
  // Model-selection state (see fetchBackendConfig()/updateModelBadge()/
  // renderModelRadioButtons()) - mirrors CONFIGURED_DBS/ACTIVE_DB_URL's own
  // "fetched once per /api/config round-trip, read by the badge and the
  // modal's render function" pattern. LLM_PROVIDERS is the GET response's
  // 'llm_providers' list verbatim: [{name, preset_models, default_model}, ...].
  let LLM_PROVIDERS = [];
  let ACTIVE_LLM_PROVIDER = "";
  let ACTIVE_LLM_MODEL = "";
  // "Bring Your Own Key" (Preferences dialog's third section) - the GET
  // response's 'llm_byok_key_set' verbatim: {google: bool, anthropic: bool,
  // openai: bool}, reporting whether THIS user has a saved key for each
  // provider. Booleans only, same as every other field this module fetches
  // from /api/config's response - the raw key is never sent to the
  // browser at all (see state_store.py's get_session docstring on
  // llm_byok_key_set), so there is no client-side variable holding it.
  let LLM_BYOK_KEY_SET = { google: false, anthropic: false, openai: false };
  let currentGoogleClientId = null;
  let googleIdToken = null;
  let customDbUrl = "";
  let customDbName = "";
  let customDatabases = [];
  let autoSqlExecuteEnabled = true;
  // True when running on Cloud Run and the current request has no verified
  // login (i.e. the backend resolved it to a per-session "anonymous:..."
  // identity - see auth.py's ANONYMOUS_USER_ID_PREFIX). Anonymous users get
  // full translate/execute functionality, their own (session-scoped,
  // isolated) translation history, AND their own custom DB connections -
  // nothing is gated behind sign-in anymore. This flag still matters for
  // the UI, though: an anonymous visitor's admin-configured presets are
  // never sent their real connection strings/credentials (unlike their own
  // custom connections), so the config modal never shows a preset's URL to
  // them - see renderDbRadioButtons(), which now matches presets by id
  // (ACTIVE_PRESET_ID) for anonymous and signed-in users alike.
  let isAnonymousUser = false;

  // True once /api/config reports Google Sign-In is configured (auth_enabled
  // + a google_client_id). Used to skip tour/UI bits that point at the
  // sign-in control when there's nothing there to point at (local/no-auth
  // deployments).
  let googleAuthEnabled = false;

  function getDatabaseNameFromUrl(urlStr) {
    if (!urlStr) return "Custom";
    try {
      let urlToParse = urlStr;
      if (!urlStr.includes("://") && !urlStr.startsWith("/")) {
        urlToParse = "postgresql://" + urlStr;
      }
      const url = new URL(urlToParse);
      let dbname = url.pathname.replace(/^\//, '');
      if (dbname.includes('?')) {
        dbname = dbname.split('?')[0];
      }
      return dbname || "Custom";
    } catch (e) {
      try {
        const match = urlStr.match(/\/([^/?#]+)(\?|#|$)/);
        if (match && match[1]) {
          return match[1];
        }
      } catch (err) {}
      return "Custom";
    }
  }

  function maskConnectionUrl(urlStr) {
    if (!urlStr) return "";
    try {
      const match = urlStr.match(/^([^:]+:\/\/)([^:]+):([^@]+)(@.+)$/);
      if (match) {
        return `${match[1]}${match[2]}:******${match[4]}`;
      }
      return urlStr;
    } catch (e) {
      return urlStr;
    }
  }

  function unmaskConnectionUrl(newValue, originalUrl) {
    if (!newValue) return "";
    if (newValue.includes(":******@") && originalUrl) {
      try {
        const origMatch = originalUrl.match(/^([^:]+:\/\/)([^:]+):([^@]+)(@.+)$/);
        if (origMatch) {
          const originalPassword = origMatch[3];
          return newValue.replace(/:[*]{6}@/, `:${originalPassword}@`);
        }
      } catch (e) {
        console.error("Failed to unmask URL:", e);
      }
    }
    return newValue;
  }

  // Active state tracker for multi-tab query results
  let currentResultsList = [];
  let activeResultIndex = 0;

  // Report Error / Report Wrong Result (see report_routes.py's module
  // docstring, and setReportContext()/reportButtonHtml() below) - True
  // once GET /api/config's 'issue_reporting_enabled' confirms a deployer
  // has actually configured a recipient + SMTP connection server-side.
  // reportButtonHtml() renders nothing at all while this is False, rather
  // than showing a button that would just fail the moment it's clicked.
  let ISSUE_REPORTING_ENABLED = false;

  // Whatever's CURRENTLY on screen in the results area that's eligible to
  // be reported, or null when nothing is (no result yet, a translation/
  // network/history error - those are already-handled cases out of this
  // feature's scope, see report_routes.py's module docstring - a cancelled
  // query, or the initial empty state). Kept in sync by setReportContext(),
  // called from every render path that shows something reportable
  // (renderTableResult()'s isText/isError/success/no-dataset branches,
  // renderNoSqlResponse(), and executeSql()'s own bare connect()-failure
  // fallback that bypasses renderTableResult entirely) - see each call
  // site's own comment for why it passes what it does.
  let currentReportContext = null;

  // Whichever report/feedback context the CURRENTLY-OPEN #reportIssueModal
  // is actually for - set once, at openReportIssueModal() time, and read
  // by buildReportPayload()/sendReportIssue() from then on, rather than
  // those re-reading currentReportContext live. Two reasons this is a
  // separate variable instead of just reusing currentReportContext
  // directly: (1) the Help dialog's "Send Feedback" button (see
  // REPORT_CATEGORY_CONFIG.feedback below) opens this same modal with a
  // synthetic {category: 'feedback'} context that was never, and should
  // never be, assigned to currentReportContext - that variable's whole
  // purpose is tracking what's reportABLE about the currently-displayed
  // results tab, which "feedback about the app in general" simply isn't.
  // (2) it keeps the open modal stable against currentReportContext
  // changing out from under it - e.g. a background render resetting it to
  // null - while the user is still filling in the details textarea.
  let activeReportContext = null;

  // "All databases" mode's "route" outcome (see translate_routes.py's
  // module docstring): tracks ONE streaming turn's progressive-render
  // state from the moment its "phase_a_route" NDJSON event arrives
  // (startAllModeStreaming()) through however many "phase_b_connection_
  // done" events follow (handlePhaseBConnectionDone()), any per-
  // connection /api/execute calls that fire along the way
  // (executeOneAllModeConnection()), and finally maybeFinalize() once
  // every selected connection has settled AND the terminal /api/translate
  // line has arrived. Set ONLY inside translatePrompt() - deliberately
  // NOT inside clearResultsDisplay(), since executeSql() also calls that
  // same helper at its own start and would otherwise wipe this out before
  // a manual Execute click (auto-execute disabled - see executeSql()'s
  // own router-route branch below) gets a chance to read it. Null again
  // once a turn has fully settled (maybeFinalize()) or hit its one
  // "never persists history" partial-failure branch (executeSql()'s
  // failure branch, matching this mode's pre-existing behavior from
  // before this streaming redesign).
  let allModeStreamState = null;

  // Fallback for a router_route response that arrives with NO live
  // "phase_a_route"/"phase_b_connection_done" events at all - i.e.
  // allModeStreamState above was never created for this turn. In real
  // production traffic this never happens (translate_routes.py's
  // stream_translation() always emits phase_a_route before any "route"
  // outcome's terminal line), but a non-streamed single-JSON response
  // still needs to work correctly - the old-browser fallback in
  // readTranslateStream() (no ReadableStream support), or a test double
  // that mocks /api/translate as one flat body with no NDJSON framing at
  // all. Same shape/lifecycle this app used for EVERY router_route turn
  // before progressive streaming existed: set in translatePrompt()'s
  // router_route branch (only in its `else` - no live stream - case),
  // consumed (cleared back to null) exactly once by whichever branch of
  // executeSql() actually renders with it, or immediately in
  // translatePrompt() itself when there's nothing left to execute at all.
  let pendingAllModeNotes = null;

  // Re-entrancy guard shared by translatePrompt() and executeSql(), the
  // app's only two entry points that fire a translation/execution turn.
  // Checked-and-set as the very FIRST synchronous statement in each -
  // before either function's own `await fetchBackendConfig()` - so there
  // is no gap for a second concurrent call to slip through. Before this
  // flag existed, setButtonsDisabled(true) (the only "an action is in
  // flight" signal either function had) wasn't applied until AFTER that
  // first await resolved, so a second Enter press, Translate click, or
  // Execute click landing during that real network round trip started a
  // fully concurrent second call - both calls then mutated the exact same
  // shared, non-request-scoped state (allModeStreamState,
  // pendingAllModeNotes, currentResultsList, chatStore, the one shared
  // CodeMirror sqlEditor instance, the one resultsRetryStatus banner) with
  // no isolation, so whichever call's async work resolved last silently
  // won - overwriting or corrupting the other's still-in-flight turn.
  // executeSql() is also called internally, already-awaited, from within
  // translatePrompt() itself (its two `autoSqlExecuteEnabled` branches) -
  // those calls pass `{ internal: true }` to skip re-checking/re-setting
  // this flag, since translatePrompt() already holds it for the whole
  // turn, execute included.
  let uiActionBusy = false;

  // Backs the "Cancel" button (cancelInFlightQuery() below): the
  // AbortController whose signal every fetch() belonging to the CURRENT
  // turn is given, so aborting one call aborts every other in-flight
  // fetch that's part of the same turn too (e.g. translatePrompt()'s own
  // /api/translate call and any /api/execute call it kicked off
  // internally). Replaced (never mutated) at the top of every NEW,
  // non-internal translatePrompt()/executeSql() call - an internal
  // executeSql() call (one made FROM WITHIN translatePrompt(), already
  // awaited there) reuses whatever controller the enclosing turn already
  // set, since it's part of the same turn, not a new one.
  let currentAbortController = null;

  // Monotonically increasing counter identifying the CURRENT turn, bumped
  // only by a new, non-internal translatePrompt()/executeSql() call (an
  // internal executeSql() call reads this without bumping it - see
  // currentAbortController's comment above for why). Each such call
  // captures its own `myTurnId` at the moment it starts; any cleanup code
  // that runs later (a `.then()`/`.catch()`/`finally` on a fetch promise)
  // checks `myTurnId === currentTurnId` before touching any shared UI
  // state (buttons, uiActionBusy, banners, results). This is what makes
  // cancelInFlightQuery() safe: it resets everything synchronously and
  // bumps nothing itself, but the original (now-stale) call's own
  // eventual cleanup - which can still arrive asynchronously well after
  // the Cancel click, since aborting a fetch doesn't retroactively un-queue
  // work already scheduled on it - will see its captured myTurnId no
  // longer matches (either because the user cancelled, or because they
  // started a newer turn before the old one's promise even settled) and
  // skip mutating state a newer turn now owns.
  let currentTurnId = 0;

  // Helper function to include Google ID tokens or auth headers in fetch requests
  function getApiHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (googleIdToken) {
      headers['Authorization'] = `Bearer ${googleIdToken}`;
    }
    return headers;
  }

  // ===========================================================================
  // ANALYTICS (Google Analytics via gtag.js - see index.html's gtag.js
  // snippet, which only ever configures the default page_view/enhanced-
  // measurement events on its own). Beyond that, this app fires a small,
  // fixed set of custom events for the interactions actually worth seeing
  // in GA4 - see trackEvent()'s call sites throughout this file for the
  // full list: translate_submitted, sql_executed, error_shown,
  // report_submitted, database_selected, model_selected, help_viewed,
  // history_viewed, history_nav_clicked, preferences_viewed, login, logout,
  // mic_used, quick_prompt_clicked, tour_exited. Custom, app-specific names
  // throughout (not GA4's own recommended-event vocabulary) - per explicit
  // request.
  // ===========================================================================

  // GA4 silently truncates a custom event parameter's string value at 100
  // characters - truncating here instead makes that visible in the value
  // itself (a trailing '…') rather than a value that just quietly stops
  // mid-word in GA4's UI with no indication anything was cut. Used by every
  // call site below that passes free text a user typed/received (prompts,
  // SQL, error messages) - never needed for a short, bounded value (a
  // provider name, a category string).
  function truncateForAnalytics(value, maxLength = 100) {
    const text = (value == null ? '' : String(value)).trim();
    if (text.length <= maxLength) return text;
    return text.slice(0, maxLength - 1) + '…';
  }

  // Thin wrapper around gtag('event', ...) - every call site just passes
  // plain, already-computed params. Safe to call even if gtag.js hasn't
  // loaded (or never loads at all - an ad/tracker blocker, offline dev,
  // the script still downloading): window.gtag is defined synchronously by
  // index.html's own inline snippet (it just queues into `dataLayer`,
  // resolved later once/if the async script itself loads), so this is
  // effectively always available by the time any of this file's event
  // handlers can fire - the guard just keeps a missing/blocked gtag.js
  // from ever throwing instead of silently no-op'ing.
  function trackEvent(name, params) {
    if (typeof window.gtag === 'function') {
      window.gtag('event', name, params || {});
    }
  }

  // The active connection's dialect, for pairing with database_name on
  // analytics events. A preset's type never comes back on ACTIVE_DB_TYPE
  // itself (see that variable's own comment - the server only sends
  // active_database_type for a custom connection), so this looks a preset's
  // type up from CONFIGURED_DBS by ACTIVE_PRESET_ID instead - the same
  // "match by id, not URL" pattern updateConnectionDetails() already uses
  // for the exact same preset-vs-custom distinction.
  function getActiveDatabaseType() {
    if (ACTIVE_IS_CUSTOM) return ACTIVE_DB_TYPE || '';
    const preset = CONFIGURED_DBS.find((db) => db.id === ACTIVE_PRESET_ID);
    return (preset && preset.type) || '';
  }

  // ===========================================================================
  // 2. DOM ELEMENT REFERENCES + SMALL MODAL WIRING
  //    (login-required modal, help modal fetch/open logic - full onboarding
  //    wiring for the help button lives further down, in section 6)
  // ===========================================================================

  // Every modal (#configModal, #helpModal, #historyModal, #confirmModal,
  // #loginRequiredModal) shares the exact same .modal-overlay z-index (see
  // style.css) - fine when only one is ever open at a time, but two CAN
  // legitimately be open together now (e.g. the "See Help & Documentation"
  // link inside the DB connection dialog opens #helpModal without closing
  // #configModal first). With z-index tied, stacking falls back to DOM
  // order, which has nothing to do with which modal the user actually
  // opened most recently - #configModal happens to sit later in
  // index.html than #helpModal, so it always won and visually buried Help
  // behind it. bringModalToFront() fixes that generally, for any modal
  // opened on top of any other: each call hands out a fresh, strictly
  // increasing inline z-index, so whichever modal was shown/clicked-into
  // last is always the one on top - call it right alongside every
  // `<modal>.classList.remove('hidden')` in this file. Starts one above
  // .modal-overlay's own 1000 and stays far below .tour-overlay's 2000,
  // even after many opens in one session.
  let nextModalZIndex = 1001;
  function bringModalToFront(modalEl) {
    if (!modalEl) return;
    modalEl.style.zIndex = String(nextModalZIndex++);
  }

  // ===========================================================================
  // THEME SWITCHING (dark/light - see the Preferences modal below). Persisted
  // client-side only (localStorage), unlike auto_sql_execute which is a
  // server-side session field - there's no server-rendered content whose
  // correctness depends on theme, so there's nothing for the backend to know.
  // An inline <head> script in index.html reads the same storage key before
  // any stylesheet loads (see its comment there) so the very first paint
  // already has the right data-theme attribute - this section only handles
  // switching it after load, plus keeping CodeMirror/Chart.js in sync since
  // neither reads CSS custom properties on its own.
  // ===========================================================================
  const THEME_STORAGE_KEY = 'datalectTheme';

  function getCurrentTheme() {
    const attr = document.documentElement.getAttribute('data-theme');
    return attr === 'light' ? 'light' : 'dark';
  }

  function setTheme(theme) {
    const normalized = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', normalized);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch (e) {
      // localStorage unavailable (private browsing, disabled storage, etc.) -
      // the theme still applies for this page view, it just won't persist.
    }
    if (sqlEditor) {
      sqlEditor.setOption('theme', normalized === 'light' ? 'eclipse' : 'dracula');
    }
    // The history-stats charts bake resolved colors into their Chart.js
    // config at creation time (Chart.js doesn't read CSS custom properties
    // live), so the only way to re-theme an already-rendered chart is to
    // rebuild it from the same data used last time.
    if ((chartCountInstance || chartTotalTokensInstance) && lastStatsDataForCharts) {
      renderStatisticsCharts(lastStatsDataForCharts);
    }
  }

  // DOM Elements - Primary Controls
  const aiPrompt = document.getElementById('aiPrompt');
  const sqlQueryTextarea = document.getElementById('sqlQuery');
  const translateBtn = document.getElementById('translateBtn');
  const runBtn = document.getElementById('runBtn');
  const stopBtn = document.getElementById('stopBtn');
  const purgeHistoryBtn = document.getElementById('purgeHistoryBtn');
  const goBackBtn = document.getElementById('goBackBtn');
  const goForwardBtn = document.getElementById('goForwardBtn');
  updateHistoryNavButtons();
  const micBtn = document.getElementById('micBtn');
  // Opens #reportIssueModal in 'wrong_sql' mode (see REPORT_CATEGORY_CONFIG
  // below) - sits beside #runBtn inside the SQL box itself, so it's wired
  // separately from both the resultsBody-delegated error/wrong_result
  // triggers and the header's #sendFeedbackBtn.
  const reportSqlBtn = document.getElementById('reportSqlBtn');

  // DOM Elements - Config Modal & Connection Status
  const configModal = document.getElementById('configModal');
  const configTriggerBadge = document.getElementById('configTriggerBadge');
  const modalCloseBtn = document.getElementById('modalCloseBtn');
  const configSaveBtn = document.getElementById('configSaveBtn');
  const connDbName = document.getElementById('connDbName');
  const connDbDot = document.getElementById('connDbDot');

  // DOM Elements - Model Selection Modal & Badge (mirrors the DB connection
  // badge/modal pair above - see updateModelBadge()/renderModelRadioButtons()).
  const modelModal = document.getElementById('modelModal');
  const modelTriggerBadge = document.getElementById('modelTriggerBadge');
  const modelModalCloseBtn = document.getElementById('modelModalCloseBtn');
  const modelSaveBtn = document.getElementById('modelSaveBtn');
  const modelBadgeName = document.getElementById('modelBadgeName');

  // DOM Elements - Preferences Modal (theme + auto-execute-SQL - opened from
  // the header's #prefsBtn on desktop, or #moreMenuPrefsBtn on mobile; see
  // the wiring block below). autoSqlExecuteCheckbox used to live in
  // #configModal - its id is unchanged so every other reference to it below
  // still resolves, only its home in the DOM (and its save flow) moved.
  const preferencesModal = document.getElementById('preferencesModal');
  const prefsBtn = document.getElementById('prefsBtn');
  const preferencesModalCloseBtn = document.getElementById('preferencesModalCloseBtn');
  const preferencesSaveBtn = document.getElementById('preferencesSaveBtn');
  const themeOptionDark = document.getElementById('themeOptionDark');
  const themeOptionLight = document.getElementById('themeOptionLight');
  const autoSqlExecuteCheckbox = document.getElementById('autoSqlExecuteCheckbox');
  // Bring Your Own Key (Preferences dialog's third section) - one
  // {input, clearBtn} pair per provider, keyed by the same "google"/
  // "anthropic"/"openai" names used everywhere else (LLM_PROVIDERS,
  // LLM_BYOK_KEY_SET, translate_routes.py's _LLM_PROVIDERS).
  const BYOK_PROVIDER_FIELDS = {
    google: {
      input: document.getElementById('byokKeyGoogle'),
      clearBtn: document.querySelector('.byok-clear-btn[data-byok-provider="google"]'),
    },
    anthropic: {
      input: document.getElementById('byokKeyAnthropic'),
      clearBtn: document.querySelector('.byok-clear-btn[data-byok-provider="anthropic"]'),
    },
    openai: {
      input: document.getElementById('byokKeyOpenai'),
      clearBtn: document.querySelector('.byok-clear-btn[data-byok-provider="openai"]'),
    },
  };
  // Tracks which provider(s) had their "x" (remove) button clicked since
  // the modal was last opened - see loadPreferencesIntoUI()/savePreferences()
  // below for why an empty input alone isn't enough to tell "leave this
  // key untouched" apart from "actively clear it" (the key is never
  // redisplayed, so a blank box is also what an already-saved key looks
  // like - see LLM_BYOK_KEY_SET). Typing into a box after clicking its "x"
  // un-marks it, so an immediate change of mind doesn't still send a clear
  // alongside the freshly typed replacement.
  const byokProvidersMarkedForClear = new Set();

  // DOM Elements - Login Required Modal. Not currently triggered by
  // anything: translation history and saving a custom DB connection were
  // the two features this used to gate for anonymous visitors, and neither
  // needs sign-in anymore (see isAnonymousUser's comment above). Left in
  // place (and still wired below) in case a future gated feature needs it.
  const loginRequiredModal = document.getElementById('loginRequiredModal');
  const loginRequiredModalText = document.getElementById('loginRequiredModalText');
  const loginRequiredModalCloseBtn = document.getElementById('loginRequiredModalCloseBtn');
  const loginRequiredModalOkBtn = document.getElementById('loginRequiredModalOkBtn');

  function showLoginRequiredModal(message) {
    if (!loginRequiredModal) return;
    if (loginRequiredModalText) loginRequiredModalText.textContent = message;
    loginRequiredModal.classList.remove('hidden');
    bringModalToFront(loginRequiredModal);
  }

  function closeLoginRequiredModal() {
    if (loginRequiredModal) loginRequiredModal.classList.add('hidden');
  }

  if (loginRequiredModalCloseBtn) {
    loginRequiredModalCloseBtn.addEventListener('click', closeLoginRequiredModal);
  }
  if (loginRequiredModalOkBtn) {
    loginRequiredModalOkBtn.addEventListener('click', closeLoginRequiredModal);
  }
  if (loginRequiredModal) {
    loginRequiredModal.addEventListener('click', (e) => {
      if (e.target === loginRequiredModal) closeLoginRequiredModal();
    });
  }

  // The DB config badge is fully clickable for anonymous users - they may
  // open the dialog, switch between admin-configured presets, AND save
  // their own custom connections (see isAnonymousUser's comment above).
  // Nothing is gated behind sign-in here anymore, so the badge's tooltip no
  // longer needs to differ by identity - kept as a function (rather than
  // inlined at the call site) in case a future gated feature needs it
  // again. Called whenever isAnonymousUser changes (i.e. every time
  // fetchBackendConfig() resolves).
  function updateAnonymousRestrictions() {
    if (configTriggerBadge) {
      configTriggerBadge.title = 'Connection Info (Click to configure)';
    }
  }

  // DOM Elements - Help Modal
  const helpModal = document.getElementById('helpModal');
  const helpBtn = document.getElementById('helpBtn');
  const helpModalCloseBtn = document.getElementById('helpModalCloseBtn');
  const helpModalBody = document.getElementById('helpModalBody');

  // Fetches help.html once and caches the result, so repeat opens of the
  // modal don't re-fetch. help.html is a plain HTML fragment (not a full
  // document) served as a static asset alongside index.html.
  let helpContentPromise = null;
  function loadHelpContent() {
    if (!helpContentPromise) {
      helpContentPromise = fetch('help.html')
        .then(res => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.text();
        });
    }
    return helpContentPromise;
  }

  function openHelpModal() {
    if (!helpModal) return;
    helpModal.classList.remove('hidden');
    bringModalToFront(helpModal);
    updateRestoreQuickPromptsVisibility();
    if (!helpModalBody) return;
    loadHelpContent()
      .then(html => {
        helpModalBody.innerHTML = html;
      })
      .catch(err => {
        helpModalBody.innerHTML = '<p class="text-muted">Sorry, the documentation could not be loaded. Please try again.</p>';
        console.error('Failed to load help.html:', err);
        // Allow retrying on next open rather than caching the failure.
        helpContentPromise = null;
      });
  }

  // DOM Elements - History Modal & Tabs
  const historyModal = document.getElementById('historyModal');
  const historyBtn = document.getElementById('historyBtn');
  const historyModalCloseBtn = document.getElementById('historyModalCloseBtn');
  const historyTableHeader = document.getElementById('historyTableHeader');
  const historyTableBody = document.getElementById('historyTableBody');

  const tabBtnTranslations = document.getElementById('tabBtnTranslations');
  const tabBtnStatistics = document.getElementById('tabBtnStatistics');
  const historyTabTranslations = document.getElementById('historyTabTranslations');
  const historyTabStatistics = document.getElementById('historyTabStatistics');

  // DOM Elements - Results Table & Tabs
  const resultsRetryStatus = document.getElementById('resultsRetryStatus');
  const resultsTabsNav = document.getElementById('resultsTabsNav');
  const resultsHeader = document.getElementById('resultsHeader');
  const resultsBody = document.getElementById('resultsBody');

  // DOM Elements - Report Error / Report Wrong Result (see
  // setReportContext()/reportButtonHtml() and openReportIssueModal()
  // below). There's no static button element here any more - the button
  // itself is rendered INLINE, inside whichever tab it's reporting on (see
  // reportButtonHtml()'s own comment for why), so only the modal has fixed
  // DOM elements to look up.
  const reportIssueModal = document.getElementById('reportIssueModal');
  const reportIssueModalTitle = document.getElementById('reportIssueModalTitle');
  const reportIssueModalCloseBtn = document.getElementById('reportIssueModalCloseBtn');
  const reportIssueIntro = document.getElementById('reportIssueIntro');
  const reportIssuePreviewSection = document.getElementById('reportIssuePreviewSection');
  const reportIssuePreviewLabel = document.getElementById('reportIssuePreviewLabel');
  const reportIssuePreview = document.getElementById('reportIssuePreview');
  // Editable counterpart to reportIssuePreview above - shown instead of it
  // only for categories with previewEditable:true (currently just
  // 'wrong_sql' - see REPORT_CATEGORY_CONFIG and openReportIssueModal()).
  const reportIssuePreviewEditable = document.getElementById('reportIssuePreviewEditable');
  const reportIssueDetailsLabel = document.getElementById('reportIssueDetailsLabel');
  const reportIssueDetails = document.getElementById('reportIssueDetails');
  const reportIssueStatus = document.getElementById('reportIssueStatus');
  const reportIssueSendBtn = document.getElementById('reportIssueSendBtn');
  const reportIssueCancelBtn = document.getElementById('reportIssueCancelBtn');
  // Opens the same modal in 'feedback' mode (see REPORT_CATEGORY_CONFIG
  // below) - lives in the app header (next to the Doc/#helpBtn button), not
  // a results tab or the Help dialog, so it's wired separately from the
  // resultsBody-delegated error/wrong_result triggers. Its narrow-screen
  // twin, #moreMenuFeedbackBtn (see the MORE MENU section below), simply
  // forwards to a click on this same button rather than duplicating any of
  // this wiring.
  const sendFeedbackBtn = document.getElementById('sendFeedbackBtn');

  // Chart.js Instances
  let chartCountInstance = null;
  let chartTotalTokensInstance = null;

  // ===========================================================================
  // 3. SPEECH RECOGNITION (mic button)
  // ===========================================================================
  // Speech Recognition Instance & Multi-target Handler
  let recognition = null;
  let isListening = false;
  let activeMicBtn = null;
  let activeTargetInput = null;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onstart = () => {
      isListening = true;
      if (activeMicBtn) activeMicBtn.classList.add('listening');
    };

    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      // Occurrence only - no recorded speech content is sent (privacy).
      trackEvent('mic_used', {});
      if (activeTargetInput) {
        activeTargetInput.value = transcript;
        activeTargetInput.dispatchEvent(new Event('input'));
      }
    };

    recognition.onerror = (event) => {
      console.error('Speech recognition error:', event.error);
      if (activeMicBtn) activeMicBtn.classList.remove('listening');
      isListening = false;
    };

    recognition.onend = () => {
      if (activeMicBtn) activeMicBtn.classList.remove('listening');
      isListening = false;
    };
  } else {
    if (micBtn) micBtn.style.display = 'none';
  }

  function setupMicButton(btn, targetInput) {
    if (!btn || !recognition) return;
    btn.addEventListener('click', () => {
      if (isListening) {
        recognition.stop();
        if (activeMicBtn === btn) return;
      }
      activeMicBtn = btn;
      activeTargetInput = targetInput;
      recognition.start();
    });
  }

  setupMicButton(micBtn, aiPrompt);

  let sqlEditor = null;
  if (sqlQueryTextarea && window.CodeMirror) {
    sqlEditor = window.CodeMirror.fromTextArea(sqlQueryTextarea, {
      mode: 'text/x-sql',
      theme: getCurrentTheme() === 'light' ? 'eclipse' : 'dracula',
      lineNumbers: true,
      lineWrapping: true,
      placeholder: sqlQueryTextarea.getAttribute('placeholder') || "You may enter SQL here and execute it..."
    });
  }

  const sqlContainer = document.querySelector('.speech-bubble-wrapper.sql-bubble');

  if (sqlContainer && sqlEditor && window.ResizeObserver) {
    let resizeTimer;
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        sqlEditor.setSize('100%', '100%');
        sqlEditor.refresh();
      }, 50);
    });
    resizeObserver.observe(sqlContainer);
  }

  window.addEventListener('resize', () => {
    if (sqlEditor) {
      sqlEditor.setSize('100%', '100%');
      sqlEditor.refresh();
    }
  });

  function parseJwt(token) {
    try {
      const base64Url = token.split('.')[1];
      const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
      const jsonPayload = decodeURIComponent(atob(base64).split('').map(c => {
        return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
      }).join(''));
      return JSON.parse(jsonPayload);
    } catch (e) {
      return null;
    }
  }

  let lastRenderedAuthState = null;

  function handleLogout() {
    trackEvent('logout', {});
    googleIdToken = null;
    if (window.google && google.accounts && google.accounts.id) {
      google.accounts.id.disableAutoSelect();
    }
    // Logging out drops back to a (new, distinct) anonymous session - the
    // prompt/SQL/results on screen belonged to the just-logged-out user's
    // identity and connection, so they're cleared the same way a DB
    // connection change clears them (see clearActiveQueryState()).
    clearActiveQueryState();
    renderAuthUI(currentGoogleClientId);
    fetchBackendConfig();
  }

  function renderAuthUI(clientId) {
    if (clientId) currentGoogleClientId = clientId;
    const container = document.getElementById('g_id_signin');
    if (!container) return;

    const existingToken = googleIdToken;
    const payload = existingToken ? parseJwt(existingToken) : null;
    const isExpired = payload && payload.exp && (payload.exp * 1000 < Date.now());

    // renderAuthUI() runs on every fetchBackendConfig() call - including
    // once per prompt/execute, since translatePrompt() re-syncs config
    // first. When nothing about the auth state has actually changed,
    // skip re-rendering: for a signed-out (anonymous) user the "no
    // token" branch below tears down and rebuilds the Google Sign-In
    // button (a real iframe) from scratch, which was causing visible
    // header flicker/jitter on every single request. Signed-in users
    // don't hit this because their branch renders a small static avatar
    // div, and local (no-auth) mode never calls this function at all -
    // which is why the jitter only showed up for anonymous Cloud Run use.
    const signedIn = !!(existingToken && payload && !isExpired);
    const authStateKey = signedIn ? `in:${payload.email || ''}` : `out:${currentGoogleClientId || ''}`;
    if (authStateKey === lastRenderedAuthState) {
      return;
    }
    lastRenderedAuthState = authStateKey;

    if (existingToken && payload && !isExpired) {
      const userEmail = payload.email || 'Authenticated';
      const initial = userEmail.charAt(0).toUpperCase() || 'U';
      const avatarContent = payload.picture 
        ? `<img src="${payload.picture}" class="auth-avatar-img" alt="Avatar">` 
        : `<span class="auth-avatar-initial">${initial}</span>`;

      container.innerHTML = `
        <div class="auth-menu-wrapper">
          <button type="button" id="authAvatarBtn" class="auth-avatar-circle" title="${userEmail}" aria-expanded="false" aria-haspopup="true">
            ${avatarContent}
          </button>
          <div id="authDropdown" class="auth-dropdown-menu hidden">
            <div class="auth-dropdown-header">
              <span class="auth-dropdown-email">${userEmail}</span>
            </div>
            <div class="auth-dropdown-divider"></div>
            <button id="logoutBtn" class="auth-dropdown-item" type="button">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
                <polyline points="16 17 21 12 16 7"></polyline>
                <line x1="21" y1="12" x2="9" y2="12"></line>
              </svg>
              Log out
            </button>
          </div>
        </div>
      `;

      const avatarBtn = document.getElementById('authAvatarBtn');
      const dropdown = document.getElementById('authDropdown');

      avatarBtn?.addEventListener('click', (e) => {
        e.stopPropagation();
        const isHidden = dropdown.classList.toggle('hidden');
        avatarBtn.setAttribute('aria-expanded', !isHidden);
      });

      document.getElementById('logoutBtn')?.addEventListener('click', handleLogout);

      const closeDropdownOnOutside = (e) => {
        if (dropdown && !dropdown.classList.contains('hidden') && !container.contains(e.target)) {
          dropdown.classList.add('hidden');
          avatarBtn?.setAttribute('aria-expanded', 'false');
        }
      };

      document.removeEventListener('click', window._authDropdownClickListener);
      window._authDropdownClickListener = closeDropdownOnOutside;
      document.addEventListener('click', window._authDropdownClickListener);

    } else {
      if (window._authDropdownClickListener) {
        document.removeEventListener('click', window._authDropdownClickListener);
        window._authDropdownClickListener = null;
      }

      if (isExpired) {
        googleIdToken = null;
      }

      container.innerHTML = '';
      const targetClientId = clientId || currentGoogleClientId;
      if (window.google && google.accounts && targetClientId) {
        google.accounts.id.initialize({
          client_id: targetClientId,
          callback: (response) => {
            if (response.credential) {
              googleIdToken = response.credential;
              // No identity/PII in the params - GA is for usage counts, not
              // a record of who signed in.
              trackEvent('login', {});
              // A new user logging on takes over what was, until now, an
              // anonymous (or a different user's) session - whatever
              // prompt/SQL/results are on screen belong to that prior
              // identity, not this one, so clear them the same way a DB
              // connection change does (see clearActiveQueryState()).
              clearActiveQueryState();
              renderAuthUI(targetClientId);
              fetchBackendConfig();
            }
          }
        });

        google.accounts.id.renderButton(container, {
          theme: 'filled_black',
          size: 'medium',
          shape: 'rectangular',
          type: 'standard',
          text: 'signin',
          logo_alignment: 'left'
        });

        // Deliberately no google.accounts.id.prompt() here - on Cloud Run
        // the app supports anonymous use, so we don't want the One Tap
        // sign-in prompt popping up unasked on every load. The rendered
        // button above is always available for anyone who wants to log in.
      }
    }
  }

  function initGoogleAuth(clientId) {
    renderAuthUI(clientId);
  }

  // ===========================================================================
  // MORE MENU (triple-dot mobile header menu)
  //    Collapses Help/History/Sign-in into one dropdown under the same
  //    narrow-header breakpoint style.css uses to hide them (see
  //    NARROW_HEADER_MEDIA_QUERY below, and the @media (max-width: 480px)
  //    block in style.css). The Help/History items just forward a .click()
  //    to the real (CSS-hidden-at-this-width) header buttons, which fires
  //    their existing real listeners unchanged - no logic duplicated. The
  //    sign-in control is different: #g_id_signin holds a real, cross-origin
  //    Google Sign-In iframe (or, once signed in, our own avatar+dropdown)
  //    that can't be click-forwarded into - so instead the very same live
  //    node is physically reparented between the header and
  //    #moreMenuAuthSlot whenever the breakpoint is crossed. renderAuthUI()
  //    looks the container up by ID and only ever sets its innerHTML, so it
  //    doesn't care which parent currently holds it.
  // ===========================================================================
  const NARROW_HEADER_MEDIA_QUERY = '(max-width: 480px)';
  const moreMenuWrapper = document.getElementById('moreMenuWrapper');
  const moreMenuBtn = document.getElementById('moreMenuBtn');
  const moreMenuDropdown = document.getElementById('moreMenuDropdown');
  const moreMenuHelpBtn = document.getElementById('moreMenuHelpBtn');
  const moreMenuFeedbackBtn = document.getElementById('moreMenuFeedbackBtn');
  const moreMenuHistoryBtn = document.getElementById('moreMenuHistoryBtn');
  const moreMenuPrefsBtn = document.getElementById('moreMenuPrefsBtn');
  const moreMenuAuthSlot = document.getElementById('moreMenuAuthSlot');
  const headerActionsEl = document.querySelector('.header-actions');

  function closeMoreMenu() {
    if (!moreMenuDropdown || moreMenuDropdown.classList.contains('hidden')) return;
    moreMenuDropdown.classList.add('hidden');
    moreMenuBtn?.setAttribute('aria-expanded', 'false');
  }

  if (moreMenuBtn && moreMenuDropdown && moreMenuWrapper) {
    moreMenuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = moreMenuDropdown.classList.toggle('hidden');
      moreMenuBtn.setAttribute('aria-expanded', String(!isHidden));
    });

    document.addEventListener('click', (e) => {
      if (!moreMenuDropdown.classList.contains('hidden') && !moreMenuWrapper.contains(e.target)) {
        closeMoreMenu();
      }
    });
  }

  if (moreMenuHelpBtn) {
    moreMenuHelpBtn.addEventListener('click', () => {
      closeMoreMenu();
      helpBtn?.click();
    });
  }

  // Forwards to the real header button rather than duplicating its
  // ISSUE_REPORTING_ENABLED visibility gate or its click wiring - see
  // fetchBackendConfig(), which toggles both this item and #sendFeedbackBtn
  // together, and the comment on the sendFeedbackBtn const above.
  if (moreMenuFeedbackBtn) {
    moreMenuFeedbackBtn.addEventListener('click', () => {
      closeMoreMenu();
      sendFeedbackBtn?.click();
    });
  }

  if (moreMenuHistoryBtn) {
    moreMenuHistoryBtn.addEventListener('click', () => {
      closeMoreMenu();
      historyBtn?.click();
    });
  }

  if (moreMenuPrefsBtn) {
    moreMenuPrefsBtn.addEventListener('click', () => {
      closeMoreMenu();
      prefsBtn?.click();
    });
  }

  // Keeps the live #g_id_signin node in the right place as the viewport
  // crosses the narrow-header breakpoint - see the block comment above.
  function relocateAuthContainer(isNarrow) {
    const authContainer = document.getElementById('g_id_signin');
    if (!authContainer || !headerActionsEl || !moreMenuAuthSlot || !moreMenuWrapper) return;
    if (isNarrow) {
      if (authContainer.parentElement !== moreMenuAuthSlot) {
        moreMenuAuthSlot.appendChild(authContainer);
      }
    } else {
      if (authContainer.parentElement !== headerActionsEl) {
        headerActionsEl.insertBefore(authContainer, moreMenuWrapper);
      }
      closeMoreMenu();
    }
  }

  if (moreMenuWrapper) {
    const narrowHeaderQuery = window.matchMedia(NARROW_HEADER_MEDIA_QUERY);
    relocateAuthContainer(narrowHeaderQuery.matches);
    const handleNarrowHeaderChange = (e) => relocateAuthContainer(e.matches);
    if (narrowHeaderQuery.addEventListener) {
      narrowHeaderQuery.addEventListener('change', handleNarrowHeaderChange);
    } else if (narrowHeaderQuery.addListener) {
      // Safari <14 / older WebKit fallback.
      narrowHeaderQuery.addListener(handleNarrowHeaderChange);
    }
  }

  // ===========================================================================
  // 4. SHARED UI HELPERS
  //    (button/textarea state, SQL formatting/display, results-display
  //    resets, history-nav button state, live DB connection status)
  // ===========================================================================
  function setButtonsDisabled(disabled) {
    if (translateBtn) translateBtn.disabled = disabled;
    if (runBtn) runBtn.disabled = disabled;
    if (micBtn) micBtn.disabled = disabled;
    if (reportSqlBtn) reportSqlBtn.disabled = disabled;
    // Disable the NL prompt box itself while a translate/execute call is
    // in flight - previously only the trigger buttons were disabled, so
    // the box stayed editable (misleadingly implying a fresh edit could
    // still do something) and, worse, its own Enter-key handler could
    // still fire. A disabled textarea can't be focused or receive
    // keyboard events at all, so this closes that off entirely rather
    // than relying solely on the `translateBtn.disabled` check inside
    // that handler.
    if (aiPrompt) aiPrompt.disabled = disabled;
    // The SQL editor itself, same reasoning as aiPrompt just above - and
    // the SAME disabled/re-enabled window, which matters here specifically:
    // setButtonsDisabled(true) fires before translatePrompt() even starts
    // fetching, and setButtonsDisabled(false) only fires once the whole
    // turn is over (translation, then auto-execute, then - in "all
    // databases" mode - Phase C's summary - see translatePrompt()'s outer
    // finally). So the box stays read-only for the entire time in between,
    // including the moment setSqlQuery(data.sql) fills it in mid-turn:
    // without this, the freshly-generated SQL was immediately editable
    // even though auto-execute was often still running against the
    // ORIGINAL text, silently editing "results still in flight" SQL that
    // had nothing to do with what was about to be (or already being)
    // executed. readOnly (not 'nocursor') still allows selecting/copying
    // the SQL while it's inactive, just not typing into it.
    if (sqlEditor) {
      sqlEditor.setOption('readOnly', disabled);
      sqlEditor.getWrapperElement().classList.toggle('cm-readonly', disabled);
    }
    // Example prompt chips: queried live (rather than via the
    // examplePromptButtons closure declared further down) so this works
    // regardless of where in the file setButtonsDisabled is called from.
    // Without this, clicking one chip while its translation is still in
    // flight let someone click a second (or third) chip and stack up
    // overlapping requests.
    document.querySelectorAll('.example-chip').forEach(btn => {
      btn.disabled = disabled;
    });
    document.body.style.cursor = disabled ? 'wait' : 'default';
    // Cancel button: only ever shown/enabled while something's actually in
    // flight - it's the inverse of every other control toggled above.
    if (stopBtn) stopBtn.classList.toggle('hidden', !disabled);

    // DB connection / model badges: opening either popup mid-turn would let
    // someone switch the active connection or model out from under a
    // request that's already running against the OLD one - the same
    // "don't let the ground shift under an in-flight turn" reasoning as
    // locking aiPrompt/sqlEditor above, just for a different pair of
    // controls. badge-disabled is an existing (previously unused) "grayed
    // out, not-allowed cursor" style - see its own comment in style.css,
    // written for a different, still-unwired anonymous-user scenario, but
    // the visual is exactly right here too, so it's reused rather than
    // adding a near-identical second class. The badges are plain <div>s
    // (no native `disabled`), so the click handlers themselves check for
    // this class and no-op - see modelTriggerBadge's/configTriggerBadge's
    // own 'click' listeners below. The doc/history/preferences icons next
    // to them are deliberately left alone: none of their popups touch the
    // active connection, model, or any state an in-flight turn depends on.
    if (configTriggerBadge) configTriggerBadge.classList.toggle('badge-disabled', disabled);
    if (modelTriggerBadge) modelTriggerBadge.classList.toggle('badge-disabled', disabled);

    // Sign-in/sign-out control: signing in or out mid-turn tears down the
    // whole active turn out from under it (see auth-disabled's own comment
    // in style.css for exactly what renderAuthUI()'s sign-in callback and
    // handleLogout() each do) - previously fully clickable throughout, with
    // "unpredictable" results. Queried live rather than cached at the top
    // of the file - same reasoning as the example-chip lookup above: this
    // container's own node persists for the page's whole life (only its
    // innerHTML is rebuilt, by renderAuthUI()), but querying it fresh here
    // means this still works regardless of where in the file
    // setButtonsDisabled() is called from.
    const authContainer = document.getElementById('g_id_signin');
    if (authContainer) authContainer.classList.toggle('auth-disabled', disabled);

    if (disabled) {
      if (goBackBtn) goBackBtn.disabled = true;
      if (goForwardBtn) goForwardBtn.disabled = true;
    } else {
      // Re-enabling: defer to the boundary logic rather than unconditionally
      // turning them back on (e.g. stay disabled if already at the oldest turn).
      updateHistoryNavButtons();
    }
  }

  function getSqlQuery() {
    return sqlEditor ? sqlEditor.getValue().trim() : (sqlQueryTextarea ? sqlQueryTextarea.value.trim() : '');
  }

  function formatSql(sql) {
    if (window.sqlFormatter && typeof window.sqlFormatter.format === 'function') {
      try {
        return window.sqlFormatter.format(sql, { language: 'postgresql' });
      } catch (err) {
        console.warn('SQL formatting failed, returning raw SQL:', err);
        return sql;
      }
    }
    return sql;
  }

  function setSqlQuery(val) {
    const formattedVal = val ? formatSql(val) : '';
    if (sqlEditor) {
      sqlEditor.setValue(formattedVal);
      requestAnimationFrame(() => {
        sqlEditor.refresh();
      });
    } else if (sqlQueryTextarea) {
      sqlQueryTextarea.value = formattedVal;
    }
  }

  function clearResultsDisplay() {
    hideRetryStatus();
    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) resultsBody.innerHTML = '';
    currentResultsList = [];
    activeResultIndex = 0;
    setReportContext(null);
  }

  // Shown at the top of the results area (above the tabs/table, see
  // index.html) while /api/translate is working through its one
  // server-side retry loop (translate_routes.py's stream_translation() -
  // see the comment above readTranslateStream() below for why this is the
  // only retry loop left after removing the client-side one that used to
  // duplicate it). Cleared by clearResultsDisplay() so it never lingers
  // into a fresh translate/execute call or a connection switch.
  function showRetryStatus({ attempt, maxAttempts, rotatedKey }) {
    if (!resultsRetryStatus) return;
    // Provider-neutral wording: this banner now covers both providers'
    // shared transient-error retries (translate_routes.py's
    // MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS) as well as
    // Gemini's own key-rotation retries (rotatedKey: true) - the latter is
    // Gemini-exclusive (see _classify_claude_error's docstring), so
    // "switching to a different API key" is only ever shown for Gemini in
    // practice, but the message itself no longer hardcodes "Gemini" since
    // a plain transient retry can happen for either provider.
    const keyNote = rotatedKey ? ', switching to a different API key' : '';
    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> ` +
      `Translation hit a transient error${keyNote} - retrying (attempt ${attempt} of ${maxAttempts})...`;
    resultsRetryStatus.classList.remove('hidden');
  }

  function hideRetryStatus() {
    if (!resultsRetryStatus) return;
    resultsRetryStatus.classList.add('hidden');
    resultsRetryStatus.innerHTML = '';
  }

  // Single-connection-mode progress label ("Reading the database schema…",
  // then "Generating commands for the database…" - see
  // translate_routes.py's stream_translation() docstring for the
  // "phase_status" event this renders). Reuses the same banner element/
  // styling as showRetryStatus()/showAllModeStreamStatus() above rather
  // than adding a second element - this is never shown at the same time as
  // either of those (all three are mutually exclusive server-side response
  // shapes), so there's no risk of them treading on each other. Unlike
  // showRetryStatus(), there's no "attempt X of Y" counter here - just a
  // plain label naming which of the two pre-LLM-call waits is currently
  // happening, since neither wait has a meaningful progress count of its
  // own. Once /api/translate's stream ends, this same banner element is
  // reused again for "Fetching results from the database…" - see
  // showFetchingResultsStatus() below, covering the THIRD real wait
  // (submitting the generated SQL to the actual database and waiting on
  // it), which previously had no indicator of any kind once the SQL
  // arrived.
  function showPhaseStatus(evt) {
    if (!resultsRetryStatus) return;
    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> ${evt.message}`;
    resultsRetryStatus.classList.remove('hidden');
  }

  // Single-connection mode's own execution-wait indicator - shown by
  // executeSql() around its /api/execute call, but ONLY when this isn't
  // an "all databases" mode turn (that mode has its own, per-connection
  // progress banner - see showAllModeStreamStatus()/showAllModeSummarizing
  // Status() above). /api/execute isn't itself streamed (unlike /api/
  // translate), so there's no server-driven progress here - just a static
  // label for the one real wait (the query actually running against the
  // database) that used to leave nothing on screen at all once the SQL
  // had already landed in the (now read-only) editor.
  function showFetchingResultsStatus() {
    if (!resultsRetryStatus) return;
    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> Fetching results from the database…`;
    resultsRetryStatus.classList.remove('hidden');
  }

  // /api/translate streams newline-delimited JSON (see
  // translate_routes.py's module docstring): zero or more
  // {"status": "retrying", ...} progress lines emitted live as the
  // server's one Gemini-call retry loop runs, plus - for the
  // single-connection path only - two {"status": "phase_status", "phase":
  // "schema"|"generating_sql", "message": ...} lines emitted once each,
  // ahead of the schema lookup and the LLM call respectively (see
  // showPhaseStatus() above) - or, for the "all databases" mode "route"
  // outcome only, one {"status": "phase_a_route", ...} line followed by
  // one {"status": "phase_b_connection_done", ...} line per selected
  // connection (see translate_routes.py's stream_translation() docstring).
  // Followed in every case by exactly one terminal {"status": "done",
  // success, sql/error, ...token usage...} line - the same shape
  // /api/translate used to return as its whole body before streaming
  // existed. A request that never reaches that retry loop at all (missing
  // prompt/API key, a 401 from the auth guard, or a mocked response in
  // tests - see fixtures.js's mockTranslate()) isn't streamed - it's still
  // a single plain JSON object, which this reads exactly the same way: one
  // line, no "status" field, straight into finalData.
  //
  // `onEvent`, if given, is called for every line EXCEPT the terminal
  // 'done' one, in arrival order, as soon as each is parsed - this is
  // what lets a caller react to 'retrying'/'phase_a_route'/
  // 'phase_b_connection_done' lines live rather than only after the
  // whole stream has finished (this function's own return value is
  // still just the terminal line, same as before onEvent existed).
  async function readTranslateStream(response, onEvent) {
    if (!response.body || !response.body.getReader) {
      // No ReadableStream support (very old browser) - fall back to a
      // single json() read. No live progress/streaming events in that
      // case, but still functionally correct once the whole body has
      // arrived.
      return response.json();
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalData = null;

    const consumeLine = (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let parsed;
      try {
        parsed = JSON.parse(trimmed);
      } catch (err) {
        console.warn('Failed to parse a line of the /api/translate stream:', trimmed, err);
        return;
      }
      const isProgressLine = parsed.status === 'retrying'
        || parsed.status === 'phase_status'
        || parsed.status === 'phase_a_route'
        || parsed.status === 'phase_b_connection_done';
      if (isProgressLine) {
        // 'retrying' is today's only pre-existing progress line;
        // 'phase_status'/'phase_a_route'/'phase_b_connection_done' are new
        // (see this function's docstring above) - and, going forward, any
        // other intermediate status this stream ever grows can be handled
        // the same way without this function needing to know about it by
        // name.
        if (onEvent) onEvent(parsed);
      } else {
        // The terminal 'done' line, or (old-browser/mocked-response
        // fallback) a single-line body with no "status" field at all -
        // either way, this becomes the function's return value, same as
        // before onEvent existed.
        finalData = parsed;
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: true });

      let newlineIndex;
      while ((newlineIndex = buffer.indexOf('\n')) !== -1) {
        consumeLine(buffer.slice(0, newlineIndex));
        buffer = buffer.slice(newlineIndex + 1);
      }

      if (done) {
        buffer += decoder.decode();
        consumeLine(buffer);
        break;
      }
    }

    return finalData || {};
  }

  // Wipes everything tied to the connection that was just switched away
  // from: the NL prompt, the generated SQL, the results grid, and the
  // turn-navigation history (chatStore) - without clearing chatStore too,
  // clicking "go back" after a connection change would silently restore
  // the previous connection's prompt/SQL/results, defeating the point of
  // clearing them here. Called from triggerConfigSave() only when the
  // active connection identity actually changed (not on every save, e.g.
  // re-saving the same connection or toggling auto-execute).
  function clearActiveQueryState() {
    if (aiPrompt) aiPrompt.value = '';
    setSqlQuery('');
    clearResultsDisplay();
    chatStore.clear();
    updateHistoryTurnsSubtitle();
    // A pinned multi-database selection only ever makes sense for the
    // conversation it was picked for - every existing trigger for this
    // function (new chat, logout, sign-in, connection-identity change) is
    // already exactly the boundary a pin should reset at, so this rides
    // along with zero new call sites.
    PINNED_CONNECTIONS = [];
  }

  function updateHistoryTurnsSubtitle() {
    const clearMsgEl = document.getElementById('historyActionMsg');
    if (clearMsgEl) {
      clearMsgEl.textContent = '';
    }
    updateHistoryNavButtons();
  }

  function updateHistoryNavButtons() {
    // chatStore holds [user, model] pairs. When only one turn remains,
    // it's already the oldest turn on screen - going back from there would
    // pop it and leave the UI blank, so disable one step early.
    const atOldestTurn = !chatStore.canUndo();
    const atNewestTurn = !chatStore.canRedo();

    if (goBackBtn) {
      goBackBtn.disabled = atOldestTurn;
      goBackBtn.classList.toggle('is-boundary', atOldestTurn);
      goBackBtn.title = atOldestTurn ? "No earlier turns" : "Go back to previous turn";
    }
    if (goForwardBtn) {
      goForwardBtn.disabled = atNewestTurn;
      goForwardBtn.classList.toggle('is-boundary', atNewestTurn);
      goForwardBtn.title = atNewestTurn ? "No later turns" : "Go forward to next turn";
    }
  }

  // Fires the real liveness check (a genuine connect() + query against
  // whatever database is active - see /api/ping's own docstring) and
  // updates the header dot whenever it resolves. Deliberately called
  // WITHOUT awaiting it from every call site below (updateConnectionDetails())
  // - a slow/unreachable connection used to make the config modal's open
  // and Save actions hang for however long /api/ping took (up to
  // DB_CONNECT_TIMEOUT_SECONDS, ~10s by default, per statement), since
  // those flows used to `await` this before letting the modal become
  // visible/closing it. Now the modal opens/closes immediately and this
  // keeps running in the background, updating the dot in place once it's
  // done - same end result, just never blocking the UI on it.
  async function checkDbStatus() {
    if (!connDbDot) return;

    // Immediate feedback that a (re)check is now in flight, rather than
    // leaving the previous connected/disconnected state up for however
    // long this background check takes - see the "checking" style's own
    // comment in style.css.
    connDbDot.className = 'status-dot checking';

    try {
      // /api/ping (not /api/execute with a hardcoded query string) - no
      // single SQL text is valid across every dialect this app supports.
      // The previous "SELECT current_user, current_database();" was
      // Postgres-specific and always failed against BigQuery (no
      // current_database() function there); the "SELECT 1;" that replaced
      // it was itself later found to always fail against Oracle (no
      // SELECT-without-FROM form there). Both permanently showed the
      // badge as disconnected on a perfectly working connection. Rather
      // than clientside guess yet another string that happens to work for
      // whatever dialects exist today, the server resolves the active
      // connection's actual backend and asks it for its own
      // always-correct liveness_sql (see backends/base.py) - the same
      // place every other per-dialect SQL quirk in this app already
      // lives, not duplicated here.
      const response = await fetch('/api/ping', {
        method: 'GET',
        headers: getApiHeaders(),
        credentials: 'same-origin',
      });

      const data = await response.json();
      if (response.ok && data.success) {
        connDbDot.className = 'status-dot connected';
      } else {
        connDbDot.className = 'status-dot disconnected';
      }
    } catch (err) {
      connDbDot.className = 'status-dot disconnected';
    }
  }

  // Multi-database question-answering (see server/translate_routes.py's
  // module docstring): the badge has room for exactly one name, but 2+
  // connections can now be in scope at once - showing just the primary's
  // name in that case silently implies the OTHER in-scope connection(s)
  // don't exist, which is exactly the confusion a user checking 2+ boxes
  // in the connection picker and then seeing only one name in the badge
  // would run into. Returns {count, label, names} - `label` is what the
  // badge text should show (the primary's own name when count <= 1,
  // "All databases" otherwise) and `names` is the full in-scope name list
  // (resolved via configured_databases/custom_databases, both already
  // present on every /api/config response) for the tooltip.
  //
  // "All" (data.in_scope_mode === 'all') is checked FIRST and directly -
  // the same source of truth isAllConnectionsSelected() uses - rather
  // than inferred from in_scope_preset_ids/in_scope_custom_connection_keys'
  // combined length the way the fallback branch below still does for a
  // legacy multi-select session. Those two arrays are NOT what decides
  // "all" mode (see db.py's resolve_in_scope_descriptors: "all" ignores
  // them entirely in favor of dynamically resolving every currently-
  // configured connection) and can be arbitrarily short - even a single
  // leftover entry from whatever was last explicitly picked before "All"
  // was selected (see triggerConfigSave(): picking "All" leaves them
  // untouched rather than sending fresh ones) - so counting them would
  // wrongly show just one connection's name for a session genuinely in
  // "all" mode, exactly the bug this once had.
  function summarizeInScopeConnections(data) {
    const configuredDbs = data?.configured_databases || [];
    const customDbs = data?.custom_databases || [];
    if (data?.in_scope_mode === 'all') {
      const names = [...configuredDbs.map(db => db.name), ...customDbs.map(db => db.name)];
      return { count: names.length, label: names.length > 1 ? 'All databases' : null, names };
    }
    const presetIds = data?.in_scope_preset_ids || [];
    const customKeys = data?.in_scope_custom_connection_keys || [];
    const count = presetIds.length + customKeys.length;
    if (count <= 1) return { count, label: null, names: [] };
    // A legacy session that saved an arbitrary multi-connection subset
    // before the binary single/all choice existed (see
    // resolve_in_scope_descriptors' docstring) - still more than one
    // connection in scope, so this reuses the same "All databases" badge
    // text as real "all" mode above (this badge has no separate copy for
    // "several specific connections"), just resolved from the explicit
    // arrays instead of the full configured/custom lists.
    const names = [
      ...presetIds.map(id => configuredDbs.find(db => db.id === id)?.name || id),
      ...customKeys.map(key => customDbs.find(db => db.connection_key === key)?.name || key),
    ];
    return { count, label: 'All databases', names };
  }

  async function updateConnectionDetails(data) {
    const badge = document.getElementById('configTriggerBadge');
    const inScopeSummary = summarizeInScopeConnections(data);

    if (isAnonymousUser && !data?.active_is_custom) {
      // The backend withholds a PRESET's username/connection string from
      // anonymous requests (an admin's credential, not the visitor's own),
      // but does send back its display name (e.g. "Demo") in
      // data.database_name since that's just a label, not a credential.
      // This only applies while the anonymous visitor is actually ON a
      // preset, though - once they're on their own self-supplied custom
      // connection (active_is_custom), there's nothing of theirs being
      // hidden from them, so that falls through to the same real-details
      // path an authenticated user gets, below.
      if (badge) badge.style.display = '';
      const anonDbLabel = inScopeSummary.label || data?.database_name || 'Database';
      if (connDbName) {
        connDbName.textContent = data?.active_connection_missing ? `⚠ ${anonDbLabel}` : anonDbLabel;
      }
      if (configTriggerBadge) {
        configTriggerBadge.title = data?.active_connection_missing
          ? (data.active_connection_missing_message || anonDbLabel)
          : inScopeSummary.count > 1
            ? `In scope: ${inScopeSummary.names.join(', ')} (Click to configure)`
            : `Connected to: ${anonDbLabel} (Click to configure)`;
      }
      document.title = `Datalect`;
      // Deliberately not awaited - see checkDbStatus()'s own comment on
      // why this must never block the modal open/Save flow that calls
      // into this function.
      checkDbStatus();
      return;
    }

    if (!data?.database_name && !data?.custom_database_name) {
      if (badge) badge.style.display = 'none';
      return;
    }

    if (badge) badge.style.display = '';

    const matchedPreset = CONFIGURED_DBS.find(db => db.id === data.active_preset_id);
    // Matching by the preset's stable "id" (not URL) also works for
    // anonymous users, whose CONFIGURED_DBS entries never carry a "url" at
    // all (see the redacted configured_databases the server sends them). A
    // custom connection's URL can still collide with a preset's, so
    // active_is_custom (the server's record of which one the user actually
    // picked) breaks the tie - without it, a colliding preset match would
    // always win here even when the user explicitly selected their own
    // custom connection with the same URL.
    const primaryDisplayName = data.active_is_custom
      ? (data.custom_database_name || data.database_name || "Database")
      : (matchedPreset?.name || data.database_name || "Database");
    // 2+ connections in scope (see summarizeInScopeConnections above) -
    // the badge shows a count instead of just the primary's name, since
    // showing only one name would silently hide that other connection(s)
    // are also in play for this session's questions.
    const dbDisplayName = inScopeSummary.label || primaryDisplayName;

    // A previously-selected preset/custom connection that's since been
    // removed or renamed still resolves to a real (default) connection -
    // see db.py's resolve_active_descriptor - but the badge should say so
    // rather than silently showing the default as if it were what the
    // user actually picked (see config_routes.py's
    // active_connection_missing/_message).
    if (data.active_connection_missing) {
      if (configTriggerBadge) {
        configTriggerBadge.title = data.active_connection_missing_message || `Connected to: ${dbDisplayName} (Click to configure)`;
      }
      if (connDbName) {
        connDbName.textContent = `⚠ ${dbDisplayName}`;
      }
      document.title = `Datalect`;
      // Deliberately not awaited - see checkDbStatus()'s own comment on
      // why this must never block the modal open/Save flow that calls
      // into this function.
      checkDbStatus();
      return;
    }

    if (configTriggerBadge) {
      configTriggerBadge.title = inScopeSummary.count > 1
        ? `In scope: ${inScopeSummary.names.join(', ')} (Click to configure)`
        : `Connected to: ${dbDisplayName} (Click to configure)`;
    }

    if (connDbName) {
      connDbName.textContent = dbDisplayName;
    }

    document.title = `Datalect`;

    // Deliberately not awaited - see checkDbStatus()'s own comment on why
    // this must never block the modal open/Save flow that calls into
    // this function.
    checkDbStatus();
  }

  function updateModelBadge() {
    if (!modelBadgeName) return;
    // ACTIVE_LLM_MODEL alone (not "provider/model") - the provider is
    // implied by which model is showing, and the modal (grouped by
    // provider heading) is where that grouping actually matters; the badge
    // itself just needs to answer "what model am I using right now" at a
    // glance, same one-value-only spirit as the DB badge's connDbName.
    modelBadgeName.textContent = ACTIVE_LLM_MODEL || "Model";
    if (modelTriggerBadge) {
      modelTriggerBadge.title = ACTIVE_LLM_MODEL
        ? `Using model: ${ACTIVE_LLM_MODEL} (Click to configure)`
        : 'Model Info (Click to configure)';
    }
  }

  // ===========================================================================
  // 5. BACKEND CONFIG SYNC + DATABASE CONNECTION CONFIG MODAL
  //    (fetch /api/config, render preset/custom DB radio options, save
  //    connection + auto-execute preference, config modal open/close)
  // ===========================================================================
  async function fetchBackendConfig() {
    try {
      const response = await fetch('/api/config', { headers: getApiHeaders(), credentials: 'same-origin' });
      const data = await response.json();

      isAnonymousUser = Boolean(data && data.is_cloud_run && !data.authenticated);
      updateAnonymousRestrictions();

      CONFIGURED_DBS = data.configured_databases || [];
      DEFAULT_DB_URL = data.default_database_url || "";
      ACTIVE_DB_URL = data.active_database_url || DEFAULT_DB_URL;
      
      if (data.custom_database_name !== undefined) {
        customDbName = data.custom_database_name;
      }
      if (data.custom_database_url !== undefined) {
        customDbUrl = data.custom_database_url;
      }
      if (data.custom_databases !== undefined) {
        customDatabases = data.custom_databases;
      } else if (customDbUrl) {
        customDatabases = [{ name: customDbName || "Custom", type: 'postgres', url: customDbUrl, config: {} }];
      } else {
        customDatabases = [];
      }

      if (data.auto_sql_execute !== undefined) {
        autoSqlExecuteEnabled = Boolean(data.auto_sql_execute);
      }

      // Theme (Preferences modal's color-scheme choice) is now persisted
      // server-side (session, or user if logged in - see state_store.py),
      // not just in localStorage. A real "dark"/"light" value here means
      // the user explicitly saved a preference at some point, so it wins
      // over whatever's currently applied (e.g. a fresh browser/device
      // with no localStorage entry of its own, or a stale localStorage
      // value from before this account last saved a different choice) -
      // reapplying via setTheme() also re-syncs localStorage, so the next
      // page load's flash-prevention script (index.html's inline <head>
      // script, which only ever reads localStorage before this fetch can
      // resolve) picks up the right value too. A blank value ("" - never
      // explicitly saved) deliberately leaves the current theme alone,
      // whatever localStorage/the default already applied for first paint.
      if (data.theme === 'dark' || data.theme === 'light') {
        if (getCurrentTheme() !== data.theme) {
          setTheme(data.theme);
        }
      }

      // Keeps the turn-navigation cap in lockstep with HISTORY_MAX_TURNS,
      // the same env var /api/translate uses to decide how many past
      // turns actually reach the LLM (see createChatHistoryStore's
      // setMaxTurns() for why this can't just be a hardcoded constant).
      chatStore.setMaxTurns(data.history_max_turns);

      if (data.auth_enabled && data.google_client_id) {
        googleAuthEnabled = true;
        initGoogleAuth(data.google_client_id);
      }

      if (data.active_database_url) {
        ACTIVE_DB_URL = data.active_database_url;
      } else if (!ACTIVE_DB_URL && DEFAULT_DB_URL) {
        ACTIVE_DB_URL = DEFAULT_DB_URL;
      }
      ACTIVE_IS_CUSTOM = Boolean(data.active_is_custom);
      ACTIVE_DB_TYPE = data.active_database_type || "";
      ACTIVE_CUSTOM_CONNECTION_KEY = data.active_custom_connection_key || "";
      ACTIVE_USES_CUSTOM_CREDENTIALS = Boolean(data.active_uses_custom_credentials);
      ACTIVE_PRESET_ID = data.active_preset_id ?? null;

      LLM_PROVIDERS = data.llm_providers || [];
      ACTIVE_LLM_PROVIDER = data.active_llm_provider || "";
      ACTIVE_LLM_MODEL = data.active_llm_model || "";
      LLM_BYOK_KEY_SET = data.llm_byok_key_set || { google: false, anthropic: false, openai: false };

      // No re-render needed here for the inline Report buttons -
      // reportButtonHtml() (called from inside renderTableResult()/
      // renderNoSqlResponse()/executeSql()'s own per-tab rendering) reads
      // this fresh at the moment each result is actually drawn, which
      // always happens well after this initial config fetch resolves. The
      // The header's "Send Feedback" button (and its narrow-screen
      // more-menu twin) IS a persistent element though (see sendFeedbackBtn's
      // own comment on why it isn't rendered inline the way the others
      // are), so it needs an explicit toggle here instead.
      ISSUE_REPORTING_ENABLED = Boolean(data.issue_reporting_enabled);
      if (sendFeedbackBtn) sendFeedbackBtn.classList.toggle('hidden', !ISSUE_REPORTING_ENABLED);
      if (moreMenuFeedbackBtn) moreMenuFeedbackBtn.classList.toggle('hidden', !ISSUE_REPORTING_ENABLED);
      // Same gate for the SQL box's "report wrong SQL" thumbs-down button -
      // it's just as persistent an element as the two above.
      if (reportSqlBtn) reportSqlBtn.classList.toggle('hidden', !ISSUE_REPORTING_ENABLED);

      IN_SCOPE_PRESET_IDS = data.in_scope_preset_ids || [];
      IN_SCOPE_CUSTOM_KEYS = data.in_scope_custom_connection_keys || [];
      IN_SCOPE_MODE = data.in_scope_mode === 'all' ? 'all' : 'single';
      if (data.max_in_scope_connections) {
        MAX_IN_SCOPE_CONNECTIONS = data.max_in_scope_connections;
      }

      renderDbRadioButtons();
      loadConfigIntoUI();

      await updateConnectionDetails(data);
      updateModelBadge();
    } catch (err) {
      console.error("Failed to fetch backend configuration:", err);
      if (connDbDot) connDbDot.className = 'status-dot disconnected';
    }
  }

  function makeEmptyCustomDb(type) {
    if (type === 'bigquery') {
      return { name: '', type: 'bigquery', url: '', config: { project_id: '', dataset: '', billing_project_id: '', credentials_json: '' } };
    }
    if (type === 'snowflake') {
      // auth_method is UI-only state (not a server field) deciding which
      // of password/private_key gets sent - see the sf-auth-method select
      // handler in renderCustomDbRows below.
      return {
        name: '', type: 'snowflake', url: '',
        config: {
          account: '', user: '', warehouse: '', database: '', schema: '', role: '',
          auth_method: 'password', password: '', private_key: '', private_key_passphrase: '',
        },
      };
    }
    if (type === 'databricks') {
      return {
        name: '', type: 'databricks', url: '',
        config: { server_hostname: '', http_path: '', catalog: '', schema: '', access_token: '' },
      };
    }
    if (type === 'oracle') {
      return {
        name: '', type: 'oracle', url: '',
        // ssl defaults to true (unlike every other field here) - most
        // Oracle connections added through this dialog target Oracle
        // Cloud, which requires it (see backends/oracle.py's module
        // docstring); a plain on-prem/XE listener is the exception, not
        // the common case, so it's opt-out rather than opt-in here.
        config: { host: '', port: '', service_name: '', sid: '', schema: '', user: '', password: '', ssl: true },
      };
    }
    if (type === 'redshift') {
      // No "ssl" field, unlike Oracle's - Redshift connections always
      // require TLS (see backends/redshift.py's connect()), so there's no
      // per-connection choice to expose here.
      return {
        name: '', type: 'redshift', url: '',
        config: { host: '', port: '', database: '', schema: '', user: '', password: '' },
      };
    }
    if (type === 'mssql') {
      // "encrypt" defaults to true (same opt-out-not-opt-in rationale as
      // Oracle's "ssl" above) - most real SQL Server deployments, and
      // Azure SQL Database in particular, require encryption outright, so
      // a connection that leaves it unset would simply fail to connect at
      // all (see backends/mssql.py's module docstring).
      return {
        name: '', type: 'mssql', url: '',
        config: { host: '', port: '', database: '', schema: '', user: '', password: '', encrypt: true },
      };
    }
    if (type === 'sheets') {
      // credentials_json is optional here, unlike every credentialed
      // dialect above - a blank value keeps this connection reaching only
      // a genuinely public spreadsheet (see backends/sheets.py's module
      // docstring); a pasted service-account key is what unlocks a
      // private, explicitly-shared one.
      return {
        name: '', type: 'sheets', url: '',
        config: { spreadsheet_url: '', tab_name: '', credentials_json: '' },
      };
    }
    if (type === 'MongoDB') {
      // Unlike Postgres/MySQL just below, MongoDB has a real url (the
      // bare mongodb:// URI) PLUS separate structured config fields - see
      // backends/mongodb_sql.py's and config_routes.py's module
      // docstrings for why it's a hybrid of the two shapes.
      return {
        name: '', type: 'MongoDB', url: '',
        config: { database: '', user: '', password: '' },
      };
    }
    // Postgres and MySQL share the same simple shape (a single URL field,
    // no dialect-specific config) - see backends/mysql.py's module
    // docstring - so both fall through here, preserving whichever was
    // actually selected rather than collapsing MySQL into Postgres. Any
    // other/unrecognized value (there shouldn't be one - the dropdown
    // only ever offers these nine types) also lands on Postgres, matching
    // this function's original default.
    return {
      name: '',
      type: (type === 'mysql') ? type : 'postgres',
      url: '',
      config: {},
    };
  }

  // Renders every entry in `customDatabases` (including in-progress blank
  // rows added via "+ Add custom connection") as an editable row with a
  // dialect selector. Each row's inputs keep `customDatabases[index]` in
  // sync live via their own 'input' listeners, so by the time
  // triggerConfigSave() runs there's nothing left to harvest from the DOM.
  function renderCustomDbRows(activeUrl) {
    const container = document.getElementById('customDbsContainer');
    if (!container) return;

    const allSelected = isAllConnectionsSelected();

    // Focusing/editing a custom row's own field checks that row's radio -
    // true radio semantics (this is a single-select group again, see
    // renderDbRadioButtons()) mean that alone is enough to uncheck
    // whatever else was checked, so there's nothing else to track here.
    function selectDbConnectionRow(radio) {
      if (radio) radio.checked = true;
    }

    let html = '';
    customDatabases.forEach((db, index) => {
      const cfg = db.config || {};
      const isBigQuery = db.type === 'bigquery';
      const isSnowflake = db.type === 'snowflake';
      const isMySQL = db.type === 'mysql';
      const isDatabricks = db.type === 'databricks';
      const isOracle = db.type === 'oracle';
      const isRedshift = db.type === 'redshift';
      const isSqlServer = db.type === 'mssql';
      const isSheets = db.type === 'sheets';
      const isMongoSql = db.type === 'MongoDB';
      const sfAuthMethod = cfg.auth_method || (cfg.private_key ? 'private_key' : 'password');
      // Checked state comes from the in-scope set (see IN_SCOPE_CUSTOM_KEYS'
      // docstring) matched by connection_key - but, same as a preset
      // option above, only when "All" isn't the current selection (see
      // isAllConnectionsSelected()). Falls back to the legacy single-
      // active-connection URL match only for a row with no connection_key
      // at all (saved before that field existed on individual rows).
      const isSelected = !allSelected && (db.connection_key
        ? IN_SCOPE_CUSTOM_KEYS.includes(db.connection_key)
        : (ACTIVE_IS_CUSTOM && !ACTIVE_CUSTOM_CONNECTION_KEY && Boolean(db.url) && activeUrl === db.url));

      // A connection_key is only ever present on a row that came back from
      // the server (see config_routes.py's get_db_connections) - a row
      // just added via "+ Add custom connection" this session never has
      // one. That's the signal for "previously configured and shown so it
      // can be selected": those default to collapsed (just type/name, an
      // expand toggle, and remove), since their details aren't needed to
      // pick them. A brand-new row defaults to expanded instead, since
      // there's nothing to select yet without filling it in. Either way,
      // _expanded (a client-only field, never sent to the server) tracks
      // an explicit user override once they've toggled it, surviving
      // re-renders since it lives on the object itself rather than index.
      const isExpanded = db._expanded !== undefined ? db._expanded : !db.connection_key;

      // Row 1 (all types): selection radio, dialect select, and Name -
      // dialect-specific fields live on their own dedicated rows below,
      // never crowding this first line.
      html += `
        <div class="custom-db-card">
          <div class="custom-db-header-row">
            <input type="radio" name="db_connection_option" value="custom-${index}" data-dbname="${db.name || ''}" ${isSelected ? 'checked' : ''}>
            <select class="config-input custom-db-type-select" data-index="${index}">
              <option value="postgres" ${(!isBigQuery && !isSnowflake && !isMySQL && !isDatabricks && !isOracle && !isRedshift && !isSqlServer && !isSheets && !isMongoSql) ? 'selected' : ''}>PostgreSQL</option>
              <option value="mysql" ${isMySQL ? 'selected' : ''}>MySQL</option>
              <option value="bigquery" ${isBigQuery ? 'selected' : ''}>BigQuery</option>
              <option value="snowflake" ${isSnowflake ? 'selected' : ''}>Snowflake</option>
              <option value="databricks" ${isDatabricks ? 'selected' : ''}>Databricks</option>
              <option value="oracle" ${isOracle ? 'selected' : ''}>Oracle</option>
              <option value="redshift" ${isRedshift ? 'selected' : ''}>Redshift</option>
              <option value="mssql" ${isSqlServer ? 'selected' : ''}>SQL Server</option>
              <option value="sheets" ${isSheets ? 'selected' : ''}>Google Sheets</option>
              <option value="MongoDB" ${isMongoSql ? 'selected' : ''}>MongoDB</option>
            </select>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-name-${index}">Name:</label>
              <input type="text" id="custom-db-name-${index}" class="config-input custom-db-name-input" data-index="${index}" placeholder="e.g. My Database" value="${db.name || ''}" autocomplete="off">
            </div>
            <button type="button" class="btn btn-secondary custom-db-toggle-btn" data-index="${index}" aria-expanded="${isExpanded}" title="${isExpanded ? 'Hide connection details' : 'Show connection details'}">${isExpanded ? '▾' : '▸'}</button>
            <button type="button" class="btn btn-secondary custom-db-remove-btn" data-index="${index}" title="Remove this connection">&times;</button>
          </div>
          ${isExpanded ? (isBigQuery ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-bq-project-${index}">Project ID:</label>
              <input type="text" id="custom-db-bq-project-${index}" class="config-input custom-db-bq-project" data-index="${index}" placeholder="Project ID" value="${cfg.project_id || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-bq-dataset-${index}">Dataset:</label>
              <input type="text" id="custom-db-bq-dataset-${index}" class="config-input custom-db-bq-dataset" data-index="${index}" placeholder="Dataset" value="${cfg.dataset || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-bq-billing-${index}"><a href="https://cloud.google.com/bigquery/docs/managing-jobs" target="_blank" rel="noopener noreferrer" title="What a billing project is in BigQuery (Google Cloud docs)">Billing Project ID:</a></label>
              <input type="text" id="custom-db-bq-billing-${index}" class="config-input custom-db-bq-billing" data-index="${index}" placeholder="Billing project ID" value="${cfg.billing_project_id || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row align-start">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-bq-creds-${index}"><a href="https://cloud.google.com/iam/docs/keys-create-delete" target="_blank" rel="noopener noreferrer" title="How to create a service account key (Google Cloud docs)">Service Account Key:</a></label>
              <textarea id="custom-db-bq-creds-${index}" class="config-input custom-db-bq-creds" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Key saved - leave blank to keep it, or paste a new one to replace it' : 'Service-account key (JSON)'}" rows="3" autocomplete="off"></textarea>
            </div>
          </div>
          ` : isSnowflake ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-warehouse-${index}">Warehouse:</label>
              <input type="text" id="custom-db-sf-warehouse-${index}" class="config-input custom-db-sf-warehouse" data-index="${index}" placeholder="Warehouse" value="${cfg.warehouse || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-database-${index}">Database:</label>
              <input type="text" id="custom-db-sf-database-${index}" class="config-input custom-db-sf-database" data-index="${index}" placeholder="Database" value="${cfg.database || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-sf-schema-${index}" class="config-input custom-db-sf-schema" data-index="${index}" placeholder="Schema" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-account-${index}">Account:</label>
              <input type="text" id="custom-db-sf-account-${index}" class="config-input custom-db-sf-account" data-index="${index}" placeholder="e.g. xy12345.us-east-1" value="${cfg.account || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-user-${index}">User:</label>
              <input type="text" id="custom-db-sf-user-${index}" class="config-input custom-db-sf-user" data-index="${index}" placeholder="Username" value="${cfg.user || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-role-${index}">Role: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-sf-role-${index}" class="config-input custom-db-sf-role" data-index="${index}" placeholder="Role" value="${cfg.role || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-sf-auth-${index}">Authentication Method:</label>
              <select id="custom-db-sf-auth-${index}" class="config-input custom-db-sf-auth-method" data-index="${index}">
                <option value="password" ${sfAuthMethod === 'password' ? 'selected' : ''}>Password</option>
                <option value="private_key" ${sfAuthMethod === 'private_key' ? 'selected' : ''}>Key pair (private key)</option>
              </select>
            </div>
          </div>
          ${sfAuthMethod === 'private_key' ? `
          <div class="custom-db-field-row align-start">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-private-key-${index}"><a href="https://docs.snowflake.com/en/user-guide/key-pair-auth" target="_blank" rel="noopener noreferrer" title="Key-pair authentication (Snowflake docs)">Private Key:</a></label>
              <textarea id="custom-db-sf-private-key-${index}" class="config-input custom-db-sf-private-key" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Key saved - leave blank to keep it, or paste a new one to replace it' : 'Private key (PEM)'}" rows="2" autocomplete="off"></textarea>
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sf-passphrase-${index}">Passphrase: <span class="optional-hint">(if key is encrypted)</span></label>
              <input type="password" id="custom-db-sf-passphrase-${index}" class="config-input custom-db-sf-passphrase" data-index="${index}" placeholder="Private key passphrase" value="${cfg.private_key_passphrase || ''}" autocomplete="off">
            </div>
          </div>
          ` : `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-sf-password-${index}">Password:</label>
              <input type="password" id="custom-db-sf-password-${index}" class="config-input custom-db-sf-password" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Password saved - leave blank to keep it, or type a new one to replace it' : 'Password'}" autocomplete="off">
            </div>
          </div>
          `}
          ` : isDatabricks ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-dbx-hostname-${index}">Server Hostname:</label>
              <input type="text" id="custom-db-dbx-hostname-${index}" class="config-input custom-db-dbx-hostname" data-index="${index}" placeholder="e.g. dbc-a1b2c3d4-e5f6.cloud.databricks.com" value="${cfg.server_hostname || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-dbx-path-${index}">HTTP Path:</label>
              <input type="text" id="custom-db-dbx-path-${index}" class="config-input custom-db-dbx-path" data-index="${index}" placeholder="e.g. /sql/1.0/warehouses/0123456789abcdef" value="${cfg.http_path || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-dbx-catalog-${index}">Catalog: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-dbx-catalog-${index}" class="config-input custom-db-dbx-catalog" data-index="${index}" placeholder="Catalog" value="${cfg.catalog || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-dbx-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-dbx-schema-${index}" class="config-input custom-db-dbx-schema" data-index="${index}" placeholder="Schema" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-dbx-token-${index}"><a href="https://docs.databricks.com/en/dev-tools/auth/pat.html" target="_blank" rel="noopener noreferrer" title="Personal access tokens (Databricks docs)">Access Token:</a></label>
              <input type="password" id="custom-db-dbx-token-${index}" class="config-input custom-db-dbx-token" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Token saved - leave blank to keep it, or paste a new one to replace it' : 'Personal access token'}" autocomplete="off">
            </div>
          </div>
          ` : isOracle ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-host-${index}">Host:</label>
              <input type="text" id="custom-db-ora-host-${index}" class="config-input custom-db-ora-host" data-index="${index}" placeholder="e.g. db.example.com" value="${cfg.host || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-port-${index}">Port:</label>
              <input type="text" id="custom-db-ora-port-${index}" class="config-input custom-db-ora-port" data-index="${index}" placeholder="1521" value="${cfg.port || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-service-${index}">Service Name:</label>
              <input type="text" id="custom-db-ora-service-${index}" class="config-input custom-db-ora-service" data-index="${index}" placeholder="e.g. ORCLPDB1" value="${cfg.service_name || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-sid-${index}">SID: <span class="optional-hint">(legacy)</span></label>
              <input type="text" id="custom-db-ora-sid-${index}" class="config-input custom-db-ora-sid" data-index="${index}" placeholder="SID" value="${cfg.sid || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-ora-schema-${index}" class="config-input custom-db-ora-schema" data-index="${index}" placeholder="Defaults to the connecting user" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-user-${index}">User:</label>
              <input type="text" id="custom-db-ora-user-${index}" class="config-input custom-db-ora-user" data-index="${index}" placeholder="Username" value="${cfg.user || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ora-password-${index}">Password:</label>
              <input type="password" id="custom-db-ora-password-${index}" class="config-input custom-db-ora-password" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Password saved - leave blank to keep it, or type a new one to replace it' : 'Password'}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="checkbox-option" for="custom-db-ora-ssl-${index}">
                <input type="checkbox" id="custom-db-ora-ssl-${index}" class="config-input custom-db-ora-ssl" data-index="${index}" ${cfg.ssl ? 'checked' : ''}>
                <span class="checkbox-label">Use TLS (required for Oracle Cloud)</span>
              </label>
            </div>
          </div>
          ` : isRedshift ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-host-${index}">Host:</label>
              <input type="text" id="custom-db-rs-host-${index}" class="config-input custom-db-rs-host" data-index="${index}" placeholder="e.g. my-cluster.abc123.us-east-1.redshift.amazonaws.com" value="${cfg.host || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-port-${index}">Port:</label>
              <input type="text" id="custom-db-rs-port-${index}" class="config-input custom-db-rs-port" data-index="${index}" placeholder="5439" value="${cfg.port || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-database-${index}">Database:</label>
              <input type="text" id="custom-db-rs-database-${index}" class="config-input custom-db-rs-database" data-index="${index}" placeholder="Database" value="${cfg.database || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-rs-schema-${index}" class="config-input custom-db-rs-schema" data-index="${index}" placeholder="Defaults to the connecting user's search_path" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-user-${index}">User:</label>
              <input type="text" id="custom-db-rs-user-${index}" class="config-input custom-db-rs-user" data-index="${index}" placeholder="Username" value="${cfg.user || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-rs-password-${index}">Password:</label>
              <input type="password" id="custom-db-rs-password-${index}" class="config-input custom-db-rs-password" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Password saved - leave blank to keep it, or type a new one to replace it' : 'Password'}" autocomplete="off">
            </div>
          </div>
          ` : isSqlServer ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-host-${index}">Host:</label>
              <input type="text" id="custom-db-ms-host-${index}" class="config-input custom-db-ms-host" data-index="${index}" placeholder="e.g. my-server.database.windows.net" value="${cfg.host || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-port-${index}">Port:</label>
              <input type="text" id="custom-db-ms-port-${index}" class="config-input custom-db-ms-port" data-index="${index}" placeholder="1433" value="${cfg.port || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-database-${index}">Database:</label>
              <input type="text" id="custom-db-ms-database-${index}" class="config-input custom-db-ms-database" data-index="${index}" placeholder="Database" value="${cfg.database || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-ms-schema-${index}" class="config-input custom-db-ms-schema" data-index="${index}" placeholder="Defaults to dbo" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-user-${index}">User:</label>
              <input type="text" id="custom-db-ms-user-${index}" class="config-input custom-db-ms-user" data-index="${index}" placeholder="Username" value="${cfg.user || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-ms-password-${index}">Password:</label>
              <input type="password" id="custom-db-ms-password-${index}" class="config-input custom-db-ms-password" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Password saved - leave blank to keep it, or type a new one to replace it' : 'Password'}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="checkbox-option" for="custom-db-ms-encrypt-${index}">
                <input type="checkbox" id="custom-db-ms-encrypt-${index}" class="config-input custom-db-ms-encrypt" data-index="${index}" ${cfg.encrypt !== false ? 'checked' : ''}>
                <span class="checkbox-label">Encrypt Connection (required for Azure SQL Database)</span>
              </label>
            </div>
          </div>
          ` : isSheets ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-sh-url-${index}">Spreadsheet URL:</label>
              <input type="text" id="custom-db-sh-url-${index}" class="config-input custom-db-sh-url" data-index="${index}" placeholder="https://docs.google.com/spreadsheets/d/.../edit" value="${cfg.spreadsheet_url || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-sh-tab-${index}">Tab Name:</label>
              <input type="text" id="custom-db-sh-tab-${index}" class="config-input custom-db-sh-tab" data-index="${index}" placeholder="e.g. Sheet1" value="${cfg.tab_name || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row align-start">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-sh-creds-${index}"><a href="https://cloud.google.com/iam/docs/keys-create-delete" target="_blank" rel="noopener noreferrer" title="How to create a service account key (Google Cloud docs)">Service Account Key (optional):</a></label>
              <textarea id="custom-db-sh-creds-${index}" class="config-input custom-db-sh-creds" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Key saved - leave blank to keep it, or paste a new one to replace it' : 'Only needed for a private sheet (JSON)'}" rows="3" autocomplete="off"></textarea>
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <span class="optional-hint">Leave the key blank for a public sheet ("Anyone with the link can view"). For a private sheet, share it with a service account's email and paste that account's JSON key above.</span>
            </div>
          </div>
          ` : isMongoSql ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-mongo-uri-${index}">URI:</label>
              <input type="text" id="custom-db-mongo-uri-${index}" class="config-input custom-db-mongo-uri" data-index="${index}" placeholder="mongodb://atlas-sql-xxxxx.a.query.mongodb.net/?ssl=true&authSource=admin" value="${db.url || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-mongo-database-${index}">Database:</label>
              <input type="text" id="custom-db-mongo-database-${index}" class="config-input custom-db-mongo-database" data-index="${index}" placeholder="Database" value="${cfg.database || ''}" autocomplete="off">
            </div>
            <div class="custom-db-field">
              <label class="custom-db-field-label" for="custom-db-mongo-user-${index}">User:</label>
              <input type="text" id="custom-db-mongo-user-${index}" class="config-input custom-db-mongo-user" data-index="${index}" placeholder="Username" value="${cfg.user || ''}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-mongo-password-${index}">Password:</label>
              <input type="password" id="custom-db-mongo-password-${index}" class="config-input custom-db-mongo-password" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Password saved - leave blank to keep it, or type a new one to replace it' : 'Password'}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <span class="optional-hint custom-db-mongo-hint">Get these values from the ODBC connection string Atlas gave you when enabling the SQL Interface on your cluster. Note: the interface supports one read operations.</span>
            </div>
          </div>
          ` : `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-url-${index}">URL:</label>
              <input type="text" id="custom-db-url-${index}" class="config-input custom-db-url-input" data-index="${index}" placeholder="${isMySQL ? 'mysql://user:password@host:3306/dbname' : 'postgresql://user:password@host:5432/dbname'}" value="${maskConnectionUrl(db.url)}" autocomplete="off">
            </div>
          </div>
          <div class="custom-db-field-row align-start">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-cacert-${index}">CA Certificate: <span class="optional-hint">(optional - only needed if your URL sets sslmode=verify-ca or verify-full; ignored for a unix_socket connection)</span></label>
              <textarea id="custom-db-cacert-${index}" class="config-input custom-db-cacert" data-index="${index}" placeholder="Paste a PEM-encoded CA certificate here to verify the server (not needed for sslmode=require)" rows="3" autocomplete="off">${cfg.ca_cert_pem || ''}</textarea>
            </div>
          </div>
          `) : ''}
        </div>
      `;
    });

    html += `<button type="button" id="addCustomDbBtn" class="btn btn-secondary custom-db-add-btn">+ Add custom connection</button>`;

    container.innerHTML = html;

    container.querySelectorAll('.custom-db-type-select').forEach(select => {
      select.addEventListener('change', () => {
        const index = parseInt(select.dataset.index);
        const existingName = (customDatabases[index] && customDatabases[index].name) || '';
        customDatabases[index] = makeEmptyCustomDb(select.value);
        customDatabases[index].name = existingName;
        renderCustomDbRows(activeUrl);
      });
    });

    container.querySelectorAll('.custom-db-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const index = parseInt(btn.dataset.index);
        const db = customDatabases[index];
        const currentlyExpanded = db._expanded !== undefined ? db._expanded : !db.connection_key;
        db._expanded = !currentlyExpanded;
        renderCustomDbRows(activeUrl);
      });
    });

    container.querySelectorAll('.custom-db-remove-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const index = parseInt(btn.dataset.index);
        customDatabases.splice(index, 1);
        renderCustomDbRows(activeUrl);
      });
    });

    container.querySelectorAll('.custom-db-name-input').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        customDatabases[index].name = input.value.trim();
        if (radio) radio.dataset.dbname = customDatabases[index].name;
      });
    });

    container.querySelectorAll('.custom-db-url-input').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const unmaskedUrl = unmaskConnectionUrl(input.value.trim(), customDatabases[index].url);
        customDatabases[index].url = unmaskedUrl;
        // Only auto-fill the name from the URL while the user hasn't typed
        // one of their own in the name field above - an explicit name must
        // never be silently overwritten by editing the URL afterwards.
        if (!customDatabases[index].name) {
          customDatabases[index].name = getDatabaseNameFromUrl(unmaskedUrl);
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = customDatabases[index].name;
        }
        if (radio) radio.dataset.dbname = customDatabases[index].name;
      });
    });

    container.querySelectorAll('.custom-db-mongo-uri').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        // Unlike Postgres/MySQL's url, Mongo's uri never carries a
        // credential any more (see backends/mongodb_sql.py's module
        // docstring) - no masking/unmasking needed, this is just an
        // ordinary text field.
        customDatabases[index].url = input.value.trim();
      });
    });

    container.querySelectorAll(
      '.custom-db-mongo-database, .custom-db-mongo-user, .custom-db-mongo-password'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-mongo-database')) db.config.database = input.value.trim();
        if (input.classList.contains('custom-db-mongo-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-mongo-password')) db.config.password = input.value;
        // Same rule as every other structured dialect below: don't
        // clobber a name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.database || 'Custom MongoDB';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll('.custom-db-cacert').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        if (!customDatabases[index].config) customDatabases[index].config = {};
        // Not a secret (see backends/postgres.py's/backends/mysql.py's
        // module docstrings), so unlike every credential textarea below
        // there's no masking/"leave blank to keep it" convention - a
        // blank value here really does mean "no CA cert", clearing
        // whatever was saved before.
        customDatabases[index].config.ca_cert_pem = input.value.trim();
      });
    });

    container.querySelectorAll('.custom-db-bq-project, .custom-db-bq-dataset, .custom-db-bq-billing, .custom-db-bq-creds').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-bq-project')) db.config.project_id = input.value.trim();
        if (input.classList.contains('custom-db-bq-dataset')) db.config.dataset = input.value.trim();
        if (input.classList.contains('custom-db-bq-billing')) db.config.billing_project_id = input.value.trim();
        if (input.classList.contains('custom-db-bq-creds')) db.config.credentials_json = input.value.trim();
        // No db.url here any more - BigQuery has no real url of its own
        // (see config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the Postgres URL input above: don't clobber a name
        // the user already typed themselves.
        if (!db.name) {
          db.name = db.config.dataset || 'Custom BigQuery';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll('.custom-db-sf-auth-method').forEach(select => {
      select.addEventListener('change', () => {
        const index = parseInt(select.dataset.index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        db.config.auth_method = select.value;
        // Switching auth methods shows a different credential field below
        // (password vs. private key/passphrase) - needs a re-render, same
        // as the dialect <select> above.
        renderCustomDbRows(activeUrl);
      });
    });

    container.querySelectorAll(
      '.custom-db-sf-account, .custom-db-sf-database, .custom-db-sf-user, .custom-db-sf-warehouse, '
      + '.custom-db-sf-schema, .custom-db-sf-role, .custom-db-sf-password, .custom-db-sf-private-key, '
      + '.custom-db-sf-passphrase'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-sf-account')) db.config.account = input.value.trim();
        if (input.classList.contains('custom-db-sf-database')) db.config.database = input.value.trim();
        if (input.classList.contains('custom-db-sf-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-sf-warehouse')) db.config.warehouse = input.value.trim();
        if (input.classList.contains('custom-db-sf-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-sf-role')) db.config.role = input.value.trim();
        if (input.classList.contains('custom-db-sf-password')) db.config.password = input.value;
        if (input.classList.contains('custom-db-sf-private-key')) db.config.private_key = input.value.trim();
        if (input.classList.contains('custom-db-sf-passphrase')) db.config.private_key_passphrase = input.value;
        // No db.url here any more - Snowflake has no real url of its own
        // (see config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the Postgres/BigQuery inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.database || 'Custom Snowflake';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll(
      '.custom-db-dbx-hostname, .custom-db-dbx-path, .custom-db-dbx-catalog, '
      + '.custom-db-dbx-schema, .custom-db-dbx-token'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-dbx-hostname')) db.config.server_hostname = input.value.trim();
        if (input.classList.contains('custom-db-dbx-path')) db.config.http_path = input.value.trim();
        if (input.classList.contains('custom-db-dbx-catalog')) db.config.catalog = input.value.trim();
        if (input.classList.contains('custom-db-dbx-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-dbx-token')) db.config.access_token = input.value;
        // No db.url here any more - Databricks has no real url of its own
        // (see config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the other dialect inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.http_path || 'Custom Databricks';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll(
      '.custom-db-ora-host, .custom-db-ora-port, .custom-db-ora-service, .custom-db-ora-sid, '
      + '.custom-db-ora-user, .custom-db-ora-schema, .custom-db-ora-password, .custom-db-ora-ssl'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      const isCheckbox = input.type === 'checkbox';
      // A checkbox has no meaningful "focus to select this row" moment the
      // way a text field does (it toggles on click, not on typing after
      // tabbing in) - only wired to 'change', not 'focus', unlike every
      // other Oracle field below.
      if (!isCheckbox) {
        input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      }
      input.addEventListener(isCheckbox ? 'change' : 'input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-ora-host')) db.config.host = input.value.trim();
        if (input.classList.contains('custom-db-ora-port')) db.config.port = input.value.trim();
        if (input.classList.contains('custom-db-ora-service')) db.config.service_name = input.value.trim();
        if (input.classList.contains('custom-db-ora-sid')) db.config.sid = input.value.trim();
        if (input.classList.contains('custom-db-ora-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-ora-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-ora-password')) db.config.password = input.value;
        if (input.classList.contains('custom-db-ora-ssl')) db.config.ssl = input.checked;
        // No db.url here any more - Oracle has no real url of its own (see
        // config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // service_name takes precedence over sid when both are somehow
        // filled in, same as config_routes.py's _oracle_identity.
        const serviceOrSid = db.config.service_name || db.config.sid;
        // Same rule as the other dialect inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = serviceOrSid || 'Custom Oracle';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll(
      '.custom-db-rs-host, .custom-db-rs-port, .custom-db-rs-database, '
      + '.custom-db-rs-schema, .custom-db-rs-user, .custom-db-rs-password'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-rs-host')) db.config.host = input.value.trim();
        if (input.classList.contains('custom-db-rs-port')) db.config.port = input.value.trim();
        if (input.classList.contains('custom-db-rs-database')) db.config.database = input.value.trim();
        if (input.classList.contains('custom-db-rs-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-rs-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-rs-password')) db.config.password = input.value;
        // No db.url here any more - Redshift has no real url of its own
        // (see config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the other dialect inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.database || 'Custom Redshift';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll(
      '.custom-db-ms-host, .custom-db-ms-port, .custom-db-ms-database, '
      + '.custom-db-ms-schema, .custom-db-ms-user, .custom-db-ms-password, .custom-db-ms-encrypt'
    ).forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      const isCheckbox = input.type === 'checkbox';
      // Same "no focus-to-select moment" reasoning as Oracle's ssl checkbox
      // above - only wired to 'change', not 'focus'.
      if (!isCheckbox) {
        input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      }
      input.addEventListener(isCheckbox ? 'change' : 'input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-ms-host')) db.config.host = input.value.trim();
        if (input.classList.contains('custom-db-ms-port')) db.config.port = input.value.trim();
        if (input.classList.contains('custom-db-ms-database')) db.config.database = input.value.trim();
        if (input.classList.contains('custom-db-ms-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-ms-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-ms-password')) db.config.password = input.value;
        if (input.classList.contains('custom-db-ms-encrypt')) db.config.encrypt = input.checked;
        // No db.url here any more - SQL Server has no real url of its own
        // (see config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the other dialect inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.database || 'Custom SQL Server';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    container.querySelectorAll('.custom-db-sh-url, .custom-db-sh-tab, .custom-db-sh-creds').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-sh-url')) db.config.spreadsheet_url = input.value.trim();
        if (input.classList.contains('custom-db-sh-tab')) db.config.tab_name = input.value.trim();
        // Optional - see makeEmptyCustomDb's sheets branch. A blank value
        // here is never sent to the server at all (see the payload-
        // building spots below), so leaving this untouched never clobbers
        // an already-saved key the way an always-required field would.
        if (input.classList.contains('custom-db-sh-creds')) db.config.credentials_json = input.value.trim();
        // No db.url here any more - Sheets has no real url of its own (see
        // config_routes.py's module docstring); radio-selection now
        // matches by connection_key instead (see isSelected above).
        // Same rule as the other dialect inputs above: don't clobber a
        // name the user already typed themselves.
        if (!db.name) {
          db.name = db.config.tab_name || 'Custom Sheet';
          const nameInput = container.querySelector(`.custom-db-name-input[data-index="${index}"]`);
          if (nameInput) nameInput.value = db.name;
        }
        if (radio) radio.dataset.dbname = db.name;
      });
    });

    const addBtn = document.getElementById('addCustomDbBtn');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        customDatabases.push(makeEmptyCustomDb('postgres'));
        renderCustomDbRows(activeUrl);
        requestAnimationFrame(() => {
          const inputs = container.querySelectorAll('.custom-db-name-input');
          const last = inputs[inputs.length - 1];
          if (last) last.focus();
        });
      });
    }
  }

  // Multi-database question-answering (see server/translate_routes.py's
  // module docstring) is scoped to a binary choice, not an arbitrary
  // subset: either ONE specific connection is in scope (today's original,
  // unchanged behavior) or EVERY configured connection is ("All", see
  // renderDbRadioButtons()' new radio option below). Which one is true is
  // read straight from the server-persisted IN_SCOPE_MODE (see its
  // declaration above for why this is more reliable than inferring "all"
  // from the in-scope arrays' combined length) - so this stays correct
  // even for a session with only one connection actually configured but
  // in_scope_mode "all", or a legacy session with 2+ specific connections
  // saved under the old checkbox picker's arbitrary-subset UI but
  // in_scope_mode still "single" (or never explicitly saved at all).
  function isAllConnectionsSelected() {
    return IN_SCOPE_MODE === 'all';
  }

  function renderDbRadioButtons(currentDbUrl) {
    const radioGroup = document.getElementById('modalDbRadioGroup');
    if (!radioGroup) return;

    const activeUrl = currentDbUrl || ACTIVE_DB_URL || DEFAULT_DB_URL;
    const allSelected = isAllConnectionsSelected();

    let html = `
      <label class="radio-option all-databases-option">
        <input type="radio" name="db_connection_option" value="all" ${allSelected ? 'checked' : ''}>
        <span class="radio-label">All configured databases</span>
      </label>
      <p class="all-databases-hint">
        Ask a question without picking a database first - the app figures out which connection(s) it applies to,
        and can query more than one at once when a question genuinely needs it.
      </p>
    `;

    html += `<div class="radio-group-heading">Pre-configured Database Playgrounds</div>`;

    // Two visual columns, purely a layout grouping (no change to what's
    // selectable or how - db_connection_option/preset:<id> works exactly
    // the same either way): the 4 "simple credential" dialects that speak
    // a single connection-string/user+password (Postgres, MySQL, Oracle,
    // SQL Server) on the left, the other 5 structured/cloud dialects
    // (BigQuery, Snowflake, Databricks, Redshift, Google Sheets) on the
    // right. LEFT_COLUMN_TYPES is exhaustive over every dialect this app
    // supports today (see this file's isComplete*/config_routes.py's
    // module docstring for the full list) - a future new dialect type not
    // in either set falls into the right column by default, below.
    // MongoDB Atlas SQL added to the left ("simple credential") column
    // too - like Postgres/MySQL/Oracle/SQL Server, it's a single-server
    // connection a user types in directly, not a structured/cloud
    // dialect - see backends/mongodb_sql.py. (It does have its own
    // database/user/password fields like Oracle/SQL Server do, just no
    // separate identity/display-url concept.)
    const LEFT_COLUMN_TYPES = new Set(['postgres', 'mysql', 'oracle', 'mssql', 'MongoDB']);
    const leftPresets = [];
    const rightPresets = [];
    CONFIGURED_DBS.forEach((db) => {
      (LEFT_COLUMN_TYPES.has(db.type) ? leftPresets : rightPresets).push(db);
    });

    const renderPresetOption = (db) => {
      // Encodes the preset's stable id (never a secret, unlike the real
      // URL) rather than the URL itself or its array position - the id
      // survives the admin reordering/adding/removing presets between
      // deployments and works identically whether or not this visitor's
      // CONFIGURED_DBS entries are redacted (see fetchBackendConfig()).
      // Resolved server-side via payload.preset_id (see triggerConfigSave()).
      const value = `preset:${db.id}`;
      // Checked state comes from the in-scope set (see IN_SCOPE_PRESET_IDS'
      // docstring), not ACTIVE_PRESET_ID directly, but is only ever true
      // for this SPECIFIC preset when "All" isn't the current selection
      // (see isAllConnectionsSelected()) - the radio group is single-select
      // again, so exactly one of "All" or one specific connection is
      // checked at a time. A session that's never explicitly saved an
      // in-scope set has this array lazily derived server-side from the
      // single active connection (state_store.py's get_session), so a
      // never-touched session's one radio shows checked exactly as before
      // this feature existed.
      const isSelected = !allSelected && IN_SCOPE_PRESET_IDS.includes(db.id);
      return `
        <label class="radio-option">
          <input type="radio" name="db_connection_option" value="${value}" data-dbname="${db.name}" ${isSelected ? 'checked' : ''}>
          <span class="radio-label">${db.name}</span>
        </label>
      `;
    };

    html += `
      <div class="preset-columns">
        <div class="preset-column">${leftPresets.map(renderPresetOption).join('')}</div>
        <div class="preset-column">${rightPresets.map(renderPresetOption).join('')}</div>
      </div>
    `;

    html += `<div class="radio-group-heading radio-group-heading-custom">Custom Database Connections</div>`;
    // Short reassurance, not a full explanation - see help.html's "Database
    // Connections"/"User Authentication" sections (opened via the link
    // below) for the actual detail: encryption at rest, and exactly what
    // "your own session" vs. "your account" scoping means.
    html += `
      <p class="custom-db-security-note">
        Your custom connections are private and secure (<a href="#" id="customDbSecurityNoteHelpLink">see Documentation</a>).
      </p>
    `;
    html += `<div id="customDbsContainer" class="custom-dbs-list"></div>`;

    radioGroup.innerHTML = html;

    renderCustomDbRows(activeUrl);

    // Re-wired on every render, not just once at startup - the link above
    // is recreated from scratch each time renderDbRadioButtons() rebuilds
    // radioGroup.innerHTML, same as customDbsContainer's own inputs below.
    const securityNoteHelpLink = document.getElementById('customDbSecurityNoteHelpLink');
    if (securityNoteHelpLink) {
      securityNoteHelpLink.addEventListener('click', (e) => {
        e.preventDefault();
        openHelpModal();
      });
    }
  }

  // ===========================================================================
  // MODEL SELECTION MODAL (fetch already covered by fetchBackendConfig() -
  // see LLM_PROVIDERS/ACTIVE_LLM_PROVIDER/ACTIVE_LLM_MODEL - this section
  // just renders/saves the radio list, mirroring renderDbRadioButtons()/
  // triggerConfigSave() above but scoped to model selection only, since a
  // model choice is otherwise fully independent of the DB connection form.)
  // ===========================================================================

  // Display-only company names for the modal's radio-group headings.
  // provider.name IS "google"/"anthropic"/"openai" server-side now (see
  // translate_routes.py's _LLM_PROVIDERS) - this map exists only because
  // naively title-casing that string would render OpenAI's heading as
  // "Openai" instead of "OpenAI"; Google/Anthropic would already come out
  // right without it, but spelling all three out here is clearer than a
  // one-off special case for just the exception.
  const LLM_PROVIDER_DISPLAY_NAMES = {
    google: "Google",
    anthropic: "Anthropic",
    openai: "OpenAI",
  };

  function renderModelRadioButtons() {
    const radioGroup = document.getElementById('modalModelRadioGroup');
    if (!radioGroup) return;

    // One radio-group-heading + column of radio-options per provider (see
    // renderDbRadioButtons() for the same heading/radio-option markup this
    // reuses verbatim via the shared .radio-group/.radio-option/
    // .radio-group-heading CSS classes) - "organized by llm_provider", as
    // requested, without needing any new CSS.
    // Provider/model names are server-configured (env vars an admin sets),
    // never raw end-user input - same trust level renderDbRadioButtons()
    // already extends to db.name above, so this interpolates them
    // unescaped too, consistent with that existing convention.
    let html = '';
    LLM_PROVIDERS.forEach((provider) => {
      const providerLabel = LLM_PROVIDER_DISPLAY_NAMES[provider.name] ||
        (provider.name.charAt(0).toUpperCase() + provider.name.slice(1));
      html += `<div class="radio-group-heading">${providerLabel}</div>`;
      html += (provider.preset_models || []).map((model) => {
        const value = `${provider.name}::${model}`;
        const isSelected = provider.name === ACTIVE_LLM_PROVIDER && model === ACTIVE_LLM_MODEL;
        return `
          <label class="radio-option">
            <input type="radio" name="llm_model_option" value="${value}" ${isSelected ? 'checked' : ''}>
            <span class="radio-label">${model}</span>
          </label>
        `;
      }).join('');
    });

    radioGroup.innerHTML = html;
  }

  function closeModelModal() {
    if (modelModal) modelModal.classList.add('hidden');
  }

  async function saveModelSelection() {
    const modelSaveErrorEl = document.getElementById('modelSaveError');
    if (modelSaveErrorEl) {
      modelSaveErrorEl.style.display = 'none';
      modelSaveErrorEl.textContent = '';
    }

    const checked = document.querySelector('input[name="llm_model_option"]:checked');
    if (!checked) {
      closeModelModal();
      return;
    }
    const separatorIndex = checked.value.indexOf('::');
    const llmProvider = checked.value.slice(0, separatorIndex);
    const llmModel = checked.value.slice(separatorIndex + 2);

    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({ llm_provider: llmProvider, llm_model: llmModel }),
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.error || 'Failed to save model selection.');
      }
      await fetchBackendConfig();
      trackEvent('model_selected', { provider: llmProvider, model: llmModel });
      closeModelModal();
    } catch (err) {
      if (modelSaveErrorEl) {
        modelSaveErrorEl.textContent = err.message || 'Failed to save model selection.';
        modelSaveErrorEl.style.display = 'block';
      }
    }
  }

  if (modelTriggerBadge && modelModal) {
    modelTriggerBadge.addEventListener('click', async () => {
      // See setButtonsDisabled()'s own comment on badge-disabled - a query
      // is in flight, so opening this modal is blocked entirely rather
      // than just visually grayed out (the div has no native `disabled`
      // to rely on for that).
      if (modelTriggerBadge.classList.contains('badge-disabled')) return;
      await fetchBackendConfig();
      renderModelRadioButtons();
      const modelSaveErrorEl = document.getElementById('modelSaveError');
      if (modelSaveErrorEl) {
        modelSaveErrorEl.style.display = 'none';
        modelSaveErrorEl.textContent = '';
      }
      modelModal.classList.remove('hidden');
      bringModalToFront(modelModal);
    });
  }

  if (modelModalCloseBtn) {
    modelModalCloseBtn.addEventListener('click', closeModelModal);
  }

  if (modelSaveBtn) {
    modelSaveBtn.addEventListener('click', saveModelSelection);
  }

  // ===========================================================================
  // PREFERENCES MODAL (theme + auto-execute-SQL). Mirrors the Model Selection
  // Modal above: a small, independent settings surface with its own minimal
  // POST to /api/config, distinct from the DB connection form's
  // triggerConfigSave(). Theme itself never goes to the server (see the
  // THEME SWITCHING section) - only auto_sql_execute is persisted there.
  // ===========================================================================

  function closePreferencesModal() {
    if (preferencesModal) preferencesModal.classList.add('hidden');
  }

  function loadPreferencesIntoUI() {
    const currentTheme = getCurrentTheme();
    if (themeOptionDark) themeOptionDark.checked = currentTheme === 'dark';
    if (themeOptionLight) themeOptionLight.checked = currentTheme === 'light';
    if (autoSqlExecuteCheckbox) {
      autoSqlExecuteCheckbox.checked = autoSqlExecuteEnabled;
    }

    // Bring Your Own Key - every box always starts blank (the saved key,
    // if any, is never sent to the browser - see LLM_BYOK_KEY_SET's own
    // comment); the placeholder is what actually shows whether a key is
    // currently saved for that provider, same wording/pattern the custom
    // connection form already uses for has_custom_credentials fields.
    byokProvidersMarkedForClear.clear();
    Object.keys(BYOK_PROVIDER_FIELDS).forEach((providerName) => {
      const field = BYOK_PROVIDER_FIELDS[providerName];
      if (!field.input) return;
      field.input.value = '';
      field.input.placeholder = LLM_BYOK_KEY_SET[providerName]
        ? 'Key saved - leave blank to keep it, or paste a new one to replace it'
        : 'Paste your API key';
    });
  }

  async function savePreferences() {
    const preferencesSaveErrorEl = document.getElementById('preferencesSaveError');
    if (preferencesSaveErrorEl) {
      preferencesSaveErrorEl.style.display = 'none';
      preferencesSaveErrorEl.textContent = '';
    }

    const selectedTheme = themeOptionLight && themeOptionLight.checked ? 'light' : 'dark';
    setTheme(selectedTheme);

    const autoSqlExecuteValue = autoSqlExecuteCheckbox
      ? autoSqlExecuteCheckbox.checked
      : autoSqlExecuteEnabled;

    // Bring Your Own Key - only the provider(s) actually touched this save
    // are included at all (see StateStore.set_session's llm_byok_keys
    // contract): a freshly typed value replaces the saved key, an "x"-
    // cleared-and-left-blank box sends "" to explicitly remove it, and a
    // box that's just sitting blank because nothing was ever saved (or
    // because a saved key simply isn't being changed right now) is
    // omitted entirely rather than sent as "" - that would wrongly clear
    // an already-saved key the user never asked to touch.
    const llmByokKeys = {};
    Object.keys(BYOK_PROVIDER_FIELDS).forEach((providerName) => {
      const field = BYOK_PROVIDER_FIELDS[providerName];
      if (!field.input) return;
      const typedValue = field.input.value.trim();
      if (typedValue) {
        llmByokKeys[providerName] = typedValue;
      } else if (byokProvidersMarkedForClear.has(providerName)) {
        llmByokKeys[providerName] = '';
      }
    });

    const preferencesPayload = { auto_sql_execute: autoSqlExecuteValue, theme: selectedTheme };
    if (Object.keys(llmByokKeys).length > 0) {
      preferencesPayload.llm_byok_keys = llmByokKeys;
    }

    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify(preferencesPayload),
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.error || 'Failed to save preferences.');
      }
      await fetchBackendConfig();
      closePreferencesModal();
    } catch (err) {
      if (preferencesSaveErrorEl) {
        preferencesSaveErrorEl.textContent = err.message || 'Failed to save preferences.';
        preferencesSaveErrorEl.style.display = 'block';
      }
    }
  }

  if (prefsBtn && preferencesModal) {
    prefsBtn.addEventListener('click', async () => {
      trackEvent('preferences_viewed', {});
      await fetchBackendConfig();
      loadPreferencesIntoUI();
      const preferencesSaveErrorEl = document.getElementById('preferencesSaveError');
      if (preferencesSaveErrorEl) {
        preferencesSaveErrorEl.style.display = 'none';
        preferencesSaveErrorEl.textContent = '';
      }
      preferencesModal.classList.remove('hidden');
      bringModalToFront(preferencesModal);
    });
  }

  if (preferencesModalCloseBtn) {
    preferencesModalCloseBtn.addEventListener('click', closePreferencesModal);
  }

  if (preferencesSaveBtn) {
    preferencesSaveBtn.addEventListener('click', savePreferences);
  }

  // Bring Your Own Key - "x" click marks that provider for an explicit
  // clear on the next Save (see byokProvidersMarkedForClear's comment) and
  // gives immediate visual feedback (blanks the box, flips the placeholder
  // to the "not set" wording) without waiting for the round trip; typing
  // into the box again un-marks it, since that's a change of mind - the
  // freshly typed value should replace the key, not clear-then-replace.
  Object.keys(BYOK_PROVIDER_FIELDS).forEach((providerName) => {
    const field = BYOK_PROVIDER_FIELDS[providerName];
    if (field.clearBtn) {
      field.clearBtn.addEventListener('click', () => {
        byokProvidersMarkedForClear.add(providerName);
        if (field.input) {
          field.input.value = '';
          field.input.placeholder = 'Paste your API key';
        }
      });
    }
    if (field.input) {
      field.input.addEventListener('input', () => {
        byokProvidersMarkedForClear.delete(providerName);
      });
    }
  });

  async function triggerConfigSave({ closeModal = false } = {}) {
    let dbType = 'postgres';
    let dbUrlValue = null;
    let dbNameValue = null;
    let dbProjectId = null;
    let dbDataset = null;
    let dbBillingProjectId = null;
    let dbCredentialsJson = null;
    let dbAccount = null;
    let dbUser = null;
    let dbWarehouse = null;
    let dbDatabase = null;
    let dbSchema = null;
    let dbRole = null;
    let dbPassword = null;
    let dbPrivateKey = null;
    let dbPrivateKeyPassphrase = null;
    let dbServerHostname = null;
    let dbHttpPath = null;
    let dbCatalog = null;
    let dbAccessToken = null;
    let dbHost = null;
    let dbPort = null;
    let dbServiceName = null;
    let dbSid = null;
    let dbSsl = null;
    let dbSpreadsheetUrl = null;
    let dbTabName = null;
    // Postgres-only (see backends/postgres.py's module docstring) - not a
    // credential, so unlike dbPassword/dbCredentialsJson above this never
    // needs a "may be blank, server reuses the saved one" fallback; a
    // blank value really does mean "no CA cert supplied this time".
    let dbCaCertPem = null;
    // Named distinctly from BigQuery's own dbCredentialsJson above - these
    // are two different dialects' credentials, both optional/reuse-when-
    // blank, but never the same variable.
    let dbSheetsCredentialsJson = null;
    let dbEncrypt = null;
    let isCustomOption = false;
    // Set only for anonymous users picking a preset by its stable id (see
    // renderDbRadioButtons()) - the server resolves the real connection
    // from this id itself, since anonymous users never receive one. Signed-
    // in users' preset selections are still matched by their real field
    // values in _parse_incoming_connection (see its own comments), not by
    // id, so this stays null for them even though the radio value now
    // encodes an id for both user types.
    let presetId = null;

    // A custom BigQuery connection is only "complete"/selectable/saveable
    // once it has BOTH its own billing project ID and its own key - either
    // freshly entered, or (for the key only, since it's never redisplayed)
    // already saved server-side (has_custom_credentials). Billing project
    // isn't a secret and IS always redisplayed as-is, so it has no
    // equivalent "already saved" bypass - it must actually be filled in.
    // See config_routes.py's module docstring for why neither field ever
    // falls back to a preset's or this app's own billing project for a
    // custom connection.
    const isCompleteBigQuery = (db) => db && db.type === 'bigquery' && db.config
      && db.config.project_id && db.config.dataset
      && db.config.billing_project_id
      && (db.config.credentials_json || db.has_custom_credentials);
    // Same idea for Snowflake, minus the billing dimension (Snowflake has
    // no BigQuery-style separate billing project) but with a credential
    // that's one of two mutually-exclusive shapes - either counts as
    // "has a credential", freshly entered or (since neither is ever
    // redisplayed) already saved server-side.
    const isCompleteSnowflake = (db) => db && db.type === 'snowflake' && db.config
      && db.config.account && db.config.user && db.config.warehouse && db.config.database
      && (db.config.password || db.config.private_key || db.has_custom_credentials);
    // Same idea for Databricks - no billing dimension, and exactly one
    // credential shape (an access token) rather than Snowflake's two, but
    // otherwise the same "freshly entered, or already saved server-side"
    // rule (see backends/databricks.py's module docstring - PAT-only for
    // this first pass).
    const isCompleteDatabricks = (db) => db && db.type === 'databricks' && db.config
      && db.config.server_hostname && db.config.http_path
      && (db.config.access_token || db.has_custom_credentials);
    // Same idea for Oracle - core identifying fields (host, user, and one
    // of service_name/sid) plus a single credential shape (password),
    // same "freshly entered, or already saved server-side" rule as every
    // other structured dialect above (see backends/oracle.py's module
    // docstring - plain username/password only for this first pass).
    const isCompleteOracle = (db) => db && db.type === 'oracle' && db.config
      && db.config.host && db.config.user && (db.config.service_name || db.config.sid)
      && (db.config.password || db.has_custom_credentials);
    // Same idea for Redshift - core identifying fields (host, database,
    // user) plus a single credential shape (password), same "freshly
    // entered, or already saved server-side" rule as every other
    // structured dialect above (see backends/redshift.py's module
    // docstring - plain username/password only for this first pass).
    const isCompleteRedshift = (db) => db && db.type === 'redshift' && db.config
      && db.config.host && db.config.database && db.config.user
      && (db.config.password || db.has_custom_credentials);
    // Same idea for SQL Server - core identifying fields (host, database,
    // user) plus a single credential shape (password), same "freshly
    // entered, or already saved server-side" rule as every other
    // structured dialect above (see backends/mssql.py's module docstring -
    // plain SQL Login username/password only for this first pass).
    // "encrypt" isn't part of completeness - it has its own always-present
    // default (true) at the backend layer, so it's never a blocking field.
    const isCompleteMssql = (db) => db && db.type === 'mssql' && db.config
      && db.config.host && db.config.database && db.config.user
      && (db.config.password || db.has_custom_credentials);
    // Same idea for Google Sheets - but credentials_json is deliberately
    // NOT part of this completeness check, unlike every credentialed
    // dialect above: it's optional (see backends/sheets.py's module
    // docstring), so a row with just these two non-secret fields filled in
    // is already a fully valid (public-sheet) connection.
    const isCompleteSheets = (db) => db && db.type === 'sheets' && db.config
      && db.config.spreadsheet_url && db.config.tab_name;
    // Postgres and MySQL are both "simple URL" dialects (see
    // backends/mysql.py's module docstring) - a single non-blank url is
    // all either needs to be selectable/saveable. Named generically
    // (not isCompletePostgres) since it now covers both. MongoDB is
    // explicitly excluded here (unlike before this dialect had its own
    // database/user/password fields) and gets its own isCompleteMongo
    // check below instead, since a bare url alone is no longer enough
    // for it.
    const isCompleteSimpleUrlDb = (db) => db && db.type !== 'bigquery' && db.type !== 'snowflake' && db.type !== 'databricks' && db.type !== 'oracle' && db.type !== 'redshift' && db.type !== 'mssql' && db.type !== 'sheets' && db.type !== 'MongoDB'
      && db.url && db.url.trim() !== "";
    // MongoDB Atlas SQL is a hybrid: a real url (like Postgres/MySQL)
    // PLUS separate structured config fields (like every dialect above)
    // - see backends/mongodb_sql.py's and config_routes.py's module
    // docstrings. Same "freshly entered, or already saved server-side"
    // credential rule as the rest.
    const isCompleteMongo = (db) => db && db.type === 'MongoDB' && db.url && db.url.trim() !== ""
      && db.config && db.config.database && db.config.user
      && (db.config.password || db.has_custom_credentials);
    // Combined "is this custom row saveable/selectable at all" check,
    // hoisted out of the custom-connection branch below so the "All"
    // synthesis just above it can reuse the exact same definition of
    // "complete" when there are no presets configured at all.
    const isCompleteCustomDb = (d) => isCompleteBigQuery(d) || isCompleteSnowflake(d) || isCompleteDatabricks(d) || isCompleteOracle(d) || isCompleteRedshift(d) || isCompleteMssql(d) || isCompleteSheets(d) || isCompleteMongo(d) || isCompleteSimpleUrlDb(d);

    // The radio group is single-select again (see renderDbRadioButtons()),
    // so exactly one input is ever checked - no more "most recently
    // focused row" tiebreaking needed among several simultaneously-checked
    // boxes the way the old checkbox-based picker required.
    const selectedDbRadio = document.querySelector('input[name="db_connection_option"]:checked');

    // "All configured databases" (see renderDbRadioButtons()'s new radio
    // option) has no dedicated preset/custom fields of its own - the
    // single PRIMARY connection (today's pre-existing connection_id/
    // is_custom fields) is still just whichever connection would be
    // first in stable order (presets, then custom - see db.py's
    // resolve_in_scope_descriptors), same rule already used server-side
    // for resolving the primary out of an in-scope set. Synthesizing an
    // equivalent preset:<id>/custom-<index> value here lets the exact same
    // branch logic below (already handling every dialect) run unchanged
    // rather than duplicating it for this option.
    let effectiveSelectionValue = selectedDbRadio ? selectedDbRadio.value : null;
    if (effectiveSelectionValue === 'all') {
      if (CONFIGURED_DBS.length > 0) {
        effectiveSelectionValue = `preset:${CONFIGURED_DBS[0].id}`;
      } else {
        const firstCompleteIndex = customDatabases.findIndex(isCompleteCustomDb);
        effectiveSelectionValue = firstCompleteIndex >= 0 ? `custom-${firstCompleteIndex}` : null;
      }
    }

    if (effectiveSelectionValue) {
      if (effectiveSelectionValue.startsWith('custom-')) {
        isCustomOption = true;
        const index = parseInt(effectiveSelectionValue.split('-')[1]);
        const selectedDb = customDatabases[index];
        const chosen = isCompleteCustomDb(selectedDb) ? selectedDb : customDatabases.find(isCompleteCustomDb);

        if (isCompleteBigQuery(chosen)) {
          dbType = 'bigquery';
          dbProjectId = chosen.config.project_id;
          dbDataset = chosen.config.dataset;
          dbBillingProjectId = chosen.config.billing_project_id;
          // May be blank if the user didn't re-paste a key while just
          // re-selecting/renaming an already-saved connection - the
          // server reuses the previously-stored key in that case (it's
          // never sent back to us to re-display, see get_db_connections).
          dbCredentialsJson = chosen.config.credentials_json || null;
          dbNameValue = chosen.name || dbDataset;
          // No dbUrlValue here - BigQuery has no real url of its own, and
          // the server never reads payload.database_url for this type
          // (see config_routes.py's module docstring / _parse_incoming_
          // connection), so there's nothing meaningful to send.
        } else if (isCompleteSnowflake(chosen)) {
          dbType = 'snowflake';
          dbAccount = chosen.config.account;
          dbUser = chosen.config.user;
          dbWarehouse = chosen.config.warehouse;
          dbDatabase = chosen.config.database;
          dbSchema = chosen.config.schema || null;
          dbRole = chosen.config.role || null;
          // Same "may be blank, server reuses the saved one" rule as
          // BigQuery's credentials_json above - neither password nor
          // private_key is ever sent back to redisplay.
          dbPassword = chosen.config.password || null;
          dbPrivateKey = chosen.config.private_key || null;
          dbPrivateKeyPassphrase = chosen.config.private_key_passphrase || null;
          dbNameValue = chosen.name || dbDatabase;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteDatabricks(chosen)) {
          dbType = 'databricks';
          dbServerHostname = chosen.config.server_hostname;
          dbHttpPath = chosen.config.http_path;
          dbCatalog = chosen.config.catalog || null;
          dbSchema = chosen.config.schema || null;
          // May be blank if the user didn't re-paste a token while just
          // re-selecting/renaming an already-saved connection - the server
          // reuses the previously-stored token in that case (it's never
          // sent back to us to re-display, see get_db_connections).
          dbAccessToken = chosen.config.access_token || null;
          dbNameValue = chosen.name || dbHttpPath;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteOracle(chosen)) {
          dbType = 'oracle';
          dbHost = chosen.config.host;
          dbPort = chosen.config.port || null;
          dbServiceName = chosen.config.service_name || null;
          dbSid = chosen.config.sid || null;
          dbUser = chosen.config.user;
          dbSchema = chosen.config.schema || null;
          // May be blank if the user didn't retype a password while just
          // re-selecting/renaming an already-saved connection - the server
          // reuses the previously-stored password in that case (it's never
          // sent back to us to re-display, see get_db_connections).
          dbPassword = chosen.config.password || null;
          dbSsl = Boolean(chosen.config.ssl);
          dbNameValue = chosen.name || dbServiceName || dbSid;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteRedshift(chosen)) {
          dbType = 'redshift';
          dbHost = chosen.config.host;
          dbPort = chosen.config.port || null;
          dbDatabase = chosen.config.database;
          dbUser = chosen.config.user;
          dbSchema = chosen.config.schema || null;
          // May be blank if the user didn't retype a password while just
          // re-selecting/renaming an already-saved connection - the server
          // reuses the previously-stored password in that case (it's never
          // sent back to us to re-display, see get_db_connections).
          dbPassword = chosen.config.password || null;
          dbNameValue = chosen.name || dbDatabase;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteMssql(chosen)) {
          dbType = 'mssql';
          dbHost = chosen.config.host;
          dbPort = chosen.config.port || null;
          dbDatabase = chosen.config.database;
          dbUser = chosen.config.user;
          dbSchema = chosen.config.schema || null;
          // May be blank if the user didn't retype a password while just
          // re-selecting/renaming an already-saved connection - the server
          // reuses the previously-stored password in that case (it's never
          // sent back to us to re-display, see get_db_connections).
          dbPassword = chosen.config.password || null;
          // Unlike dbSsl above, absence here means "on" (see
          // backends/mssql.py's module docstring) - so this reads as
          // "explicitly false" vs. "anything else (including undefined)",
          // not truthy vs. falsy.
          dbEncrypt = chosen.config.encrypt !== false;
          dbNameValue = chosen.name || dbDatabase;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteSheets(chosen)) {
          dbType = 'sheets';
          dbSpreadsheetUrl = chosen.config.spreadsheet_url;
          dbTabName = chosen.config.tab_name;
          // Never re-displayed by the server (see state_store.py's
          // _CREDENTIAL_CONFIG_FIELDS), so this is only ever non-null when
          // the user just typed a new one in this same editing session -
          // mirrors dbPassword's own restore line above.
          dbSheetsCredentialsJson = chosen.config.credentials_json || null;
          dbNameValue = chosen.name || dbTabName;
          // No dbUrlValue here - see the BigQuery branch's comment above.
        } else if (isCompleteMongo(chosen)) {
          dbType = 'MongoDB';
          dbUrlValue = chosen.url;
          dbDatabase = chosen.config.database;
          dbUser = chosen.config.user;
          // May be blank if the user didn't retype a password while just
          // re-selecting/renaming an already-saved connection - the server
          // reuses the previously-stored password in that case (it's never
          // sent back to us to re-display, see get_db_connections).
          dbPassword = chosen.config.password || null;
          dbNameValue = chosen.name || dbDatabase;
        } else if (isCompleteSimpleUrlDb(chosen)) {
          dbType = chosen.type === 'mysql' ? 'mysql' : 'postgres';
          dbUrlValue = chosen.url;
          dbNameValue = chosen.name;
          // Postgres/MySQL support ca_cert_pem (see backends/postgres.py's
          // and backends/mysql.py's module docstrings).
          dbCaCertPem = (chosen.config && chosen.config.ca_cert_pem) || null;
        } else {
          dbType = 'postgres';
          dbUrlValue = DEFAULT_DB_URL;
          dbNameValue = "Default DB";
          isCustomOption = false;
        }

        customDbName = dbNameValue;
        customDbUrl = dbUrlValue;
      } else if (effectiveSelectionValue.startsWith('preset:')) {
        // Both anonymous and signed-in users select a preset purely by its
        // stable, non-secret id (see renderDbRadioButtons()) - never by
        // resending its own fields, let alone its credentials. The server
        // resolves the preset's actual connection details fresh from
        // CONFIGURED_DBS every time it's actually used (see db.py's
        // resolve_active_descriptor) and never persists them on the
        // session, so there's nothing else to send here regardless of
        // whether this visitor is signed in - matchedDb is only a
        // name+type+id skeleton for an anonymous user (see
        // fetchBackendConfig()'s redacted configured_databases) but the
        // full preset descriptor for a signed-in one, since presets aren't
        // redacted for them; either way, only its id and name are used
        // below (dbType/dbNameValue are display-only for this payload -
        // the server ignores them for a preset selection).
        const matchedPresetId = effectiveSelectionValue.slice('preset:'.length);
        const matchedDb = CONFIGURED_DBS.find(db => db.id === matchedPresetId);
        dbType = (matchedDb && matchedDb.type) || 'postgres';
        dbNameValue = matchedDb ? matchedDb.name : "Preset DB";
        presetId = matchedPresetId;
      }
    } else {
      dbUrlValue = DEFAULT_DB_URL;
      dbNameValue = "Default DB";
    }

    const payload = {
      database_name: dbNameValue,
      database_type: dbType,
      is_custom: isCustomOption,
      custom_databases: customDatabases
        .filter(d => isCompleteBigQuery(d) || isCompleteSnowflake(d) || isCompleteDatabricks(d) || isCompleteOracle(d) || isCompleteRedshift(d) || isCompleteMssql(d) || isCompleteSheets(d) || isCompleteMongo(d) || isCompleteSimpleUrlDb(d))
        .map(d => {
          if (isCompleteBigQuery(d)) {
            return {
              type: 'bigquery',
              name: d.name,
              project_id: d.config.project_id,
              dataset: d.config.dataset,
              billing_project_id: d.config.billing_project_id,
              credentials_json: d.config.credentials_json || undefined
            };
          }
          if (isCompleteSnowflake(d)) {
            return {
              type: 'snowflake',
              name: d.name,
              account: d.config.account,
              user: d.config.user,
              warehouse: d.config.warehouse,
              database: d.config.database,
              schema: d.config.schema || undefined,
              role: d.config.role || undefined,
              password: d.config.password || undefined,
              private_key: d.config.private_key || undefined,
              private_key_passphrase: d.config.private_key_passphrase || undefined,
            };
          }
          if (isCompleteDatabricks(d)) {
            return {
              type: 'databricks',
              name: d.name,
              server_hostname: d.config.server_hostname,
              http_path: d.config.http_path,
              catalog: d.config.catalog || undefined,
              schema: d.config.schema || undefined,
              access_token: d.config.access_token || undefined,
            };
          }
          if (isCompleteOracle(d)) {
            return {
              type: 'oracle',
              name: d.name,
              host: d.config.host,
              port: d.config.port || undefined,
              service_name: d.config.service_name || undefined,
              sid: d.config.sid || undefined,
              user: d.config.user,
              schema: d.config.schema || undefined,
              password: d.config.password || undefined,
              ssl: d.config.ssl || undefined,
            };
          }
          if (isCompleteRedshift(d)) {
            return {
              type: 'redshift',
              name: d.name,
              host: d.config.host,
              port: d.config.port || undefined,
              database: d.config.database,
              user: d.config.user,
              schema: d.config.schema || undefined,
              password: d.config.password || undefined,
            };
          }
          if (isCompleteMssql(d)) {
            return {
              type: 'mssql',
              name: d.name,
              host: d.config.host,
              port: d.config.port || undefined,
              database: d.config.database,
              user: d.config.user,
              schema: d.config.schema || undefined,
              password: d.config.password || undefined,
              // Unlike every other optional field above (omitted via
              // "|| undefined" when blank), "encrypt" is always sent
              // explicitly as true/false - it's a meaningful boolean where
              // an explicit false and an absent value are different things
              // (see backends/mssql.py's module docstring: connect()
              // itself defaults to True only when the key is missing
              // entirely) - so this must never collapse to undefined.
              encrypt: d.config.encrypt !== false,
            };
          }
          if (isCompleteSheets(d)) {
            return {
              type: 'sheets',
              name: d.name,
              spreadsheet_url: d.config.spreadsheet_url,
              tab_name: d.config.tab_name,
              // Optional, and only ever sent when the user actually typed
              // one in this editing session - omitted (not sent as an
              // empty string) so a blank textarea never clobbers an
              // already-saved key server-side (_resolve_sheets_credentials
              // falls back to the saved one only when nothing is provided).
              credentials_json: d.config.credentials_json || undefined,
            };
          }
          if (isCompleteMongo(d)) {
            return {
              type: 'MongoDB',
              name: d.name,
              url: d.url,
              database: d.config.database,
              user: d.config.user,
              password: d.config.password || undefined,
            };
          }
          const simpleUrlType = d.type === 'mysql' ? 'mysql' : 'postgres';
          const simpleUrlOut = { type: simpleUrlType, name: d.name, url: d.url };
          // Shared by Postgres/MySQL (see backends/postgres.py's and
          // backends/mysql.py's module docstrings) - not a credential, so
          // it's just carried through as-is like BigQuery's
          // billing_project_id, not resolved via a "leave blank to keep
          // the saved one" helper the way passwords are.
          if (d.config && d.config.ca_cert_pem) {
            simpleUrlOut.ca_cert_pem = d.config.ca_cert_pem;
          }
          return simpleUrlOut;
        }),
    };
    if (presetId !== null) {
      payload.preset_id = presetId;
    } else if (dbType === 'bigquery') {
      payload.project_id = dbProjectId;
      payload.dataset = dbDataset;
      if (dbBillingProjectId) payload.billing_project_id = dbBillingProjectId;
      if (dbCredentialsJson) payload.credentials_json = dbCredentialsJson;
    } else if (dbType === 'snowflake') {
      payload.account = dbAccount;
      payload.user = dbUser;
      payload.warehouse = dbWarehouse;
      payload.database = dbDatabase;
      if (dbSchema) payload.schema = dbSchema;
      if (dbRole) payload.role = dbRole;
      if (dbPassword) payload.password = dbPassword;
      if (dbPrivateKey) payload.private_key = dbPrivateKey;
      if (dbPrivateKeyPassphrase) payload.private_key_passphrase = dbPrivateKeyPassphrase;
    } else if (dbType === 'databricks') {
      payload.server_hostname = dbServerHostname;
      payload.http_path = dbHttpPath;
      if (dbCatalog) payload.catalog = dbCatalog;
      if (dbSchema) payload.schema = dbSchema;
      if (dbAccessToken) payload.access_token = dbAccessToken;
    } else if (dbType === 'oracle') {
      payload.host = dbHost;
      if (dbPort) payload.port = dbPort;
      if (dbServiceName) payload.service_name = dbServiceName;
      if (dbSid) payload.sid = dbSid;
      payload.user = dbUser;
      if (dbSchema) payload.schema = dbSchema;
      if (dbPassword) payload.password = dbPassword;
      if (dbSsl) payload.ssl = true;
    } else if (dbType === 'redshift') {
      payload.host = dbHost;
      if (dbPort) payload.port = dbPort;
      payload.database = dbDatabase;
      payload.user = dbUser;
      if (dbSchema) payload.schema = dbSchema;
      if (dbPassword) payload.password = dbPassword;
    } else if (dbType === 'mssql') {
      payload.host = dbHost;
      if (dbPort) payload.port = dbPort;
      payload.database = dbDatabase;
      payload.user = dbUser;
      if (dbSchema) payload.schema = dbSchema;
      if (dbPassword) payload.password = dbPassword;
      // Always explicit, never conditional like dbSsl above - see the
      // customDatabases.map() branch's comment for why "encrypt" can't be
      // safely omitted the way every other optional field here is.
      payload.encrypt = dbEncrypt !== false;
    } else if (dbType === 'sheets') {
      payload.spreadsheet_url = dbSpreadsheetUrl;
      payload.tab_name = dbTabName;
      // Only sent when non-blank, same "don't clobber a saved key" rule
      // as dbPassword above.
      if (dbSheetsCredentialsJson) payload.credentials_json = dbSheetsCredentialsJson;
    } else if (dbType === 'MongoDB') {
      // Unlike every other structured dialect above, MongoDB also has a
      // real url (see backends/mongodb_sql.py's module docstring) - sent
      // as database_url like Postgres/MySQL, alongside the three
      // structured fields matching config_routes.py's
      // _parse_incoming_connection mongo branch.
      payload.database_url = dbUrlValue;
      payload.database = dbDatabase;
      payload.user = dbUser;
      if (dbPassword) payload.password = dbPassword;
    } else {
      payload.database_url = dbUrlValue;
      // Both simple-URL dialects support ca_cert_pem (see
      // backends/postgres.py's and backends/mysql.py's module docstrings).
      if (dbCaCertPem) payload.ca_cert_pem = dbCaCertPem;
    }

    const configSaveErrorEl = document.getElementById('configSaveError');

    // Multi-database question-answering (see server/translate_routes.py's
    // module docstring): the picker is a binary single-select choice again
    // (see renderDbRadioButtons()) - one specific connection, or "All".
    // in_scope_mode is what the server actually keys its behavior off of
    // (see db.py's resolve_in_scope_descriptors/
    // _resolve_all_configured_descriptors): "all" is expanded dynamically,
    // at request time, to every connection configured THEN - not a list
    // frozen at Save time, which is the whole point of "All" over the old
    // arbitrary-checkbox picker. Picking one SPECIFIC connection still
    // narrows scope back down to exactly that one immediately, below,
    // which is what keeps a single in-scope connection's behavior
    // byte-identical to before this feature existed.
    const allMode = selectedDbRadio && selectedDbRadio.value === 'all';
    payload.in_scope_mode = allMode ? 'all' : 'single';

    if (!allMode) {
      // A custom row with no connection_key yet (freshly added and
      // completed in this SAME save) can't be represented in the in-scope
      // arrays at all until a follow-up save actually persists it and
      // assigns one (see _parse_incoming_custom_databases' docstring) - so
      // in_scope_preset_ids/in_scope_custom_connection_keys are left
      // unset entirely in that one case (same as "All" above: the server
      // leaves whatever scope was previously saved alone) rather than sent
      // as empty arrays, which would otherwise trip the server's own "at
      // least one connection must be in scope" validation despite a
      // perfectly valid connection having just been selected.
      if (effectiveSelectionValue && effectiveSelectionValue.startsWith('preset:')) {
        payload.in_scope_preset_ids = [effectiveSelectionValue.slice('preset:'.length)];
        payload.in_scope_custom_connection_keys = [];
      } else if (effectiveSelectionValue && effectiveSelectionValue.startsWith('custom-')) {
        const index = parseInt(effectiveSelectionValue.split('-')[1], 10);
        const db = customDatabases[index];
        if (db && db.connection_key) {
          payload.in_scope_preset_ids = [];
          payload.in_scope_custom_connection_keys = [db.connection_key];
        }
      }
    }

    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify(payload)
      });

      if (response.ok) {
        const data = await response.json();
        // Captured BEFORE the ACTIVE_* globals below get overwritten, so
        // this reflects the connection that was active going into this
        // save. Compared against the same tuple after the update to detect
        // an actual connection change (see clearActiveQueryState() below) -
        // url/is_custom/connection_key/preset_id together are what uniquely
        // identify "the" active connection (custom connections:
        // connection_key; presets: preset_id, which now works the same way
        // for anonymous and signed-in users alike - see "what makes a db
        // connection unique" discussion).
        const previousConnectionIdentity = `${ACTIVE_DB_URL}|${ACTIVE_IS_CUSTOM}|${ACTIVE_CUSTOM_CONNECTION_KEY}|${ACTIVE_PRESET_ID}`;
        if (data.active_database_url) {
          ACTIVE_DB_URL = data.active_database_url;
        }
        if (data.active_is_custom !== undefined) {
          ACTIVE_IS_CUSTOM = Boolean(data.active_is_custom);
        }
        if (data.active_custom_connection_key !== undefined) {
          ACTIVE_CUSTOM_CONNECTION_KEY = data.active_custom_connection_key || "";
        }
        if (data.active_uses_custom_credentials !== undefined) {
          ACTIVE_USES_CUSTOM_CREDENTIALS = Boolean(data.active_uses_custom_credentials);
        }
        if (data.active_preset_id !== undefined) {
          ACTIVE_PRESET_ID = data.active_preset_id ?? null;
        }
        if (data.custom_database_name !== undefined) {
          customDbName = data.custom_database_name;
        }
        if (data.custom_database_url !== undefined) {
          customDbUrl = data.custom_database_url;
        }
        if (data.custom_databases !== undefined) {
          customDatabases = data.custom_databases;
        }
        if (data.auto_sql_execute !== undefined) {
          autoSqlExecuteEnabled = Boolean(data.auto_sql_execute);
        }
        if (data.in_scope_preset_ids !== undefined) {
          IN_SCOPE_PRESET_IDS = data.in_scope_preset_ids || [];
        }
        if (data.in_scope_custom_connection_keys !== undefined) {
          IN_SCOPE_CUSTOM_KEYS = data.in_scope_custom_connection_keys || [];
        }
        if (data.in_scope_mode !== undefined) {
          IN_SCOPE_MODE = data.in_scope_mode === 'all' ? 'all' : 'single';
        }

        const nextConnectionIdentity = `${ACTIVE_DB_URL}|${ACTIVE_IS_CUSTOM}|${ACTIVE_CUSTOM_CONNECTION_KEY}|${ACTIVE_PRESET_ID}`;
        if (nextConnectionIdentity !== previousConnectionIdentity) {
          clearActiveQueryState();
        } else if (PINNED_CONNECTIONS.some(p => (
          p.kind === 'preset' ? !IN_SCOPE_PRESET_IDS.includes(p.id) : !IN_SCOPE_CUSTOM_KEYS.includes(p.id)
        ))) {
          // The primary connection didn't change, but a connection this
          // conversation had pinned (see PINNED_CONNECTIONS' docstring) was
          // just unchecked from scope - the pin no longer describes a set
          // the user actually wants questions routed to, so it's cleared
          // the same way a real connection-identity change would be. The
          // server independently guards against a stale pin too (see
          // execute_routes.py's resolve_descriptor_by_reference fallback,
          // which is the only place a client-echoed pinned_connections
          // entry is still read at all), this just keeps the UI's own
          // prompt/SQL/results in sync immediately rather than waiting for
          // the next execute call to discover it server-side.
          clearActiveQueryState();
        }

        if (configSaveErrorEl) {
          configSaveErrorEl.style.display = 'none';
          configSaveErrorEl.textContent = '';
        }

        await updateConnectionDetails(data);
        // Read from the badge (just refreshed by updateConnectionDetails()
        // above) rather than any of this function's own dbNameValue-shaped
        // locals - correct across every dialect/preset/custom-connection
        // branch above without needing to know which one just ran.
        trackEvent('database_selected', {
          database_name: connDbName ? connDbName.textContent : '',
          // dbType (this function's own local var, set per-dialect above)
          // rather than the module-level ACTIVE_DB_TYPE - it's already
          // computed for exactly this save and is correct immediately,
          // without waiting on ACTIVE_DB_TYPE's next fetchBackendConfig()
          // sync.
          database_type: dbType,
          is_custom: isCustomOption,
        });
      } else {
        // e.g. a custom BigQuery connection missing its required billing
        // project ID / service-account key (see config_routes.py's
        // _CUSTOM_BIGQUERY_MISSING_FIELDS_ERROR) - surfaced here rather
        // than silently doing nothing, and the modal is kept open (see
        // below) so the user can actually fix it.
        let errorMessage = 'Failed to save configuration.';
        try {
          const errData = await response.json();
          if (errData && errData.error) errorMessage = errData.error;
        } catch (parseErr) { /* non-JSON error body - keep the generic message */ }
        if (configSaveErrorEl) {
          configSaveErrorEl.textContent = errorMessage;
          configSaveErrorEl.style.display = '';
        }
        closeModal = false;
      }
    } catch (err) {
      console.error("Failed to save backend configuration:", err);
      if (connDbDot) connDbDot.className = 'status-dot disconnected';
    }

    if (closeModal) {
      closeConfigModal();
    }
  }

  function loadConfig() {
    return {
      dbUrl: ACTIVE_DB_URL || DEFAULT_DB_URL
    };
  }

  function loadConfigIntoUI() {
    const config = loadConfig();
    renderDbRadioButtons(config.dbUrl);
    updateHistoryTurnsSubtitle();
  }

  function closeConfigModal() {
    if (configModal) configModal.classList.add('hidden');
  }

  if (configTriggerBadge && configModal) {
    configTriggerBadge.addEventListener('click', async () => {
      // See setButtonsDisabled()'s own comment on badge-disabled - a query
      // is in flight, so opening this modal is blocked entirely rather
      // than just visually grayed out (the div has no native `disabled`
      // to rely on for that).
      if (configTriggerBadge.classList.contains('badge-disabled')) return;
      // Anonymous users may open this dialog too - they can switch between
      // admin-configured presets AND save their own custom connections
      // (see isAnonymousUser's comment above and config_routes.py's
      // handle_config).
      await fetchBackendConfig();
      const configSaveErrorEl = document.getElementById('configSaveError');
      if (configSaveErrorEl) {
        configSaveErrorEl.style.display = 'none';
        configSaveErrorEl.textContent = '';
      }
      configModal.classList.remove('hidden');
      bringModalToFront(configModal);
    });
  }

  if (modalCloseBtn && configModal) {
    modalCloseBtn.addEventListener('click', closeConfigModal);
  }

  // ===========================================================================
  // GUIDED TOUR (first-run onboarding walkthrough)
  // ===========================================================================
  const tourOverlay = document.getElementById('tourOverlay');
  const tourSpotlight = document.getElementById('tourSpotlight');
  const tourTooltip = document.getElementById('tourTooltip');
  const tourStepCounter = document.getElementById('tourStepCounter');
  const tourTooltipTitle = document.getElementById('tourTooltipTitle');
  const tourTooltipBody = document.getElementById('tourTooltipBody');
  const tourSkipBtn = document.getElementById('tourSkipBtn');
  const tourBackBtn = document.getElementById('tourBackBtn');
  const tourNextBtn = document.getElementById('tourNextBtn');

  let tourStepIndex = 0;
  let tourResizeHandler = null;

  function getTourSteps() {
    const promptWrapper = aiPrompt ? aiPrompt.closest('.speech-bubble-wrapper') : null;
    const sqlWrapper = document.querySelector('.sql-bubble');
    const resultsCard = document.querySelector('.table-card');
    const historyNav = document.querySelector('.inline-history-nav');
    const authContainer = googleAuthEnabled ? document.getElementById('g_id_signin') : null;
    const quickPrompts = document.getElementById('examplePrompts');
    const quickPromptsVisible = quickPrompts && !quickPrompts.classList.contains('hidden');
    // Under the narrow-header breakpoint, historyBtn/authContainer/helpBtn/
    // sendFeedbackBtn are CSS-hidden (collapsed into the triple-dot
    // #moreMenuBtn - see the MORE MENU section above) - they'd still exist
    // in the DOM, so pointing the tour at them directly would spotlight a
    // zero-size rect. Point at the visible moreMenuBtn instead, with one
    // combined step.
    const isNarrowHeader = !!(moreMenuWrapper && window.getComputedStyle(moreMenuWrapper).display !== 'none');
    // Mirrors sendFeedbackBtn/moreMenuFeedbackBtn's own visibility gate
    // (fetchBackendConfig() toggles both on ISSUE_REPORTING_ENABLED) -
    // reused here so the tour never spotlights a feature-flagged-off button,
    // wide header or narrow.
    const feedbackMenuClause = ISSUE_REPORTING_ENABLED ? ', send feedback,' : ',';

    const steps = [
      {
        target: promptWrapper,
        title: 'Ask your question here',
        body: "Type what you want to know in plain English or any other language and hit Enter."
      },
      {
        target: quickPromptsVisible ? quickPrompts : null,
        title: 'Not sure what to ask?',
        body: 'Click one of these example prompts to see the whole flow in action, from question to SQL to results.'
      },
      {
        target: sqlWrapper,
        title: "We'll turn that into SQL",
        body: "We'll translate your question into a SQL query here. Review it - or edit it by hand - then click Execute to run it."
      },
      {
        target: resultsCard,
        title: 'Your results land here',
        body: 'Query results show up in this table, ready to scroll through or use to ask a follow-up question.'
      },
      {
        target: historyNav,
        title: 'Step back through past turns',
        body: 'Use these arrows to move back and forward through your recent prompts, SQL, and results - handy for revisiting or tweaking an earlier question.'
      },
      {
        target: configTriggerBadge,
        title: "This is the database you are connected to",
        body: "Click this badge to switch to any pre-configured database or connect to your own."
      },
      {
        target: modelTriggerBadge,
        title: "This is the AI model translating your questions",
        body: "Click this badge to switch between the available models, grouped by provider (Google, Anthropic, OpenAI)."
      },
      ...(isNarrowHeader ? [{
        target: moreMenuBtn,
        title: 'Help, history, preferences & sign-in live here',
        body: isAnonymousUser
          ? `Tap this menu for the full docs, your past translations, your preferences (color theme and auto-execute)${feedbackMenuClause} and to sign in with Google so your connections and history follow you across devices.`
          : `Tap this menu for the full docs, your past translations, your preferences (color theme and auto-execute)${feedbackMenuClause} and to sign out.`
      }] : [
      {
        target: prefsBtn,
        title: 'Make it yours',
        body: 'Click this gear icon to switch between dark and light mode, and to control whether generated SQL runs automatically.'
      },
      {
        target: historyBtn,
        title: 'Past queries, saved',
        body: 'Every translation you run is saved here so you can revisit or reuse it later.'
      },
      {
        target: authContainer,
        title: isAnonymousUser ? 'Sign in to keep things around' : "You're signed in",
        body: isAnonymousUser
          ? "Sign in with Google here so your connections and history follow you across browsers and devices."
          : 'Sign out from here anytime.'
      },
      {
        target: helpBtn,
        title: 'Stuck? Full docs are here',
        body: 'Come back to this Help button anytime for the full walkthrough, tips on multi-turn conversations, and more.'
      },
      {
        target: ISSUE_REPORTING_ENABLED ? sendFeedbackBtn : null,
        title: 'Something not right? Let us know',
        body: 'Click this button anytime to send feedback, or report a translation, SQL query, or result that looks wrong.'
      }
      ])
    ];

    return steps.filter(s => s.target);
  }

  function positionTourStep(step) {
    const rect = step.target.getBoundingClientRect();
    const pad = 6;

    tourSpotlight.style.top = `${rect.top - pad}px`;
    tourSpotlight.style.left = `${rect.left - pad}px`;
    tourSpotlight.style.width = `${rect.width + pad * 2}px`;
    tourSpotlight.style.height = `${rect.height + pad * 2}px`;

    // Measure the tooltip so we can decide which side of the target it fits on.
    tourTooltip.style.visibility = 'hidden';
    tourTooltip.style.top = '0px';
    tourTooltip.style.left = '0px';
    const ttRect = tourTooltip.getBoundingClientRect();
    const margin = 14;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const spaceBelow = vh - rect.bottom;
    const spaceAbove = rect.top;
    let top;
    if (spaceBelow >= ttRect.height + margin || spaceBelow >= spaceAbove) {
      top = Math.min(rect.bottom + margin, vh - ttRect.height - margin);
    } else {
      top = Math.max(rect.top - ttRect.height - margin, margin);
    }
    top = Math.max(top, margin);

    let left = rect.left + rect.width / 2 - ttRect.width / 2;
    left = Math.min(Math.max(left, margin), vw - ttRect.width - margin);

    tourTooltip.style.top = `${top}px`;
    tourTooltip.style.left = `${left}px`;
    tourTooltip.style.visibility = 'visible';
  }

  function showTourStep(index) {
    const steps = getTourSteps();
    if (!steps.length) {
      finishTour();
      return;
    }
    tourStepIndex = Math.max(0, Math.min(index, steps.length - 1));
    const step = steps[tourStepIndex];

    tourStepCounter.textContent = `Step ${tourStepIndex + 1} of ${steps.length}`;
    tourTooltipTitle.textContent = step.title;
    tourTooltipBody.textContent = step.body;
    tourBackBtn.style.visibility = tourStepIndex === 0 ? 'hidden' : 'visible';
    tourNextBtn.textContent = tourStepIndex === steps.length - 1 ? 'Done' : 'Next';

    positionTourStep(step);
  }

  function startGuidedTour() {
    if (!tourOverlay) return;
    tourOverlay.classList.remove('hidden');
    tourStepIndex = 0;
    showTourStep(0);

    tourResizeHandler = () => {
      const steps = getTourSteps();
      if (steps[tourStepIndex]) positionTourStep(steps[tourStepIndex]);
    };
    window.addEventListener('resize', tourResizeHandler);
  }

  function finishTour() {
    // Already hidden - a no-op call (e.g. finishTour() reached twice in a
    // row) rather than a real exit, so skip re-tracking it. Every genuine
    // exit path (Skip, clicking "Done" on the last step, and the
    // zero-matching-steps edge case in showTourStep()) funnels through
    // here, so this is the one place that needs the trackEvent() call
    // rather than duplicating it at each button handler.
    if (!tourOverlay || tourOverlay.classList.contains('hidden')) return;
    // 1-based, matching the "Step X of Y" counter the user was just looking
    // at (see showTourStep()) - not a 0-based array index.
    trackEvent('tour_exited', { step: tourStepIndex + 1 });
    tourOverlay.classList.add('hidden');
    if (tourResizeHandler) {
      window.removeEventListener('resize', tourResizeHandler);
      tourResizeHandler = null;
    }
  }

  if (tourNextBtn) {
    tourNextBtn.addEventListener('click', () => {
      const steps = getTourSteps();
      if (tourStepIndex >= steps.length - 1) {
        finishTour();
      } else {
        showTourStep(tourStepIndex + 1);
      }
    });
  }
  if (tourBackBtn) {
    tourBackBtn.addEventListener('click', () => showTourStep(tourStepIndex - 1));
  }
  if (tourSkipBtn) {
    tourSkipBtn.addEventListener('click', finishTour);
  }

  // First-run onboarding: two independent things, both gated on their own
  // localStorage flag so returning users don't see either again.
  //   1. ONBOARDING_SEEN_KEY - controls the one-time auto-open of Help on
  //      a brand-new session. Set as soon as Help has been shown once
  //      (auto-opened or manually clicked), regardless of how it's closed.
  //   2. HELP_PULSE_DISMISSED_KEY - controls the pulsing ring on the Help
  //      button. This one is deliberately NOT cleared by the auto-open or
  //      by closing the modal - it only stops pulsing once the user
  //      actually clicks the Help button themselves, so someone who just
  //      dismisses the auto-opened popup still has a visible cue that
  //      there's a Help button worth clicking.
  const ONBOARDING_SEEN_KEY = 'ydylOnboardingSeen';
  const HELP_PULSE_DISMISSED_KEY = 'ydylHelpPulseDismissed';

  // ===========================================================================
  // 6. HELP BUTTON ONBOARDING (auto-open once, pulsing ring)
  // ===========================================================================
  function hasSeenOnboarding() {
    try {
      return localStorage.getItem(ONBOARDING_SEEN_KEY) === '1';
    } catch (e) {
      return true; // localStorage unavailable (private mode, etc.) - don't nag
    }
  }
  function markOnboardingSeen() {
    try {
      localStorage.setItem(ONBOARDING_SEEN_KEY, '1');
    } catch (e) { /* ignore */ }
  }
  function hasHelpPulseDismissed() {
    try {
      return localStorage.getItem(HELP_PULSE_DISMISSED_KEY) === '1';
    } catch (e) {
      return true; // localStorage unavailable - don't nag
    }
  }
  function dismissHelpPulse() {
    try {
      localStorage.setItem(HELP_PULSE_DISMISSED_KEY, '1');
    } catch (e) { /* ignore */ }
    if (helpBtn) helpBtn.classList.remove('help-btn-attention');
  }

  if (helpBtn && helpModal) {
    if (!hasHelpPulseDismissed()) {
      helpBtn.classList.add('help-btn-attention');
    }
    helpBtn.addEventListener('click', () => {
      trackEvent('help_viewed', {});
      openHelpModal();
      markOnboardingSeen();
      dismissHelpPulse();
    });
  }

  if (helpModalCloseBtn && helpModal) {
    helpModalCloseBtn.addEventListener('click', () => {
      helpModal.classList.add('hidden');
      markOnboardingSeen();
    });
  }

  // "Replay guided tour" - lives inside the Help modal (next to "Show
  // quick prompts again") so anyone - not just during development - can
  // re-run the walkthrough without digging through localStorage.
  const replayTourBtn = document.getElementById('replayTourBtn');
  if (replayTourBtn && helpModal) {
    replayTourBtn.addEventListener('click', () => {
      helpModal.classList.add('hidden');
      startGuidedTour();
    });
  }

  // ===========================================================================
  // 7. HISTORY MODAL: TABS, STATS CHARTS, LOAD/PURGE
  // ===========================================================================
  if (tabBtnTranslations && tabBtnStatistics) {
    tabBtnTranslations.addEventListener('click', () => {
      tabBtnTranslations.classList.add('active');
      tabBtnStatistics.classList.remove('active');
      if (historyTabTranslations) historyTabTranslations.classList.remove('hidden');
      if (historyTabStatistics) historyTabStatistics.classList.add('hidden');
    });

    tabBtnStatistics.addEventListener('click', () => {
      tabBtnStatistics.classList.add('active');
      tabBtnTranslations.classList.remove('active');
      if (historyTabStatistics) historyTabStatistics.classList.remove('hidden');
      if (historyTabTranslations) historyTabTranslations.classList.add('hidden');

      requestAnimationFrame(() => {
        if (chartCountInstance) chartCountInstance.resize();
        if (chartTotalTokensInstance) chartTotalTokensInstance.resize();
      });
    });
  }

  // Cached so setTheme() can rebuild these charts with the new theme's
  // colors without needing to re-fetch /api/history's stats - Chart.js
  // bakes resolved color strings into its config at creation time and
  // never re-reads CSS custom properties on its own.
  let lastStatsDataForCharts = null;

  function renderStatisticsCharts(statsData) {
    if (!statsData || statsData.length === 0 || typeof window.Chart === 'undefined') return;
    lastStatsDataForCharts = statsData;

    const dates = statsData.map(item => item.day_date || item.date || 'Unknown');
    const totalTranslations = statsData.map(item => item.total_translations || 0);
    const sumTotalTokens = statsData.map(item => item.sum_total_tokens || 0);

    // Read the active theme's resolved colors rather than hardcoding hex
    // values, so these charts stay correct in both themes (see the THEME
    // SWITCHING section, which rebuilds these charts from
    // lastStatsDataForCharts whenever the theme changes).
    const rootStyle = getComputedStyle(document.documentElement);
    const tickColor = rootStyle.getPropertyValue('--text-secondary').trim() || '#94a3b8';
    const gridColor = rootStyle.getPropertyValue('--overlay-1').trim() || 'rgba(255,255,255,0.05)';
    const cyanColor = rootStyle.getPropertyValue('--accent-cyan').trim() || '#38bdf8';
    const cyanRgb = rootStyle.getPropertyValue('--accent-cyan-rgb').trim() || '56, 189, 248';
    const primaryColor = rootStyle.getPropertyValue('--primary').trim() || '#10b981';
    const primaryRgb = rootStyle.getPropertyValue('--primary-rgb').trim() || '16, 185, 129';

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        x: { ticks: { color: tickColor, font: { size: 10 } }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, font: { size: 10 } }, grid: { color: gridColor } }
      }
    };

    const ctxCount = document.getElementById('chartTranslationsPerDay')?.getContext('2d');
    if (ctxCount) {
      if (chartCountInstance) chartCountInstance.destroy();
      chartCountInstance = new window.Chart(ctxCount, {
        type: 'bar',
        data: {
          labels: dates,
          datasets: [{
            label: 'Total Translations',
            data: totalTranslations,
            backgroundColor: `rgba(${cyanRgb}, 0.6)`,
            borderColor: cyanColor,
            borderWidth: 1
          }]
        },
        options: commonOptions
      });
    }

    const ctxTotalTokens = document.getElementById('chartTotalTokensPerDay')?.getContext('2d');
    if (ctxTotalTokens) {
      if (chartTotalTokensInstance) chartTotalTokensInstance.destroy();
      chartTotalTokensInstance = new window.Chart(ctxTotalTokens, {
        type: 'bar',
        data: {
          labels: dates,
          datasets: [{
            label: 'Sum of Total Tokens',
            data: sumTotalTokens,
            backgroundColor: `rgba(${primaryRgb}, 0.6)`,
            borderColor: primaryColor,
            borderWidth: 1
          }]
        },
        options: commonOptions
      });
    }
  }

  function showConfirmDialog(message) {
    return new Promise((resolve) => {
      const modal = document.getElementById('confirmModal');
      const textEl = document.getElementById('confirmModalText');
      const okBtn = document.getElementById('confirmModalOkBtn');
      const cancelBtn = document.getElementById('confirmModalCancelBtn');
      const closeBtn = document.getElementById('confirmModalCloseBtn');

      if (!modal || !textEl || !okBtn || !cancelBtn) {
        resolve(confirm(message));
        return;
      }

      textEl.textContent = message;
      modal.classList.remove('hidden');
      bringModalToFront(modal);

      let cleanedUp = false;
      const cleanup = (result) => {
        if (cleanedUp) return;
        cleanedUp = true;
        modal.classList.add('hidden');
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        closeBtn?.removeEventListener('click', onCancel);
        modal.removeEventListener('click', onOutside);
        resolve(result);
      };

      const onOk = () => cleanup(true);
      const onCancel = () => cleanup(false);
      const onOutside = (e) => {
        if (e.target === modal) cleanup(false);
      };

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      closeBtn?.addEventListener('click', onCancel);
      modal.addEventListener('click', onOutside);
    });
  }

  // ===========================================================================
  // REPORT ISSUE MODAL (Report Error / Report Wrong Result / Send Feedback -
  // see report_routes.py's module docstring and setReportContext()/
  // reportButtonHtml() above). This modal IS the "review exactly what
  // you're about to report before reporting" step the feature requires -
  // there's no separate confirmation on top of it, Send just submits
  // whatever the preview below is currently showing.
  // ===========================================================================

  // Per-category copy for the modal - keeps openReportIssueModal()/
  // sendReportIssue() from needing their own if/else ladder for every piece
  // of text that differs between "reporting something about a specific
  // result" (error/wrong_result) and "general feedback about the app,
  // unrelated to any result" (feedback - see the Help dialog's "Send
  // Feedback" button). `previewLabel` matches report_routes.py's
  // _VALID_CATEGORIES value exactly, since it's echoed into both the email
  // subject/body there and this preview here - keep the two in sync if
  // either changes.
  const REPORT_CATEGORY_CONFIG = {
    error: {
      modalTitle: 'Report Error',
      previewLabel: 'Execution Error',
      intro: 'Review what will be emailed below, add any extra details, then send. This does not fix or retry anything - it just lets the developer know something looks wrong.',
      showPreview: true,
      detailsLabel: 'Additional details (optional)',
      detailsPlaceholder: 'Anything else that would help - what you expected instead, steps to reproduce, etc.',
      detailsRequired: false,
      sendLabel: 'Send Report',
      sendingLabel: 'Sending…',
    },
    wrong_result: {
      modalTitle: 'Report Wrong Result',
      previewLabel: 'Wrong Result',
      intro: 'Review what will be emailed below, add any extra details, then send. This does not fix or retry anything - it just lets the developer know something looks wrong.',
      showPreview: true,
      detailsLabel: 'Additional details (optional)',
      detailsPlaceholder: 'Anything else that would help - what you expected instead, steps to reproduce, etc.',
      detailsRequired: false,
      sendLabel: 'Send Report',
      sendingLabel: 'Sending…',
    },
    // No result/error to preview here at all (see report_routes.py's
    // module docstring on why 'feedback' omits prompt/sql/content
    // entirely) - showPreview:false hides that whole section, and the
    // details textarea (normally an optional add-on) becomes the entire
    // message, hence detailsRequired:true - an empty send would otherwise
    // produce a blank email with nothing for a reviewer to act on.
    feedback: {
      modalTitle: 'Send Feedback',
      previewLabel: 'Feedback',
      intro: 'Have a suggestion, question, or comment about Datalect? Send it straight to the developer - no mail client required.',
      showPreview: false,
      detailsLabel: 'Your feedback',
      detailsPlaceholder: "What's on your mind?",
      detailsRequired: true,
      sendLabel: 'Send Feedback',
      sendingLabel: 'Sending…',
    },
    // Triggered from the SQL box's own thumbs-down button (#reportSqlBtn),
    // not a results tab - independent of whether the SQL has ever been run.
    // Unlike every other category, previewEditable:true means the preview
    // itself is a plain <textarea> (#reportIssuePreviewEditable, seeded by
    // renderWrongSqlPreviewSeed()) that the user can freely rewrite before
    // sending - see report_routes.py's module docstring on 'wrong_sql' for
    // why the server only ever sees the edited result, bundled into
    // `content`, rather than separate prompt/sql fields. detailsLabel below
    // is a genuinely separate, optional comment box underneath that
    // editable text - contrast with 'feedback', where the details box IS
    // the whole message.
    wrong_sql: {
      modalTitle: 'Report Wrong SQL',
      previewLabel: 'Wrong SQL',
      intro: 'Review and edit the prompt/SQL below as needed, add any comments, then send. This does not fix or retry anything - it just lets the developer know the generated SQL looks wrong.',
      showPreview: true,
      previewEditable: true,
      previewSectionLabel: 'What will be sent (edit as needed)',
      detailsLabel: 'Additional comments (optional)',
      detailsPlaceholder: "What's wrong with this SQL, or what did you expect instead?",
      detailsRequired: false,
      sendLabel: 'Report Wrong SQL',
      sendingLabel: 'Sending…',
    },
  };

  function reportCategoryConfig(category) {
    return REPORT_CATEGORY_CONFIG[category] || REPORT_CATEGORY_CONFIG.error;
  }

  // The user's own question for this turn - preferring whatever's live in
  // the prompt box (covers direct-SQL-execution turns too, where there may
  // never have been a translate() call at all) and falling back to the
  // last completed turn's prompt (covers the common case: the user already
  // cleared/changed the prompt box after translating, but the result being
  // reported is still from that earlier turn). Never called for 'feedback'
  // (see buildReportPayload) - there's no "turn" a general comment about
  // the app is attached to.
  function getReportPromptText() {
    if (aiPrompt && aiPrompt.value.trim()) return aiPrompt.value.trim();
    const turn = chatStore.lastTurn();
    return (turn && turn.userEntry && turn.userEntry.text) || '';
  }

  // Builds the exact JSON body /api/report-issue expects (see that route's
  // docstring) from `context` (defaulting to activeReportContext - see its
  // own docstring for why that, not currentReportContext, is read here)
  // plus whatever else is available module-wide at report time - null when
  // there's nothing to report, which openReportIssueModal()/
  // sendReportIssue() both treat as "the button shouldn't have been
  // clickable in the first place, no-op".
  function buildReportPayload(details, context) {
    const ctx = context || activeReportContext;
    if (!ctx) return null;
    if (ctx.category === 'feedback') {
      // Deliberately just these two fields - see report_routes.py's module
      // docstring on why prompt/sql/database_name/content don't apply to
      // general app feedback the way they do for the other two categories.
      return { category: 'feedback', details: details || '' };
    }
    if (ctx.category === 'wrong_sql') {
      // Unlike error/wrong_result (prompt/sql captured automatically and
      // shown read-only below), this category's whole point is that the
      // user can rewrite the captured prompt+SQL text before it's sent -
      // see REPORT_CATEGORY_CONFIG.wrong_sql's previewEditable flag - so
      // `content` is read straight from the editable preview textarea's
      // live value, not from ctx.sql/getReportPromptText() directly. Empty
      // here the first time this runs, at modal-open (before
      // openReportIssueModal() has seeded the textarea via
      // renderWrongSqlPreviewSeed()) - harmless, since that call only uses
      // this to confirm ctx is truthy.
      return {
        category: 'wrong_sql',
        database_name: ctx.databaseName || (connDbName ? connDbName.textContent : ''),
        provider: ACTIVE_LLM_PROVIDER || '',
        model: ACTIVE_LLM_MODEL || '',
        content: reportIssuePreviewEditable ? reportIssuePreviewEditable.value.trim() : '',
        details: details || '',
      };
    }
    return {
      category: ctx.category,
      prompt: getReportPromptText(),
      sql: ctx.sql || '',
      database_name: ctx.databaseName || (connDbName ? connDbName.textContent : ''),
      provider: ACTIVE_LLM_PROVIDER || '',
      model: ACTIVE_LLM_MODEL || '',
      content: ctx.content || '',
      details: details || '',
    };
  }

  // Plain-text rendering of `payload` for #reportIssuePreview - deliberately
  // mirrors report_routes.py's own _build_email() section-by-section shape
  // (minus "Reported by"/"Additional details", which the server fills in
  // from the authenticated session and the textarea respectively - the
  // preview shows the user everything THEY are contributing, not fields the
  // server derives independently) so what's previewed here reads as a
  // faithful preview of the real email body, not an approximation of it.
  // Only ever called for error/wrong_result - 'feedback' hides this section
  // entirely (see REPORT_CATEGORY_CONFIG.feedback.showPreview) since there's
  // nothing structured to preview.
  function renderReportPreviewText(payload) {
    const categoryLabel = reportCategoryConfig(payload.category).previewLabel;
    const lines = [`Category: ${categoryLabel}`];
    if (payload.provider || payload.model) lines.push(`LLM: ${payload.provider} / ${payload.model}`);
    if (payload.database_name) lines.push(`Database/connection: ${payload.database_name}`);
    lines.push('');
    if (payload.prompt) lines.push("--- User's question ---", payload.prompt, '');
    if (payload.sql) lines.push('--- Generated SQL ---', payload.sql, '');
    if (payload.content) lines.push(`--- ${categoryLabel} content (as shown to you) ---`, payload.content, '');
    return lines.join('\n');
  }

  // Seeds #reportIssuePreviewEditable for the 'wrong_sql' category - a
  // plain, deliberately simpler layout than renderReportPreviewText()'s
  // (no "Category:"/"LLM:" header lines, since those are metadata the
  // server derives/sends separately, not part of the editable message
  // itself) since the user is meant to treat this as a starting draft
  // they'll likely trim down, not a fixed record they're just appending
  // to. Called once, at modal-open time - never regenerated afterward, so
  // edits the user makes are never clobbered by a later render.
  function renderWrongSqlPreviewSeed(ctx) {
    const lines = [];
    const prompt = getReportPromptText();
    if (prompt) lines.push('NL prompt:', prompt, '');
    lines.push('SQL:', ctx.sql || '(no SQL entered)');
    return lines.join('\n');
  }

  function closeReportIssueModal() {
    if (reportIssueModal) reportIssueModal.classList.add('hidden');
  }

  // `context` lets a caller open this modal for something OTHER than
  // whatever's currently on screen in the results area - just the Help
  // dialog's "Send Feedback" button today, passing a synthetic
  // {category: 'feedback'} that was never assigned to currentReportContext
  // (see activeReportContext's own docstring for why). Omitted (or falsy),
  // this falls back to currentReportContext exactly as before - the
  // resultsBody-delegated error/wrong_result trigger below relies on that
  // default.
  function openReportIssueModal(context) {
    const ctx = context || currentReportContext;
    if (!reportIssueModal || !ctx) return;
    // Assigned before buildReportPayload() runs, and read by
    // sendReportIssue() later - see activeReportContext's own docstring on
    // why this indirection exists instead of both reading ctx/
    // currentReportContext directly.
    activeReportContext = ctx;
    // Built with an empty `details` value purely for the preview - the
    // user's actual textarea content is re-read fresh at Send time (see
    // sendReportIssue()) rather than captured here, so edits made after
    // opening the modal are never lost.
    const payload = buildReportPayload('', ctx);
    if (!payload) return;
    const config = reportCategoryConfig(payload.category);

    if (reportIssueModalTitle) reportIssueModalTitle.textContent = config.modalTitle;
    if (reportIssueIntro) reportIssueIntro.textContent = config.intro;
    if (reportIssuePreviewSection) reportIssuePreviewSection.classList.toggle('hidden', !config.showPreview);
    if (reportIssuePreviewLabel) reportIssuePreviewLabel.textContent = config.previewSectionLabel || 'What will be sent';
    // previewEditable (currently just 'wrong_sql') swaps in the plain
    // <textarea> counterpart instead of the read-only <pre> - see
    // #reportIssuePreviewEditable's own comment above and
    // renderWrongSqlPreviewSeed(), which - unlike renderReportPreviewText()
    // below - is only ever called here, once, so later edits are never
    // overwritten by a re-render.
    if (config.previewEditable) {
      if (reportIssuePreview) reportIssuePreview.classList.add('hidden');
      if (reportIssuePreviewEditable) {
        reportIssuePreviewEditable.classList.remove('hidden');
        reportIssuePreviewEditable.value = renderWrongSqlPreviewSeed(ctx);
      }
    } else {
      if (reportIssuePreviewEditable) reportIssuePreviewEditable.classList.add('hidden');
      if (reportIssuePreview) {
        reportIssuePreview.classList.remove('hidden');
        reportIssuePreview.textContent = config.showPreview ? renderReportPreviewText(payload) : '';
      }
    }
    if (reportIssueDetailsLabel) reportIssueDetailsLabel.textContent = config.detailsLabel;
    if (reportIssueDetails) {
      reportIssueDetails.value = '';
      reportIssueDetails.placeholder = config.detailsPlaceholder;
    }
    if (reportIssueStatus) {
      reportIssueStatus.style.display = 'none';
      reportIssueStatus.textContent = '';
    }
    if (reportIssueSendBtn) {
      reportIssueSendBtn.disabled = false;
      reportIssueSendBtn.textContent = config.sendLabel;
    }

    reportIssueModal.classList.remove('hidden');
    bringModalToFront(reportIssueModal);
  }

  async function sendReportIssue() {
    const config = reportCategoryConfig(activeReportContext && activeReportContext.category);
    const detailsValue = reportIssueDetails ? reportIssueDetails.value.trim() : '';
    // Only 'feedback' sets this (see REPORT_CATEGORY_CONFIG.feedback) -
    // error/wrong_result already have the preview content itself as the
    // substantive part of the email, so an empty textarea there is fine.
    if (config.detailsRequired && !detailsValue) {
      if (reportIssueStatus) {
        reportIssueStatus.textContent = 'Please enter your feedback before sending.';
        reportIssueStatus.style.display = 'block';
      }
      return;
    }
    const payload = buildReportPayload(detailsValue, activeReportContext);
    if (!payload) return;
    // previewEditable's whole message IS the (editable) preview content
    // (see REPORT_CATEGORY_CONFIG.wrong_sql) - an empty send there, with no
    // comment either, would produce a blank report with nothing for a
    // reviewer to act on, the same concern detailsRequired guards against
    // for 'feedback' above.
    if (config.previewEditable && !payload.content && !detailsValue) {
      if (reportIssueStatus) {
        reportIssueStatus.textContent = 'Please include the SQL you want to report, or add a comment below.';
        reportIssueStatus.style.display = 'block';
      }
      return;
    }

    if (reportIssueSendBtn) {
      reportIssueSendBtn.disabled = true;
      reportIssueSendBtn.textContent = config.sendingLabel;
    }
    if (reportIssueStatus) reportIssueStatus.style.display = 'none';

    try {
      const response = await fetch('/api/report-issue', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        trackEvent('report_submitted', { category: payload.category });
        closeReportIssueModal();
      } else if (reportIssueStatus) {
        reportIssueStatus.textContent = data.error || 'Failed to send report.';
        reportIssueStatus.style.display = 'block';
      }
    } catch (err) {
      if (reportIssueStatus) {
        reportIssueStatus.textContent = err.message || 'Failed to reach the backend server.';
        reportIssueStatus.style.display = 'block';
      }
    } finally {
      if (reportIssueSendBtn) {
        reportIssueSendBtn.disabled = false;
        reportIssueSendBtn.textContent = config.sendLabel;
      }
    }
  }

  // The button itself is rendered fresh into #resultsBody on every render
  // pass (see reportButtonHtml() above) rather than being a persistent
  // element with its own listener, so a single delegated listener on the
  // never-replaced #resultsBody container (only its children are ever
  // replaced) is what makes every current/future instance of it clickable.
  // No context passed - this is the "report something about the currently-
  // displayed result" trigger, so it falls back to currentReportContext.
  if (resultsBody) {
    resultsBody.addEventListener('click', (e) => {
      const trigger = e.target.closest('[data-report-issue-trigger]');
      if (trigger) openReportIssueModal();
    });
  }
  // The header's "Send Feedback" button - a persistent element (unlike the
  // inline Report buttons above), so it gets its own listener rather than
  // delegation. Its narrow-screen more-menu twin (#moreMenuFeedbackBtn, see
  // the MORE MENU section) just forwards a click here instead of opening
  // the modal itself. Hidden/shown by fetchBackendConfig() based on
  // ISSUE_REPORTING_ENABLED, same gate the inline Report buttons already
  // use (see reportButtonHtml()) - sending feedback needs the same
  // server-side SMTP config they do.
  if (sendFeedbackBtn) {
    sendFeedbackBtn.addEventListener('click', () => openReportIssueModal({ category: 'feedback' }));
  }
  // The SQL box's "report wrong SQL" thumbs-down button - also a persistent
  // element, gated by the same ISSUE_REPORTING_ENABLED toggle above. Reads
  // the SQL box and the active connection badge fresh at click time (not
  // captured anywhere ahead of time), since the user may have been editing
  // either right up until they click this.
  if (reportSqlBtn) {
    reportSqlBtn.addEventListener('click', () => openReportIssueModal({
      category: 'wrong_sql',
      sql: getSqlQuery(),
      databaseName: connDbName ? connDbName.textContent : '',
    }));
  }
  if (reportIssueModalCloseBtn) {
    reportIssueModalCloseBtn.addEventListener('click', closeReportIssueModal);
  }
  if (reportIssueCancelBtn) {
    reportIssueCancelBtn.addEventListener('click', closeReportIssueModal);
  }
  if (reportIssueSendBtn) {
    reportIssueSendBtn.addEventListener('click', sendReportIssue);
  }

  async function loadHistoryData() {
    if (!historyTableHeader || !historyTableBody) return;
  
    historyTableHeader.innerHTML = '';
    historyTableBody.innerHTML = '<tr><td class="text-center text-muted py-8">Loading history...</td></tr>';

    document.getElementById('historyCountSubtitle')?.remove();
  
    try {
      const response = await fetch('/api/history', { headers: getApiHeaders(), credentials: 'same-origin' });
      const data = await response.json();
  
      if (response.ok && data.success) {
        const totalCount = (data.total_count !== undefined && data.total_count !== null && data.total_count > 0) 
          ? data.total_count 
          : (data.history ? data.history.length : 0);

        const purgeTitleEl = document.querySelector('.btn-purge-title');
        if (purgeTitleEl) {
          purgeTitleEl.textContent = `(${totalCount})`;
        }

        if (data.history && data.history.length > 0) {
          const rows = data.history;
          const columns = Object.keys(rows[0]);
  
          columns.forEach(col => {
            const th = document.createElement('th');
            th.textContent = col;
            historyTableHeader.appendChild(th);
          });
  
          historyTableBody.innerHTML = '';
          rows.forEach(row => {
            const tr = document.createElement('tr');
            columns.forEach(col => {
              const td = document.createElement('td');
              const val = row[col];
              td.textContent = val !== null && val !== undefined ? val : 'NULL';
              td.classList.add('cell-multiline');
              if (val === null || val === undefined) td.classList.add('text-null');
              tr.appendChild(td);
            });
            historyTableBody.appendChild(tr);
          });
        } else {
          historyTableBody.innerHTML = '<tr><td class="text-center text-muted py-8">No history records found.</td></tr>';
        }
  
        renderStatisticsCharts(data.stats || []);
      } else {
        const errMsg = response.status === 401 
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to authenticate." 
          : (data.error || `Server returned status ${response.status}`);
        historyTableBody.innerHTML = `
          <tr>
            <td class="error-cell">
              <div class="error-container">
                <span class="error-icon">⚠️</span>
                <div class="error-details">
                  <strong>Error Loading History</strong>
                  <p>${errMsg}</p>
                </div>
              </div>
            </td>
          </tr>`;
      }
    } catch (err) {
      console.error("Failed to fetch history:", err);
      historyTableBody.innerHTML = `
        <tr>
          <td class="error-cell">
            <div class="error-container">
              <span class="error-icon">⚠️</span>
              <div class="error-details">
                <strong>Error Loading History</strong>
                <p>${err.message || "Failed to reach the backend service."}</p>
              </div>
            </div>
          </td>
        </tr>`;
    }
  }

  if (historyBtn && historyModal) {
    historyBtn.addEventListener('click', () => {
      trackEvent('history_viewed', {});
      updateHistoryTurnsSubtitle();
      const purgeTitleEl = document.querySelector('.btn-purge-title');
      if (purgeTitleEl) {
        purgeTitleEl.textContent = '(...)';
      }
      historyModal.classList.remove('hidden');
      bringModalToFront(historyModal);
      loadHistoryData();
    });
  }

  if (historyModalCloseBtn && historyModal) {
    historyModalCloseBtn.addEventListener('click', () => {
      historyModal.classList.add('hidden');
    });
  }

  if (configSaveBtn) {
    configSaveBtn.addEventListener('click', async () => {
      await triggerConfigSave({ closeModal: true });
    });
  }

  // ===========================================================================
  // 8. RESULTS RENDERING HELPERS
  // ===========================================================================

  // Report Error / Report Wrong Result (see report_routes.py's module
  // docstring). `context` is either null (nothing reportable is currently
  // showing) or { category: 'error'|'wrong_result', databaseName, sql,
  // content } - `content` is the raw error text for an 'error' report, or a
  // plain-text rendering of whatever the app/model actually showed the user
  // for a 'wrong_result' one (see summarizeTabularResultForReport() below
  // for the table case). Every render path that shows something reportable
  // calls this at the point it knows that context, immediately before
  // rendering that SAME context's own reportButtonHtml() into the tab (see
  // renderTableResult()/renderNoSqlResponse() below and executeSql()'s own
  // bare-error fallback) - callers that show something NOT in scope for
  // this feature (a translation/network/history error - see
  // report_routes.py's module docstring on why those are excluded) simply
  // never call either one at all.
  //
  // Only one reportable tab's content is ever visible in #resultsBody at a
  // time (switching tabs re-renders via this same renderTableResult()), so
  // "whichever context was set most recently" is always the context that
  // matches whatever report-issue-inline-btn is currently in the DOM for
  // openReportIssueModal() (wired via a delegated click listener - see
  // below) to read.
  function setReportContext(context) {
    currentReportContext = context;
  }

  // Renders the small inline "Report Error" button as an HTML string,
  // meant to be inserted directly into the tab content that's being
  // reported on - NOT as a persistent control living somewhere outside the
  // tab. Returns '' (nothing rendered at all) both when the feature isn't
  // configured server-side (so there's no dead/disabled button to explain
  // to a user on a deployment that hasn't set this up) and for any
  // category other than 'error' - by explicit request, a "Report Wrong
  // Result" button is no longer ever shown: only a tab that's actually
  // showing an execution error gets a Report button at all. `setReportContext()`
  // callers still pass 'wrong_result' for a successful/NO-SQL tab (keeping
  // report_routes.py's server-side category and email-review-modal support
  // for it intact, in case a future UI wants it back), but with no button
  // ever rendered for that category, openReportIssueModal() is simply never
  // reached for it any more.
  //
  // A plain data attribute (not an inline onclick, and not a listener
  // re-attached after every render) is what makes clicking it work - see
  // the delegated 'click' listener on #resultsBody below, added once at
  // setup time rather than per-render, since this button is recreated
  // fresh on every renderTableResult() call.
  function reportButtonHtml(category) {
    if (!ISSUE_REPORTING_ENABLED || category !== 'error') return '';
    // Red (--danger), matching the "Execution Error" title this button
    // always sits next to (see .error-title-row/.report-issue-inline-btn--error
    // in style.css) - there's no other variant to distinguish from any more.
    return `<button type="button" class="report-issue-inline-btn report-issue-inline-btn--error" data-report-issue-trigger>🚩 Report Error</button>`;
  }

  // Same button, wrapped in its own full-width <tr><td> - for the branches
  // that need to drop it directly into #resultsBody (a <tbody>) rather than
  // into a <td> that's already open. A raw <button> string inserted straight
  // into a <tbody> would get foster-parented out of the table entirely (per
  // the HTML parsing spec's table-insertion-mode rules), so every tbody-level
  // insertion goes through this instead. `colspan` should match however many
  // columns the result actually has (1 when there's no column header at all)
  // so the row spans the full table width instead of squeezing into the
  // first column.
  //
  // In practice this is always called with category 'wrong_result' (the
  // tabular-result branches below) - reportButtonHtml() now always returns
  // '' for that category, so this resolves to '' too and no row is ever
  // actually inserted. Left in place (rather than deleted at each call
  // site) so those branches stay structurally ready if "Report Wrong
  // Result" is ever reinstated.
  function reportButtonRowHtml(category, colspan) {
    const html = reportButtonHtml(category);
    if (!html) return '';
    return `<tr class="report-issue-row"><td colspan="${colspan || 1}">${html}</td></tr>`;
  }

  // Plain-text rendering of a successful tabular result, for the email
  // preview/body of a 'wrong_result' report on that tab - capped at 25 rows
  // so a large result set doesn't balloon the report (report_routes.py
  // truncates every field server-side too, but there's no reason to make
  // the client build/POST a huge payload in the first place when the point
  // is just to show a reviewer what looked wrong).
  function summarizeTabularResultForReport(result) {
    if (!result || !result.columns || !result.columns.length) return '';
    const cols = result.columns;
    const allRows = Array.isArray(result.rows) ? result.rows : [];
    const shown = allRows.slice(0, 25);
    const lines = [cols.join(' | ')];
    shown.forEach((row) => {
      lines.push(cols.map((c) => {
        const val = row[c];
        return val === null || val === undefined ? 'NULL' : String(val);
      }).join(' | '));
    });
    if (allRows.length > shown.length) {
      lines.push(`... (${allRows.length - shown.length} more rows not shown)`);
    }
    return lines.join('\n');
  }

  function renderTableResult(result) {
    if (!resultsHeader || !resultsBody) return;
    resultsHeader.innerHTML = '';
    resultsBody.innerHTML = '';

    // Reset first, unconditionally - every branch below that actually shows
    // something reportable calls setReportContext() again with its own
    // context before returning, alongside inserting its own
    // reportButtonHtml()/reportButtonRowHtml() markup; a branch that ISN'T
    // in scope for this feature (isPending's "still fetching" placeholder)
    // simply never does either, so this null is what sticks and no button
    // is ever rendered for it.
    setReportContext(null);

    // "All databases" mode's live-streaming placeholder tab (see
    // startAllModeStreaming()) - stands in for one selected connection
    // from the moment triage picks it until either its own generation
    // call settles (handlePhaseBConnectionDone() swaps this out for a
    // real Note/error tab) or, for a real-SQL outcome, its /api/execute
    // call resolves (executeOneAllModeConnection()). Checked first since
    // it never carries isText/isError.
    if (result && result.isPending) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'response-cell';

      if (result.database && result.database.name) {
        const dbP = document.createElement('p');
        dbP.className = 'text-muted';
        dbP.textContent = `Database: ${result.database.name}`;
        td.appendChild(dbP);
      }

      const p = document.createElement('p');
      p.className = 'response-text animate-pulse';
      p.textContent = 'Fetching results…';
      td.appendChild(p);

      tr.appendChild(td);
      resultsBody.appendChild(tr);
      return;
    }

    // All-databases mode's own synthetic text tab entries (see
    // renderAllModeCombinedResults() below) - a "Summary" tab built from
    // the triage routing message, or a per-database "Note" tab built from
    // a '*** NO SQL ***' reply Phase B returned instead of real SQL.
    // Reuses the exact same `.response-cell`/`.response-text` markup
    // renderNoSqlResponse() already shows for a single-connection NO-SQL
    // reply, plus the same "Database: <name>" note line the isError
    // branch below shows when a result is tagged with a connection.
    // Checked before isError since these entries never carry both flags.
    if (result && result.isText) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'response-cell';

      if (result.database && result.database.name) {
        const dbP = document.createElement('p');
        dbP.className = 'text-muted';
        dbP.textContent = `Database: ${result.database.name}`;
        td.appendChild(dbP);
      }

      const p = document.createElement('p');
      p.className = 'response-text';
      // The "Summary" tab carries the leading-label convention (see
      // renderMarkdownLiteSummaryTab()'s own docstring) - a "Note" tab
      // (Phase B's own per-database '*** NO SQL ***' reply) never does,
      // so it's rendered plain like any other free-text reply.
      p.innerHTML = result.tabLabel === 'Summary'
        ? renderMarkdownLiteSummaryTab(result.text || '')
        : renderMarkdownLite(result.text || '');
      td.appendChild(p);
      td.insertAdjacentHTML('beforeend', reportButtonHtml('wrong_result'));

      tr.appendChild(td);
      resultsBody.appendChild(tr);
      setReportContext({
        category: 'wrong_result',
        databaseName: result.database && result.database.name,
        sql: result.query || result.sql || result.statement || '',
        content: stripNoSqlPrefix(result.text || ''),
      });
      return;
    }

    // A synthetic "this statement failed" tab entry (see
    // renderResultsWithFailedStatement() below) - same error markup
    // executeSql() has always shown for a single-statement failure, just
    // scoped to one tab's content instead of replacing the whole results
    // area, so it sits alongside the other (successful) statements' tabs.
    if (result && result.isError) {
      // Multi-database question-answering: a failure tagged with which
      // connection it came from (see renderResultsWithDatabaseFailures())
      // gets that named called out explicitly, since with more than one
      // connection involved "Execution Error" alone no longer says which
      // one - absent entirely for a single-connection failure, which never
      // carries this field.
      const dbNote = result.database && result.database.name
        ? `<p class="text-muted">Database: ${result.database.name}</p>` : '';
      resultsBody.innerHTML = `
        <tr>
          <td class="error-cell">
            <div class="error-container">
              <span class="error-icon">⚠️</span>
              <div class="error-details">
                <div class="error-title-row">
                  <strong>Execution Error</strong>
                  ${reportButtonHtml('error')}
                </div>
                ${dbNote}
                <p>${result.error || 'An error occurred during SQL execution.'}</p>
              </div>
            </div>
          </td>
        </tr>`;
      setReportContext({
        category: 'error',
        databaseName: result.database && result.database.name,
        sql: result.statement || result.query || result.sql || '',
        content: result.error || '',
      });
      trackEvent('error_shown', {
        category: 'execution',
        database_name: (result.database && result.database.name) || '',
        // result.database (see its construction sites - object literals of
        // {kind, id, name} only) never carries its own dialect, so this
        // falls back to the currently-active connection's type. In "all
        // databases" mode the erroring connection isn't always the active
        // one - not perfectly precise there, but there's no per-connection
        // type available from the server to do better.
        database_type: getActiveDatabaseType(),
        message: truncateForAnalytics(result.error || ''),
      });
      return;
    }

    // Server-side output a statement produced outside its own result set
    // (currently: Oracle's DBMS_OUTPUT.PUT_LINE, captured by backends/
    // oracle.py's execute() - see backends/base.py's execute() docstring
    // for the "notices" key's contract). Rendered as its own row, reusing
    // the same .response-cell/.response-text markup a NO-SQL reply's text
    // gets (isText branch above) - shown ABOVE any real dataset the same
    // statement also returned (rare, but not impossible), and standing in
    // for the generic "No dataset returned" message below when there's no
    // dataset at all, since the notices ARE the meaningful feedback here.
    const hasNotices = !!(result && result.notices && result.notices.length > 0);
    if (hasNotices) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'response-cell';
      if (result.columns && result.columns.length > 1) td.colSpan = result.columns.length;
      const p = document.createElement('p');
      p.className = 'response-text';
      p.textContent = result.notices.join('\n');
      td.appendChild(p);
      tr.appendChild(td);
      resultsBody.appendChild(tr);
    }

    // Whatever's actually reportable about a successful result - notices
    // (if any) plus either the tabular preview or the "no dataset"/"0 rows"
    // message, whichever this call ends up showing below. Built once here
    // (rather than duplicated at each of the three exit points) since a
    // successful result is always reportable as 'wrong_result', unlike the
    // isText/isError branches above which return before reaching this
    // point at all.
    const reportSql = (result && (result.query || result.sql || result.statement)) || getSqlQuery();
    const reportDatabaseName = result && result.database && result.database.name;

    if (!result || (!result.columns && !result.rows)) {
      if (!hasNotices) {
        resultsBody.innerHTML = `<tr><td class="text-center text-muted py-8">Statement executed successfully. No dataset returned.</td></tr>`;
      }
      if (result) {
        setReportContext({
          category: 'wrong_result',
          databaseName: reportDatabaseName,
          sql: reportSql,
          content: hasNotices ? result.notices.join('\n') : 'Statement executed successfully. No dataset returned.',
        });
        resultsBody.insertAdjacentHTML('beforeend', reportButtonRowHtml('wrong_result', result.columns ? result.columns.length : 1));
      }
      return;
    }

    if (result.columns && result.columns.length > 0) {
      result.columns.forEach(col => {
        const th = document.createElement('th');
        th.textContent = col;
        resultsHeader.appendChild(th);
      });
    }

    if (result.rows && result.rows.length > 0) {
      result.rows.forEach(row => {
        const tr = document.createElement('tr');
        result.columns.forEach(col => {
          const td = document.createElement('td');
          const val = row[col];
          td.textContent = val !== null && val !== undefined ? val : 'NULL';

          td.classList.add('cell-multiline');
          if (val === null || val === undefined) td.classList.add('text-null');
          tr.appendChild(td);
        });
        resultsBody.appendChild(tr);
      });
      setReportContext({
        category: 'wrong_result',
        databaseName: reportDatabaseName,
        sql: reportSql,
        content: (hasNotices ? result.notices.join('\n') + '\n\n' : '') + summarizeTabularResultForReport(result),
      });
      resultsBody.insertAdjacentHTML('beforeend', reportButtonRowHtml('wrong_result', result.columns.length));
    } else {
      resultsBody.innerHTML = `<tr><td colspan="${result.columns ? result.columns.length : 1}" class="text-center text-muted py-8">0 rows returned.</td></tr>`;
      setReportContext({
        category: 'wrong_result',
        databaseName: reportDatabaseName,
        sql: reportSql,
        content: (hasNotices ? result.notices.join('\n') + '\n\n' : '') + '0 rows returned.',
      });
      resultsBody.insertAdjacentHTML('beforeend', reportButtonRowHtml('wrong_result', result.columns ? result.columns.length : 1));
    }
  }

  function buildResultsTabsNav() {
    if (!resultsTabsNav) return;
    resultsTabsNav.innerHTML = '';

    if (!currentResultsList || currentResultsList.length <= 1) {
      resultsTabsNav.classList.add('hidden');
      return;
    }

    resultsTabsNav.classList.remove('hidden');
    currentResultsList.forEach((res, idx) => {
      const btn = document.createElement('button');
      const isError = !!res.isError;
      const isText = !!res.isText;
      const isPending = !!res.isPending;
      btn.className = `result-tab-btn ${idx === activeResultIndex ? 'active' : ''} ${isError ? 'result-tab-btn--error' : ''} ${isPending ? 'result-tab-btn--pending' : ''}`.trim();

      const sqlText = res.query || res.sql || res.statement || '';
      // Multi-database question-answering: a result tagged with which
      // connection it came from (see execute_routes.py's module docstring)
      // gets that connection's name prefixed onto its tab (its own line -
      // see .result-tab-btn's CSS - above the "Query N (rows)"/"Note"/etc.
      // line below it), so a script that spanned more than one database
      // still reads clearly tab-by-tab - absent entirely for a
      // single-connection script, which never carries this field at all.
      const dbLabel = res.database && res.database.name ? `${res.database.name}\n` : '';
      if (sqlText) {
        btn.setAttribute('title', dbLabel ? `${res.database.name}\n${sqlText}` : sqlText);
      }

      if (isPending) {
        // "All databases" mode's live-streaming placeholder tab (see
        // startAllModeStreaming()/renderTableResult()'s own isPending
        // branch) - same two-line name-then-status convention as every
        // other per-database tab, with a short status word instead of a
        // row count (there's nothing to count yet).
        btn.textContent = `${dbLabel}${res.tabLabel || 'Fetching…'}`;
      } else if (isText) {
        // All-databases mode's own synthetic text tabs (see
        // renderAllModeCombinedResults()) - a leading "Summary" tab (no
        // `.database`, so no name line) or a per-database "Note" tab (same
        // two-line convention as every other tab here).
        btn.textContent = `${dbLabel}${res.tabLabel || 'Note'}`;
      } else if (isError) {
        // Colored differently (via the result-tab-btn--error class) so a
        // failed statement in an otherwise-successful multi-statement
        // script draws the eye immediately, instead of looking like just
        // another results tab.
        btn.textContent = `${dbLabel}Query ${idx + 1} (Error)`;
      } else {
        const count = res.rowCount !== undefined ? res.rowCount : (res.rows ? res.rows.length : 0);
        const rowLabel = count === 1 ? '1 row' : `${count} rows`;
        btn.textContent = `${dbLabel}Query ${idx + 1} (${rowLabel})`;
      }

      btn.addEventListener('click', () => {
        activeResultIndex = idx;
        buildResultsTabsNav();
        renderTableResult(res);
      });
      resultsTabsNav.appendChild(btn);
    });
  }

  function renderMultiTurnResults(results) {
    currentResultsList = results || [];
    activeResultIndex = 0;

    if (!currentResultsList.length) {
      if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
      renderTableResult(null);
      return;
    }

    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex]);
  }

  // A multi-statement script (semicolon-separated) that fails partway
  // through gets the SAME tabbed treatment as one that fully succeeds
  // (renderMultiTurnResults above), rather than one opaque error that
  // throws away which statements ran and what they returned. `data` is
  // /api/execute's SqlExecutionError-shaped failure response (see
  // execute_routes.py's module docstring): `data.results` holds every
  // statement that succeeded BEFORE the failure, and `data.failedStatement`/
  // `data.error` describe the one that didn't - there's no tab for
  // whatever came after it, since the script correctly never got there.
  function renderResultsWithFailedStatement(data) {
    const succeeded = Array.isArray(data.results) ? data.results : [];
    const failedEntry = {
      statement: data.failedStatement || '',
      isError: true,
      error: data.error || 'An error occurred during SQL execution.',
    };
    currentResultsList = [...succeeded, failedEntry];
    // Jump straight to the failed statement's tab rather than defaulting
    // to the first one (renderMultiTurnResults's success-case behavior) -
    // it's what the user needs to see first, not something they should
    // have to go looking for.
    activeResultIndex = currentResultsList.length - 1;

    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex]);
  }

  // Multi-database question-answering's own partial-failure shape (see
  // execute_routes.py's module docstring): `data.results` holds every
  // statement that succeeded ACROSS EVERY connection the script touched
  // (each already tagged with a `.database` field - see
  // buildResultsTabsNav()'s dbLabel), and `data.failures` holds one entry
  // per connection that failed at all (the OTHER, independent connections
  // keep running and their results are still in `data.results` - see this
  // module's docstring on that policy). One synthetic error tab is
  // rendered per failure, appended after every succeeded tab (ordering
  // note: this mirrors execute_routes.py's own "grouped by connection,
  // not perfectly interleaved with successes" ordering - see
  // _execute_multi_database's docstring) - distinct from
  // renderResultsWithFailedStatement above, which is the single-
  // connection SqlExecutionError shape (exactly one failure, no
  // `.database` tagging at all) and is left completely unchanged.
  function renderResultsWithDatabaseFailures(data) {
    const succeeded = Array.isArray(data.results) ? data.results : [];
    const failureEntries = (Array.isArray(data.failures) ? data.failures : []).map((f) => ({
      statement: f.failedStatement || '',
      isError: true,
      error: f.error || 'An error occurred during SQL execution.',
      database: f.database,
    }));
    currentResultsList = [...succeeded, ...failureEntries];
    // Jump to the FIRST failure tab, same "show the user what needs
    // attention" reasoning as renderResultsWithFailedStatement's single-
    // failure jump - there just may be more than one here.
    const firstFailureIndex = currentResultsList.findIndex((r) => r.isError);
    activeResultIndex = firstFailureIndex >= 0 ? firstFailureIndex : 0;

    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex]);
  }

  // Shared by renderNoSqlResponse() below and Phase C's summary text (see
  // appendPhaseCSummaryToSummaryTab) - the "*** NO SQL ***" marker is an
  // internal convention (also used server-side for translations-table
  // logging, see translate_routes.py's record_all_databases_triage call
  // sites) that a user should never actually see verbatim.
  function stripNoSqlPrefix(rawText) {
    return (rawText || '').replace(/^\*\*\*\s*NO\s*SQL\s*\*\*\*\s*/i, '').trim();
  }

  function escapeHtml(text) {
    return (text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Minimal, dependency-free Markdown-lite renderer for LLM free-text
  // replies (single-connection NO-SQL answers, and the all-mode Summary/
  // Note tabs - see renderNoSqlResponse() and the `isText` branch in
  // renderTableResult()) - these commonly come back with **bold**,
  // *italic*/_italic_ emphasis, and occasional `inline code`, which used
  // to show up as literal asterisks/underscores/backticks now that this
  // was rendered via .textContent. Escapes HTML first (this is LLM
  // output, not trusted markup) then applies a deliberately small set of
  // inline substitutions - not a full Markdown parser (no lists, links,
  // or headings), just the emphasis these replies actually use. Newlines
  // are left untouched - .response-text's `white-space: pre-wrap` already
  // renders them as line breaks, same as before this function existed.
  // Does NOT know anything about the "All databases" mode section-label
  // convention (see renderMarkdownLiteSummaryTab() below for that) - this
  // is the plain version, safe to use on any free-text reply, including
  // ones that were never asked to carry a label at all.
  function renderMarkdownLite(rawText) {
    return applyInlineMarkdown(escapeHtml(rawText));
  }

  function applyInlineMarkdown(escapedHtml) {
    let html = escapedHtml;
    // Code spans first, so a literal asterisk/underscore inside one isn't
    // then misread as emphasis syntax by the patterns below.
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    // Bold before italic - by the time the italic patterns run, every
    // real **bold**/__bold__ pair has already been consumed, so a
    // leftover single */_ can only be genuine italic syntax.
    html = html.replace(/\*\*([^\n*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^\n_]+)__/g, '<strong>$1</strong>');
    html = html.replace(/\*([^\n*]+)\*/g, '<em>$1</em>');
    // Underscore italics require a non-word char (or start of string) on
    // either side, so a snake_case_identifier in the reply doesn't get
    // partially italicized.
    html = html.replace(/(^|[^\w\\])_([^\n_]+)_(?!\w)/g, '$1<em>$2</em>');
    return html;
  }

  // Strips a label's own **bold**/__bold__ wrapping, if the model added
  // one despite _TRIAGE_SYSTEM_INSTRUCTION/_SUMMARY_SYSTEM_INSTRUCTION
  // asking for a plain-text label - mirrors server/connection_router.py's
  // split_leading_label_line(), which does the same unwrapping for the
  // exact same reason.
  function unwrapLabelEmphasis(label) {
    const trimmed = (label || '').trim();
    const m = /^(\*\*|__)([\s\S]*)\1$/.exec(trimmed);
    return (m ? m[2] : trimmed).trim();
  }

  // Prepended by EVERY caller that hands a block of text to
  // renderMarkdownLiteSummaryTab() below and wants its own leading line
  // treated as a label - triage's own routing message (see
  // renderAllModeCombinedResults()/startAllModeStreaming()'s Summary tab
  // construction), Phase C's own summary text (see
  // appendPhaseCSummaryToSummaryTab), and the all-mode "answer" outcome's
  // own text (see renderNoSqlResponse()). Deliberately NOT inferred by
  // POSITION (e.g. "the first line of the whole string") - an earlier
  // version of this tried that, and it misfired on a Phase C summary
  // whose own first per-database paragraph (also just one line followed
  // by a blank line, per _SUMMARY_SYSTEM_INSTRUCTION's own "**Name:** ..."
  // shape) looked exactly as label-shaped as a genuine label line, with
  // no way to tell them apart from shape alone - and, separately, a
  // routing message with NO label of its own still looked label-shaped
  // once Phase C's text was joined underneath it, since the join itself
  // always inserts a blank line after it. Marking every real block
  // boundary explicitly, right where the code already knows one exists,
  // sidesteps both problems entirely. Built around a NUL character,
  // which escapeHtml() below passes through unaltered (it only touches
  // &/</>) and which no real LLM reply can ever contain, so it can never
  // collide with genuine text.
  const SUMMARY_TAB_BLOCK_MARKER = '\u0000SUMMARY_BLOCK\u0000';

  // Renders the "All databases" mode Summary tab's text (see the `isText`
  // branch in renderTableResult(), and renderNoSqlResponse() for the
  // all-mode "answer" outcome, which shares this same leading-label
  // convention) - a superset of renderMarkdownLite() that ALSO bolds
  // +underlines the leading line of every block marked with
  // SUMMARY_TAB_BLOCK_MARKER (see its own docstring for why marking is
  // done explicitly by each caller rather than inferred by position).
  // Never matches specific words - connection_router.py's
  // _TRIAGE_SYSTEM_INSTRUCTION / translate_routes.py's
  // _SUMMARY_SYSTEM_INSTRUCTION both ask the model for a "<label line>
  // \n\nbody" shape with the label written in the SAME LANGUAGE as the
  // user's own question, so there is no fixed English word left to match
  // against. Only call this for text where every block was actually
  // marked; a plain single-connection reply or a per-database "Note" tab
  // never marks anything and is rendered plain via renderMarkdownLite()
  // instead.
  function renderMarkdownLiteSummaryTab(rawText) {
    let html = escapeHtml(rawText || '');
    html = html.replace(
      new RegExp(SUMMARY_TAB_BLOCK_MARKER + '[ \\t]*([^\\n]+)\\n[ \\t]*\\n', 'g'),
      (_match, label) => `<strong><u>${unwrapLabelEmphasis(label)}</u></strong>\n\n`
    );
    return applyInlineMarkdown(html);
  }

  // `hasLabel` - true when `rawText` is known to carry the "All databases"
  // mode leading-label convention (see renderMarkdownLiteSummaryTab()
  // above): the all-mode triage "answer" outcome. False for a plain
  // single-connection NO-SQL reply, which was never asked to include one.
  function renderNoSqlResponse(rawText, { hasLabel = false } = {}) {
    const cleanText = stripNoSqlPrefix(rawText) || rawText || '';

    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) {
      resultsBody.innerHTML = '';
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'response-cell';

      const p = document.createElement('p');
      p.className = 'response-text';
      // renderMarkdownLiteSummaryTab() only bolds a block whose leading
      // line was explicitly marked (see SUMMARY_TAB_BLOCK_MARKER's own
      // docstring) - this is the one block in this text, so mark it here
      // at the call site, same as the Summary tab's construction sites do
      // for triage's own routing message and Phase C's summary text.
      p.innerHTML = hasLabel
        ? renderMarkdownLiteSummaryTab(SUMMARY_TAB_BLOCK_MARKER + cleanText)
        : renderMarkdownLite(cleanText);

      td.appendChild(p);
      td.insertAdjacentHTML('beforeend', reportButtonHtml('wrong_result'));
      tr.appendChild(td);
      resultsBody.appendChild(tr);
    }

    // Single-connection NO-SQL replies are always a direct model response
    // to the user - exactly the "wrong or misleading summarization... or
    // any other response given directly by the model" case this feature
    // targets (see report_routes.py's module docstring) - so this is
    // unconditionally reportable, unlike renderTableResult() above where
    // only some branches are.
    setReportContext({
      category: 'wrong_result',
      databaseName: '',
      sql: getSqlQuery(),
      content: cleanText,
    });
  }

  // "All databases" mode's own combined renderer - used for history
  // restoration (restoreLatestTurn()) and for the manual-Execute-button
  // batched flow (executeSql()'s router-route branch, when auto-execute
  // was off) - live streaming turns render progressively instead, see
  // startAllModeStreaming() and friends below. Merges a leading "Summary"
  // text tab (built from the triage
  // call's routing message, when there is one), one "Note" text tab per
  // database that came back with a '*** NO SQL ***' reply instead of real
  // SQL, the real per-database /api/execute results (if any SQL was
  // generated and executed at all), and error tabs for both execution
  // failures (`executeFailures` - today's existing partial-failure shape,
  // see renderResultsWithDatabaseFailures) and Phase B generation
  // failures (`notes.generationFailures` - reuses that exact same error-
  // tab shape, just a second source feeding the same list) into one
  // `currentResultsList`.
  function renderAllModeCombinedResults({ notes, executeResults, executeFailures }) {
    const routingMessage = notes && notes.routingMessage;
    const databaseNotes = (notes && notes.databaseNotes) || [];
    const generationFailures = (notes && notes.generationFailures) || [];

    const summaryTab = routingMessage
      ? [{ isText: true, tabLabel: 'Summary', text: SUMMARY_TAB_BLOCK_MARKER + routingMessage }]
      : [];
    const noteTabs = databaseNotes.map((n) => ({
      isText: true,
      tabLabel: 'Note',
      text: n.text || '',
      database: { kind: n.kind, id: n.id, name: n.name },
    }));
    const succeeded = Array.isArray(executeResults) ? executeResults : [];
    const executeFailureTabs = (Array.isArray(executeFailures) ? executeFailures : []).map((f) => ({
      statement: f.failedStatement || '',
      isError: true,
      error: f.error || 'An error occurred during SQL execution.',
      database: f.database,
    }));
    const generationFailureTabs = generationFailures.map((f) => ({
      isError: true,
      error: f.error || 'An error occurred generating SQL for this database.',
      database: { kind: f.kind, id: f.id, name: f.name },
    }));

    currentResultsList = [...summaryTab, ...noteTabs, ...succeeded, ...executeFailureTabs, ...generationFailureTabs];

    if (!currentResultsList.length) {
      if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
      renderTableResult(null);
      return;
    }

    // Same "show the user what needs attention first" reasoning as
    // renderResultsWithFailedStatement/renderResultsWithDatabaseFailures -
    // jump to the first failure if there is one, else the first entry
    // (typically the Summary tab, or the first real result when there's
    // no routing message to show).
    const firstFailureIndex = currentResultsList.findIndex((r) => r.isError);
    activeResultIndex = firstFailureIndex >= 0 ? firstFailureIndex : 0;

    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex]);
  }

  // "All databases" mode's Phase C (see translate_routes.py's
  // /api/summarize-results docstring for the full picture): once
  // /api/execute has actually run every database Phase B was routed to,
  // one more LLM call synthesizes the REAL, now-known results into a
  // single plain-language answer, which gets appended underneath the
  // Summary tab's existing routing message - triage's own message
  // necessarily can't say what the answer actually turned out to be,
  // since it's written before any real data is fetched.
  //
  // `notes` is the same shape allModeStreamState carries
  // (routingMessage/databaseNotes/generationFailures, plus the ORIGINAL
  // prompt - see startAllModeStreaming()); `executeResults`/
  // `executeFailures` are /api/execute's own results/failures for THIS
  // execution, exactly as passed into renderAllModeCombinedResults just
  // before this is called.
  function buildAllModeSummaryPayload(notes, executeResults, executeFailures) {
    const entries = [];
    (Array.isArray(executeResults) ? executeResults : []).forEach((r) => {
      const db = r.database || {};
      entries.push({
        kind: db.kind, id: db.id, name: db.name || 'Unknown database',
        columns: r.columns || [], rows: r.rows || [], rowCount: r.rowCount,
      });
    });
    ((notes && notes.databaseNotes) || []).forEach((n) => {
      entries.push({ kind: n.kind, id: n.id, name: n.name || 'Unknown database', note: n.text || '' });
    });
    ((notes && notes.generationFailures) || []).forEach((f) => {
      entries.push({
        kind: f.kind, id: f.id, name: f.name || 'Unknown database',
        error: f.error || 'Failed to generate SQL for this database.',
      });
    });
    (Array.isArray(executeFailures) ? executeFailures : []).forEach((f) => {
      const db = f.database || {};
      entries.push({
        kind: db.kind, id: db.id, name: db.name || 'Unknown database',
        error: f.error || 'Query execution failed for this database.',
      });
    });
    return entries;
  }

  // Patches Phase C's summary text into the Summary tab already built by
  // renderAllModeCombinedResults - re-renders in place only if that tab
  // happens to be the one currently showing, so it doesn't yank the user
  // back to a tab they've since navigated away from while this was in
  // flight.
  function appendPhaseCSummaryToSummaryTab(summaryText) {
    if (!currentResultsList || !currentResultsList.length) return;
    const summaryEntry = currentResultsList.find((r) => r.isText && r.tabLabel === 'Summary');
    if (!summaryEntry) return;
    // Phase C's own text always gets its own SUMMARY_TAB_BLOCK_MARKER
    // (see its docstring) so its leading label is bolded regardless of
    // whether there's already a leading block (triage's own message) to
    // join it underneath - renderMarkdownLiteSummaryTab() no longer
    // infers anything by position, only by this explicit marking.
    summaryEntry.text = summaryEntry.text
      ? `${summaryEntry.text}\n\n${SUMMARY_TAB_BLOCK_MARKER}${summaryText}`
      : `${SUMMARY_TAB_BLOCK_MARKER}${summaryText}`;
    if (currentResultsList[activeResultIndex] === summaryEntry) {
      renderTableResult(summaryEntry);
    }
  }

  // Same idea as appendPhaseCSummaryToSummaryTab just above, for the
  // failure case (see requestAllModeResultsSummary below) - Phase C's own
  // categorized, honest error message (translate_routes.py's
  // format_llm_error_for_user()) gets marked with SUMMARY_TAB_BLOCK_MARKER
  // the same way a real summary would, so its leading "the selected
  // model..." sentence is bolded and the "Actual error message received:"
  // detail underneath it reads as a distinct, secondary line - same
  // visual treatment as a real summary, just carrying an apology instead
  // of an answer, so the user sees WHY no summary appeared instead of the
  // Summary tab just silently staying as triage's routing message forever.
  function appendPhaseCErrorToSummaryTab(errorText) {
    if (!currentResultsList || !currentResultsList.length) return;
    const summaryEntry = currentResultsList.find((r) => r.isText && r.tabLabel === 'Summary');
    if (!summaryEntry) return;
    summaryEntry.text = summaryEntry.text
      ? `${summaryEntry.text}\n\n${SUMMARY_TAB_BLOCK_MARKER}${errorText}`
      : `${SUMMARY_TAB_BLOCK_MARKER}${errorText}`;
    if (currentResultsList[activeResultIndex] === summaryEntry) {
      renderTableResult(summaryEntry);
    }
  }

  // Fire-and-await (not fire-and-forget - see the two call sites in
  // executeSql() below, both already inside an async flow with buttons
  // disabled) request for Phase C's summary. Best-effort: skipped
  // entirely when there's no real data to summarize (every database just
  // noted or failed - nothing Phase C could add over what those tabs
  // already show). A failure from the endpoint itself is appended to the
  // Summary tab via appendPhaseCErrorToSummaryTab above (rather than
  // left silent, as it originally was) so the user can see why Phase C
  // didn't produce a summary - see /api/summarize-results' own docstring
  // for the two shapes `data.error` can take.
  async function requestAllModeResultsSummary(notes, executeResults, executeFailures) {
    if (!notes || !notes.prompt) return;
    const databaseResults = buildAllModeSummaryPayload(notes, executeResults, executeFailures);
    if (!databaseResults.some((e) => 'columns' in e)) return;

    try {
      const response = await fetch('/api/summarize-results', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({ prompt: notes.prompt, database_results: databaseResults }),
      });
      const data = await response.json();
      if (response.ok && data && data.success && data.summary) {
        // The server prefixes this the same "*** NO SQL ***" way any
        // other non-SQL LLM reply is (see translate_routes.py's
        // /api/summarize-results docstring) - an internal convention,
        // never meant to reach the user verbatim.
        appendPhaseCSummaryToSummaryTab(stripNoSqlPrefix(data.summary));
      } else if (data && data.error) {
        appendPhaseCErrorToSummaryTab(data.error);
      }
    } catch (err) {
      // See translatePrompt()'s identical guard - cancelInFlightQuery()
      // has already reset the UI synchronously by the time an aborted
      // fetch's promise rejects; patching the Summary tab (which may now
      // belong to a stale, already-cleared turn) on top of that would be
      // wrong.
      if (err && err.name === 'AbortError') {
        return;
      }
      console.error('Failed to summarize all-mode results:', err);
    }
  }

  // The Summary tab's CURRENT text - i.e. triage's routing message, plus
  // Phase C's synthesized answer once requestAllModeResultsSummary() has
  // patched it in (see appendPhaseCSummaryToSummaryTab). Used to pull the
  // FINAL, post-Phase-C text back out of the ephemeral currentResultsList
  // so it can be persisted onto the turn's chat-history entry - see
  // captureAllModeHistory() below.
  function getSummaryTabEntry() {
    if (!currentResultsList) return null;
    return currentResultsList.find((r) => r.isText && r.tabLabel === 'Summary') || null;
  }

  // Persists everything renderAllModeCombinedResults() needs to rebuild the
  // exact same tabbed view later - onto the turn's model entry, alongside
  // the `.text`/`.results` fields every other kind of turn already carries.
  // Without this, only the raw per-database rows survived past the current
  // render: the routing/Phase-C summary message, per-database "Note" tabs,
  // and any generation/execution failure tabs lived only in the ephemeral
  // currentResultsList - so stepping back and then forward through an
  // "all databases" turn (chatStore's undo()/redo()) silently dropped all
  // of that, leaving restoreLatestTurn() with nothing but a bare (and,
  // once summarizeResultForHistory() lost the `.database` tag too,
  // unlabeled) set of per-statement result tabs - or, when every database
  // just noted/failed instead of returning real SQL, nothing at all (see
  // the empty-`.text` guard below).
  function captureAllModeHistory(modelEntry, notes, executeFailures) {
    modelEntry.allMode = {
      routingMessage: (notes && notes.routingMessage) || null,
      databaseNotes: (notes && notes.databaseNotes) || [],
      generationFailures: (notes && notes.generationFailures) || [],
      executeFailures: executeFailures || [],
    };
    // Every database just noted/failed - translatePrompt()'s router_route
    // branch never sets modelEntry.text to anything but '' for this
    // outcome, which would both drop this turn from the LLM's history
    // entirely (build_gemini_history_contents/build_claude_history_messages/
    // build_openai_history_messages all skip any message with falsy text)
    // and make restoreLatestTurn()'s plain-text branch below treat it as
    // "no turn at all". Give it the same non-empty, "never shown verbatim"
    // text the single-connection "answer"/"failed" outcomes already use,
    // so it survives both.
    if (!modelEntry.text) {
      modelEntry.text = `*** NO SQL *** ${(notes && notes.routingMessage) || 'No database returned any data for this question.'}`;
    }
  }

  // "All databases" mode's PROGRESSIVE render path - the streaming
  // counterpart to renderAllModeCombinedResults() above (still used
  // unchanged for history restoration and, deliberately, for the manual-
  // Execute-button batched flow below - see executeSql()'s router-route
  // branch and this plan's "streaming-execute only when auto-execute is
  // on" decision). Kicks off the moment /api/translate's "phase_a_route"
  // NDJSON line arrives (see translate_routes.py's stream_translation()
  // docstring) - well before any single selected connection's own
  // generation call, let alone execution, has finished. Renders the
  // Summary tab immediately, plus one PENDING placeholder tab per
  // selected connection, and stashes everything handlePhaseBConnectionDone()/
  // executeOneAllModeConnection()/maybeFinalize() below need to keep
  // updating those tabs live as the rest of this turn's events arrive.
  function startAllModeStreaming(evt, promptText) {
    const connectionSelection = evt.connection_selection || [];
    allModeStreamState = {
      prompt: promptText,
      routingMessage: evt.routing_message || null,
      databaseNotes: [],
      generationFailures: [],
      executeResults: [],
      executeFailures: [],
      // This turn's connections, in their ORIGINAL (not completion) order -
      // the same order translate_routes.py's own final `sql_blocks` joins
      // in for the terminal line's `data.sql`. Kept here so each
      // connection's own "phase_b_connection_done" event (which arrives in
      // COMPLETION order - see that event's own docstring) can still be
      // slotted into sqlByIndex below at its correct ORIGINAL position,
      // keeping the progressively-built editor text in the same order the
      // terminal line will eventually settle on, regardless of which
      // connection's generation call happens to finish first.
      connectionOrder: connectionSelection,
      // One marked SQL string per connection above, filled in (by index,
      // not append order) as each connection's own generation finishes
      // with a real 'sql' outcome - see updateSqlEditorFromAllModeState()
      // below. A 'note'/'failed' outcome leaves its slot null, matching
      // sql_blocks' own "only entries with real SQL" filtering server-side.
      sqlByIndex: new Array(connectionSelection.length).fill(null),
      expectedTotal: connectionSelection.length,
      settledCount: 0,
      // How many connections have gotten their OWN "phase_b_connection_done"
      // event, regardless of outcome (note/failed/sql) - a pure "server-
      // side generation is done for this connection" signal, distinct from
      // settledCount just above (which only counts a real-SQL connection
      // once its execution ALSO finishes). Drives showAllModeStreamStatus()'s
      // "Generating commands (…)" -> "…and fetching results (…)" transition
      // below.
      generationSettledCount: 0,
      // Per-connection /api/execute calls kicked off below (auto-execute
      // only) - translatePrompt() awaits all of these before it can
      // safely call maybeFinalize(), since by then every one of these has
      // necessarily already been created (they're only ever pushed here,
      // synchronously, while /api/translate's own NDJSON body is still
      // being parsed - see readTranslateStream()'s docstring for why that
      // happens strictly before the terminal line resolves this promise).
      pendingExecutions: [],
      terminalData: null,
      modelEntry: null,
      finalized: false,
      // Captured once per turn (rather than re-read live) so a mid-flight
      // preference change can't make one turn behave inconsistently -
      // some connections streaming-executed, others not.
      autoExecute: autoSqlExecuteEnabled,
    };

    // Start empty rather than leaving whatever the PREVIOUS turn left
    // behind sitting there - updateSqlEditorFromAllModeState() below fills
    // this in progressively, one connection's marked SQL at a time, well
    // before the terminal /api/translate line (which used to be the ONLY
    // thing that ever touched this box for a "route" outcome) arrives.
    setSqlQuery('');

    const summaryTab = allModeStreamState.routingMessage
      ? [{ isText: true, tabLabel: 'Summary', text: SUMMARY_TAB_BLOCK_MARKER + allModeStreamState.routingMessage }]
      : [];
    const placeholderTabs = connectionSelection.map((e) => ({
      isPending: true,
      tabLabel: 'Fetching…',
      database: { kind: e.kind, id: e.id, name: e.name },
    }));
    currentResultsList = [...summaryTab, ...placeholderTabs];
    activeResultIndex = 0;
    buildResultsTabsNav();
    renderTableResult(currentResultsList[0] || null);
    showAllModeStreamStatus(allModeStreamState);
  }

  // Rebuilds the SQL editor's text from every connection whose SQL has
  // arrived SO FAR, in ORIGINAL connection order (state.connectionOrder) -
  // called once per real 'sql' outcome as handlePhaseBConnectionDone()
  // receives it, so the box fills in progressively, one database's command
  // at a time, instead of staying empty until every connection is done.
  // Joined with the same "\n\n" separator translate_routes.py's own final
  // `sql_blocks` join uses, so the text this builds up is byte-identical
  // to the terminal line's `data.sql` once every connection has reported
  // in - that terminal line still overwrites this box one more time when
  // it arrives (see translatePrompt()'s router_route branch), which is
  // harmless (same text) when every connection succeeded, and is what
  // actually applies formatSql() to the final result; it's also the ONLY
  // thing that repopulates this box at all for the non-streaming
  // (pendingAllModeNotes) fallback, since that path never calls this.
  function updateSqlEditorFromAllModeState(state) {
    const combined = state.sqlByIndex.filter(Boolean).join('\n\n');
    setSqlQuery(combined);
  }

  // Locates the still-pending placeholder tab for one connection - always
  // by (kind, id), never by a snapshotted array index, since several
  // connections' own handlers can each replace an entry in
  // currentResultsList across an `await` boundary (a /api/execute round
  // trip), which would silently invalidate any index captured beforehand.
  function findAllModePendingIndex(kind, id) {
    return currentResultsList.findIndex(
      (r) => r.isPending && r.database && r.database.kind === kind && r.database.id === id
    );
  }

  function replaceAllModePlaceholder(dbRef, tab) {
    const idx = findAllModePendingIndex(dbRef.kind, dbRef.id);
    if (idx >= 0) {
      currentResultsList[idx] = tab;
    } else {
      // Shouldn't happen in practice (every selected connection gets a
      // placeholder up front in startAllModeStreaming()) - falling back
      // to appending rather than silently dropping the result keeps this
      // defensive rather than lossy.
      currentResultsList.push(tab);
    }
  }

  // Same as replaceAllModePlaceholder above, but for a connection whose
  // OWN script had more than one statement (or partially failed partway
  // through one) - splices ALL of `tabs` in at that connection's single
  // placeholder position, instead of collapsing them down to one. Without
  // this, a multi-statement per-database script in "all databases" mode
  // only ever showed its FIRST statement's tab (see
  // executeOneAllModeConnection()'s own history comment) - single-
  // connection mode already shows one tab per statement
  // (renderMultiTurnResults/renderResultsWithFailedStatement), this brings
  // "all databases" mode's per-connection results to the same behavior.
  function replaceAllModePlaceholderWithMany(dbRef, tabs) {
    const idx = findAllModePendingIndex(dbRef.kind, dbRef.id);
    if (idx >= 0) {
      currentResultsList.splice(idx, 1, ...tabs);
    } else {
      // Same defensive fallback as replaceAllModePlaceholder above.
      currentResultsList.push(...tabs);
    }
  }

  // Settles every connection referenced in `results` and/or `failures`
  // from a BATCHED /api/execute call made against a live "all databases"
  // streaming turn - used by executeSql()'s two router-route call sites
  // below (the manual-Execute-button path, taken when auto-execute was
  // off for one or more connections still sitting in a "Ready to
  // execute" placeholder when the user clicked Execute). Groups `results`
  // by connection first - a connection whose own script had more than one
  // statement gets ALL of them as separate tabs, same fix as
  // executeOneAllModeConnection() below - then appends that connection's
  // own failure tab (if `failures` has a matching entry) right after its
  // succeeded tabs, same "don't lose partial results just because
  // something later in the same script failed" behavior
  // renderResultsWithFailedStatement already gives single-connection mode.
  // Increments state.settledCount exactly ONCE per distinct connection
  // touched here, regardless of how many statements/tabs it produced -
  // settledCount must stay a per-CONNECTION counter (see expectedTotal's
  // and showAllModeStreamStatus()'s own "M of N done" semantics), not a
  // per-tab one.
  function settleAllModeBatchedResults(state, results, failures) {
    const byConnection = new Map();
    const order = [];
    const keyOf = (db) => `${db.kind}:${db.id}`;

    (Array.isArray(results) ? results : []).forEach((result) => {
      const db = result.database || {};
      const key = keyOf(db);
      if (!byConnection.has(key)) {
        byConnection.set(key, { db, tabs: [] });
        order.push(key);
      }
      byConnection.get(key).tabs.push(result);
    });

    (Array.isArray(failures) ? failures : []).forEach((f) => {
      const db = f.database || {};
      const key = keyOf(db);
      if (!byConnection.has(key)) {
        byConnection.set(key, { db, tabs: [] });
        order.push(key);
      }
      byConnection.get(key).tabs.push({
        isError: true,
        error: f.error || 'An error occurred during SQL execution.',
        statement: f.failedStatement || '',
        database: db,
      });
      state.executeFailures.push(f);
    });

    order.forEach((key) => {
      const { db, tabs } = byConnection.get(key);
      replaceAllModePlaceholderWithMany(db, tabs);
      // Only the real result tabs feed Phase C/history - the synthetic
      // error tab pushed above isn't a row set, and state.executeFailures
      // already recorded the failure info itself.
      tabs.filter((t) => !t.isError).forEach((t) => state.executeResults.push(t));
      state.settledCount += 1;
    });
  }

  // Re-renders the tabs nav/active tab in place after a placeholder was
  // just swapped for real content, and refreshes the progress banner.
  // Deliberately does NOT change which tab is active (unlike
  // renderAllModeCombinedResults' one-shot "jump to the first failure"
  // behavior) - the whole point of streaming is that whichever tab the
  // user is currently looking at flips from "Fetching…" to real content
  // in place, without yanking their view elsewhere.
  function rerenderAllModeStream() {
    const state = allModeStreamState;
    if (!state) return;
    if (activeResultIndex >= currentResultsList.length) activeResultIndex = 0;
    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex] || null);
    showAllModeStreamStatus(state);
  }

  // Reuses the existing retry-status banner element/styling (see
  // showRetryStatus()/hideRetryStatus() above) - it's never shown at the
  // same time as a real per-attempt retry (that's a single-connection-
  // only code path), so there's no risk of the two treading on each
  // other.
  //
  // With auto-execute ON, generation and fetching are NOT sequential
  // phases - handlePhaseBConnectionDone() fires a connection's own
  // /api/execute call the instant THAT connection's SQL is generated,
  // without waiting for any other still-in-flight connection's own
  // generation call to finish (see its own dispatch of
  // executeOneAllModeConnection()). So while database A is still having
  // its SQL written, database B's results may already be coming back -
  // a single combined line reports both counts at once instead of a
  // "writing" -> "fetching" handoff that would misrepresent that overlap
  // (and, worse, could show a stale "writing" message while a fetch had
  // already failed or finished):
  //   - state.generationSettledCount: connections whose OWN
  //     "phase_b_connection_done" event has arrived (note/failed/sql,
  //     regardless of whether a real-SQL one has been executed yet).
  //   - state.settledCount: connections fully done end to end (a
  //     note/failed outcome settles immediately since it needs no
  //     execution; a real-SQL outcome settles once its own execute call
  //     returns).
  //
  // With auto-execute OFF, nothing is ever fetched during streaming -
  // every real-SQL connection just sits in its own "Ready to execute"
  // placeholder until the user clicks Execute - so only the generation
  // count is shown, and the banner hides once generation is done for
  // every connection rather than claiming a fetch that isn't happening.
  function showAllModeStreamStatus(state) {
    if (!resultsRetryStatus) return;
    const total = state.expectedTotal;

    if (!state.autoExecute) {
      if (state.generationSettledCount >= total) {
        hideAllModeStreamStatus();
        return;
      }
      resultsRetryStatus.innerHTML =
        `<span class="retry-status-icon animate-spin">⟳</span> ` +
        `Generating commands (${state.generationSettledCount} of ${total})…`;
      resultsRetryStatus.classList.remove('hidden');
      return;
    }

    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> ` +
      `Generating commands (${state.generationSettledCount} of ${total}) and ` +
      `fetching results (${state.settledCount} of ${total})…`;
    resultsRetryStatus.classList.remove('hidden');
  }

  function hideAllModeStreamStatus() {
    hideRetryStatus();
  }

  // Shown while Phase C's summarization call is in flight (see
  // requestAllModeResultsSummary() and each of its call sites below) -
  // reuses the same banner element/styling as showAllModeStreamStatus()
  // above. Previously this window had NO visible indicator at all: every
  // call site hid (or simply never showed) the progress banner as soon as
  // every selected connection settled, then made ANOTHER full network
  // round trip - a real LLM call - with nothing on screen suggesting the
  // app was still working, while the Translate/Execute buttons stayed
  // disabled for its entire duration.
  function showAllModeSummarizingStatus() {
    if (!resultsRetryStatus) return;
    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> Summarizing results…`;
    resultsRetryStatus.classList.remove('hidden');
  }

  // Handles one "phase_b_connection_done" NDJSON event (see
  // translate_routes.py's stream_translation() docstring) - called once
  // per selected connection, in COMPLETION order (not necessarily the
  // order connection_selection listed them in).
  function handlePhaseBConnectionDone(evt) {
    const state = allModeStreamState;
    if (!state) return; // a phase_a_route event always precedes this - defensive only

    // Generation is done for this connection regardless of outcome - see
    // generationSettledCount's own declaration comment and
    // showAllModeStreamStatus()'s "Generating commands…" -> "…and fetching
    // results…" transition.
    state.generationSettledCount += 1;

    if (evt.outcome === 'note') {
      const tab = {
        isText: true, tabLabel: 'Note',
        text: evt.text || 'No response was returned for this database.',
        database: { kind: evt.kind, id: evt.id, name: evt.name },
      };
      replaceAllModePlaceholder({ kind: evt.kind, id: evt.id }, tab);
      if (evt.text) {
        state.databaseNotes.push({ kind: evt.kind, id: evt.id, name: evt.name, text: evt.text });
      }
      state.settledCount += 1;
      rerenderAllModeStream();
      return;
    }

    if (evt.outcome === 'failed') {
      const tab = {
        isError: true, error: evt.error || 'An error occurred generating SQL for this database.',
        database: { kind: evt.kind, id: evt.id, name: evt.name },
      };
      replaceAllModePlaceholder({ kind: evt.kind, id: evt.id }, tab);
      state.generationFailures.push({ kind: evt.kind, id: evt.id, name: evt.name, error: evt.error });
      state.settledCount += 1;
      rerenderAllModeStream();
      return;
    }

    // evt.outcome === 'sql' - slot this connection's marked SQL into its
    // ORIGINAL position (not append order - see connectionOrder's own
    // declaration comment) and refresh the editor immediately, regardless
    // of whether auto-execute goes on to fetch anything for it, so the box
    // fills in the moment each connection's own generation call returns.
    const orderIndex = state.connectionOrder.findIndex(
      (e) => e.kind === evt.kind && e.id === evt.id
    );
    if (orderIndex >= 0) state.sqlByIndex[orderIndex] = evt.sql;
    updateSqlEditorFromAllModeState(state);

    if (!state.autoExecute) {
      // Leave the placeholder in place (just relabeled) - this connection
      // only settles once the user clicks Execute manually, which re-runs
      // today's existing BATCHED /api/execute flow (see executeSql()'s
      // router-route branch below) for every connection still in this
      // state at once.
      const idx = findAllModePendingIndex(evt.kind, evt.id);
      if (idx >= 0) {
        currentResultsList[idx] = { ...currentResultsList[idx], tabLabel: 'Ready to execute' };
        rerenderAllModeStream();
      }
      return;
    }

    state.pendingExecutions.push(executeOneAllModeConnection(evt));
    // Re-render immediately (rather than waiting for this or some OTHER
    // connection's own execution to finish) so the banner's "and fetching
    // results (…)" clause appears the instant generation finishes for
    // every connection, not whenever the next unrelated rerender happens
    // to occur afterward.
    rerenderAllModeStream();
  }

  // Fires a single-connection /api/execute call the moment its own SQL
  // has been generated (evt.sql already carries the '-- database: ...'
  // marker translate_routes.py prepended) - exploiting execute_routes.py's
  // existing single-marker-group handling, which already works correctly
  // for exactly one connection's SQL with zero backend changes. Never
  // awaited by its caller inline - tracked in
  // allModeStreamState.pendingExecutions instead, so N connections'
  // executions can run fully in parallel with each other (and with
  // whichever other connections' generation calls are still in flight).
  async function executeOneAllModeConnection(evt) {
    const state = allModeStreamState;
    const dbRef = { kind: evt.kind, id: evt.id, name: evt.name };
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({ sql: evt.sql, pinned_connections: PINNED_CONNECTIONS }),
      });
      const data = await response.json();
      const succeeded = Array.isArray(data.results) ? data.results : [];
      succeeded.forEach((r) => { if (!r.database) r.database = dbRef; });

      if (response.ok && data.success && succeeded.length) {
        // This connection's script may have had more than one statement -
        // every one of them gets its own tab (splice, not a single
        // replace), same as single-connection mode's renderMultiTurnResults
        // already does. Previously only succeeded[0] was ever kept,
        // silently dropping every statement after the first.
        replaceAllModePlaceholderWithMany(dbRef, succeeded);
        state.executeResults.push(...succeeded);
      } else {
        // Either a total failure (e.g. connect() error - no succeeded
        // statements at all) or a PARTIAL one: this connection's script had
        // more than one statement and failed partway through (see
        // execute_routes.py's SqlExecutionError-shaped `failures` entry),
        // in which case `succeeded` still holds every statement that ran
        // BEFORE the failure. Either way, keep whatever succeeded as its
        // own tab(s) instead of discarding it just because something later
        // in the same script failed - same behavior single-connection
        // mode's renderResultsWithFailedStatement already gives.
        const failureInfo = (Array.isArray(data.failures) && data.failures[0]) || null;
        const errMsg = (data && data.error) || (failureInfo && failureInfo.error)
          || 'An error occurred during SQL execution.';
        const errorTab = {
          isError: true, error: errMsg,
          statement: (failureInfo && failureInfo.failedStatement) || '',
          database: dbRef,
        };
        replaceAllModePlaceholderWithMany(dbRef, [...succeeded, errorTab]);
        state.executeResults.push(...succeeded);
        state.executeFailures.push({ database: dbRef, error: errMsg });
      }
    } catch (err) {
      // cancelInFlightQuery() may have already replaced `allModeStreamState`
      // (or set it to null) by the time this abort actually rejects -
      // mutating `state` (captured above, from the turn THIS call started
      // in) past this point would touch a stale/replaced object rather
      // than whatever the CURRENT turn (if any) is now using. Must be
      // checked before anything below touches `state`.
      if (err && err.name === 'AbortError') {
        return;
      }
      const errMsg = err.message || 'Failed to reach the execution backend server.';
      replaceAllModePlaceholder(dbRef, { isError: true, error: errMsg, database: dbRef });
      state.executeFailures.push({ database: dbRef, error: errMsg });
    }
    state.settledCount += 1;
    rerenderAllModeStream();
  }

  // Runs once every selected connection has settled (note, generation
  // failure, or executed-or-failed) AND the terminal /api/translate line
  // has arrived (translatePrompt() stashes it onto
  // allModeStreamState.terminalData/.modelEntry - see its router_route
  // branch) - these two conditions are checked independently since they
  // can complete in either order: the terminal line always arrives no
  // later than the LAST phase_b_connection_done event server-side, but
  // each connection's own CLIENT-driven /api/execute call is a separate
  // race that can easily still be in flight once the terminal line shows
  // up. Idempotent (guarded by .finalized) - safe to call from more than
  // one place without double-running Phase C or double-persisting
  // history.
  async function maybeFinalize() {
    const state = allModeStreamState;
    if (!state || state.finalized) return;
    if (state.settledCount < state.expectedTotal) return;
    if (!state.terminalData) return;
    state.finalized = true;

    const notes = {
      prompt: state.prompt,
      routingMessage: state.routingMessage,
      databaseNotes: state.databaseNotes,
      generationFailures: state.generationFailures,
    };

    // Phase C - see requestAllModeResultsSummary's docstring. Awaited so
    // callers (translatePrompt()/executeSql()) keep their buttons
    // disabled for this extra round trip, same as the pre-streaming
    // batched flow always did. The progress banner switches to a
    // "Summarizing…" message for this call rather than disappearing
    // beforehand (as it used to) - this is a real, separate LLM call that
    // can take a moment, and previously nothing on screen indicated the
    // app was still working during it.
    showAllModeSummarizingStatus();
    await requestAllModeResultsSummary(notes, state.executeResults, state.executeFailures);
    hideAllModeStreamStatus();
    const summaryEntry = getSummaryTabEntry();
    if (summaryEntry) notes.routingMessage = summaryEntry.text;

    const modelEntry = state.modelEntry;
    if (modelEntry) {
      const summarizedResults = state.executeResults.map(summarizeResultForHistory);
      if (chatStore.getPending() && !chatStore.isPendingCurrent()) {
        // Stale reference (e.g. left over from navigating through a
        // no-SQL turn) - drop it rather than risk mutating the wrong turn.
        chatStore.clearPending();
      }
      if (chatStore.isPendingCurrent()) {
        // SQL just generated by this same turn and now executed for the
        // first time - fill in its results rather than creating a
        // duplicate turn.
        const pending = chatStore.getPending();
        pending.entry.results = summarizedResults;
        captureAllModeHistory(pending.entry, notes, state.executeFailures);
        chatStore.clearPending();
      } else {
        modelEntry.results = summarizedResults;
        captureAllModeHistory(modelEntry, notes, state.executeFailures);
      }
    }

    allModeStreamState = null;
  }

  // ===========================================================================
  // 9. TRANSLATE (NL -> SQL) AND EXECUTE SQL
  // ===========================================================================
  async function translatePrompt() {
    // Synchronous re-entrancy guard - see uiActionBusy's declaration
    // comment above. Must be the very first thing this function does,
    // before the `await` below, so the check-and-set is atomic.
    if (uiActionBusy) return;
    uiActionBusy = true;
    setButtonsDisabled(true);
    // See currentAbortController/currentTurnId's own declaration comments
    // above - this is a NEW, non-internal turn, so both get a fresh value
    // here (never mutated in place).
    const myTurnId = ++currentTurnId;
    currentAbortController = new AbortController();

    try {
    await fetchBackendConfig();

    clearResultsDisplay();
    // Reset the "all databases" mode streaming state left over from a
    // PREVIOUS router_route turn - reset unconditionally so a stale
    // summary/note/placeholder set never leaks into this turn's rendering
    // (re-created below, by startAllModeStreaming(), only if THIS
    // response's own stream turns out to carry a "phase_a_route" event).
    allModeStreamState = null;
    // Same reset for the no-live-stream fallback (see its own declaration
    // comment above).
    pendingAllModeNotes = null;

    const promptText = aiPrompt ? aiPrompt.value.trim() : "";
    if (!promptText) return;

    // Fired on submission, not completion - "Errors surfaced" (see
    // trackEvent('error_shown', ...) below) is its own separate event, so
    // this one doesn't need to thread a success/failure outcome back
    // through this function's many branches (router-mode streaming,
    // NO-SQL replies, plain SQL, every error shape) just to report it here
    // too.
    // No `prompt` field - the NL prompt text itself isn't sent to GA (privacy).
    trackEvent('translate_submitted', {
      mode: isAllConnectionsSelected() ? 'all' : 'single',
      database_name: connDbName ? connDbName.textContent : '',
      database_type: getActiveDatabaseType(),
      provider: ACTIVE_LLM_PROVIDER || '',
      model: ACTIVE_LLM_MODEL || '',
    });

    // Not sending a database_url override here: fetchBackendConfig()
    // above already synced session state, and the server resolves the
    // active connection (Postgres or BigQuery, with its full descriptor)
    // from that session. A bare URL override would only be able to
    // express a Postgres connection, silently breaking a BigQuery session.
    //
    // Exactly one retry loop for a translation exists in this app, and it
    // lives server-side (translate_routes.py's per-Gemini-call loop, which
    // classifies the failure and can rotate API keys - something this
    // client has no visibility into). This used to also retry the whole
    // /api/translate request client-side after the server had already
    // exhausted its own attempts, which just silently repeated the same
    // exhausted attempt budget on top of the server's, multiplying total
    // latency on a genuinely-down/exhausted backend with nothing to show
    // for it. A single request, a single attempt.
    let response = null;
    let data = null;

    try {
      response = await fetch('/api/translate', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController.signal,
        body: JSON.stringify({
          prompt: promptText,
          history: chatStore.toPayload(),
          // translate_routes.py's /api/translate handler doesn't read this
          // key at all - the only server-side consumer of a client-echoed
          // pinned_connections entry today is execute_routes.py's
          // marker-free fallback (see below). Sent here anyway since it's
          // harmless and keeps this payload shape consistent with
          // /api/execute's - see PINNED_CONNECTIONS' docstring.
          pinned_connections: PINNED_CONNECTIONS
        })
      });

      data = await readTranslateStream(response, (evt) => {
        if (evt.status === 'retrying') { showRetryStatus(evt); return; }
        // Single-connection mode: "reading the schema" / "writing the
        // right command for the database". "All databases" mode ALSO
        // emits this same event kind,
        // with its own two `phase` values ("collecting_schema_summaries"/
        // "routing") for its own two pre-triage waits - see
        // translate_routes.py's stream_translation() docstring. Handled
        // identically either way: showPhaseStatus() just renders
        // evt.message verbatim, so no mode-specific branching is needed
        // here at all. Whichever router-mode events fire next
        // (phase_a_route/phase_b_connection_done below) naturally
        // overwrite this same banner once they arrive.
        if (evt.status === 'phase_status') { showPhaseStatus(evt); return; }
        // "All databases" mode's "route" outcome streams these two extra
        // event kinds ahead of the terminal line - see
        // translate_routes.py's stream_translation() docstring and
        // startAllModeStreaming()/handlePhaseBConnectionDone() above.
        // Neither ever fires for any other response shape.
        if (evt.status === 'phase_a_route') { startAllModeStreaming(evt, promptText); return; }
        if (evt.status === 'phase_b_connection_done') { handlePhaseBConnectionDone(evt); return; }
      });
      hideRetryStatus();

      // connection_selection is only ever present when this session had
      // 2+ connections in scope for this turn (see translate_routes.py's
      // module docstring) - absent entirely otherwise, in which case
      // PINNED_CONNECTIONS is left as whatever it already was (a NO-SQL/
      // help response, or a plain error, doesn't change what's pinned).
      // Which database(s) were actually used is still disclosed to the
      // user via the per-tab name line (buildResultsTabsNav) and the
      // `-- database: ...` comment translate_routes.py writes directly
      // into the generated SQL - no separate banner needed on top of that.
      if (data && data.connection_selection && data.connection_selection.length) {
        PINNED_CONNECTIONS = data.connection_selection.map(e => ({ kind: e.kind, id: e.id }));
      }

      // A streamed translation failure (every retry exhausted, or a
      // non-retryable error) comes back as HTTP 200 with success:false in
      // the terminal line, not a real error status - see
      // translate_routes.py's module docstring for why. The !data.sql
      // check below already treats that the same as any other failure, so
      // no separate handling is needed here; response.ok only still
      // matters for the auth-guard's real 401 (checked below) and for the
      // early-validation 400s (missing prompt/API key), which return a
      // real error status because they're not streamed at all.
      if (response && response.ok && data && data.router_route) {
        // "All databases" mode's "route" outcome (see translate_routes.py's
        // module docstring): the Summary/per-database tabs were already
        // rendered PROGRESSIVELY as this stream's own "phase_a_route"/
        // "phase_b_connection_done" events arrived (see
        // startAllModeStreaming()/handlePhaseBConnectionDone() above) -
        // `allModeStreamState` is non-null here precisely when that
        // happened. `data.sql` may legitimately be empty (every selected
        // database noted or failed instead of returning real SQL) -
        // checked as its own branch, ahead of the plain `data.sql` check
        // below, precisely because that empty-string case must NOT fall
        // through to the "Translation Error" branch the way a truly
        // absent/falsy `sql` would for every other response shape.
        const modelEntry = { role: 'model', text: data.sql || '' };
        chatStore.pushTurn(promptText, modelEntry);
        updateHistoryTurnsSubtitle();

        if (data.sql) {
          setSqlQuery(data.sql);
          chatStore.setPending(modelEntry, normalizeSqlForCompare(data.sql));
        } else {
          setSqlQuery('');
          chatStore.clearPending();
        }

        if (allModeStreamState) {
          // Attach this terminal line/turn's modelEntry so maybeFinalize()
          // can persist history once every selected connection has
          // actually settled - auto-execute may have already kicked off
          // per-connection /api/execute calls above (via
          // handlePhaseBConnectionDone()) that are still in flight, so
          // wait for every one of them before even attempting to finalize
          // (maybeFinalize() itself no-ops until settledCount reaches
          // expectedTotal - e.g. auto-execute off, with real SQL still
          // sitting in a "Ready to execute" placeholder).
          allModeStreamState.terminalData = data;
          allModeStreamState.modelEntry = modelEntry;
          await Promise.all(allModeStreamState.pendingExecutions);
          await maybeFinalize();
        } else {
          // No live "phase_a_route"/"phase_b_connection_done" events ever
          // arrived for this turn (see pendingAllModeNotes' own
          // declaration comment above for when this happens) - fall back
          // to the ORIGINAL, fully batched rendering this app used for
          // every router_route turn before progressive streaming existed.
          pendingAllModeNotes = {
            // The ORIGINAL question that started this whole turn -
            // captured here (not re-read from aiPrompt.value later) since
            // that field may have already changed by the time Phase C's
            // summarization request goes out (see
            // requestAllModeResultsSummary() below).
            prompt: promptText,
            routingMessage: data.routing_message || null,
            databaseNotes: data.database_notes || [],
            generationFailures: data.generation_failures || [],
          };

          if (data.sql) {
            if (autoSqlExecuteEnabled) {
              await executeSql(null, { internal: true });
            }
          } else {
            // Nothing to execute at all - render immediately, with no
            // /api/execute call, straight from the notes/failures already
            // stashed above.
            captureAllModeHistory(modelEntry, pendingAllModeNotes, []);
            renderAllModeCombinedResults({
              notes: pendingAllModeNotes,
              executeResults: [],
              executeFailures: [],
            });
            pendingAllModeNotes = null;
          }
        }
      } else if (response && response.ok && data && data.sql) {
        const trimmedSql = data.sql.trim();
        const isOpenHelp = trimmedSql.toUpperCase().includes('OPEN HELP POPUP');
        const isNoSql = trimmedSql.startsWith('*** NO SQL ***');

        const modelEntry = { role: 'model', text: data.sql };
        chatStore.pushTurn(promptText, modelEntry);
        updateHistoryTurnsSubtitle();

        if (isOpenHelp) {
          setSqlQuery('');
          chatStore.clearPending();
          clearResultsDisplay();

          if (helpModal) {
            openHelpModal();
          }
        } else if (isNoSql) {
          setSqlQuery('');
          chatStore.clearPending();
          // "All databases" mode's own triage "answer" outcome (no
          // real data needed) shares this exact branch with a plain
          // single-connection reply - `data.router_route` is only ever
          // set for a "route" outcome (see the branch above) - but ONLY
          // the all-mode case carries the leading-label convention (see
          // renderMarkdownLiteSummaryTab()'s docstring); a single-
          // connection reply never does. IN_SCOPE_MODE reflects the mode
          // this very request was just sent under, which is what decides
          // which of the two this is.
          renderNoSqlResponse(data.sql, { hasLabel: IN_SCOPE_MODE === 'all' });
        } else {
          setSqlQuery(data.sql);
          chatStore.setPending(modelEntry, normalizeSqlForCompare(data.sql));

          if (autoSqlExecuteEnabled) {
            await executeSql(null, { internal: true });
          }
        }
      } else {
        setSqlQuery('');

        const errMsg = response && response.status === 401
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to log in."
          : (data?.error || "An error occurred during translation.");
        console.error("Translation Error:", errMsg);
        trackEvent('error_shown', {
          category: 'translation',
          database_name: connDbName ? connDbName.textContent : '',
          database_type: getActiveDatabaseType(),
          message: truncateForAnalytics(errMsg),
        });

        if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
        if (resultsHeader) resultsHeader.innerHTML = '';
        if (resultsBody) {
          resultsBody.innerHTML = `
            <tr>
              <td class="error-cell">
                <div class="error-container">
                  <span class="error-icon">⚠️</span>
                  <div class="error-details">
                    <strong>Translation Error</strong>
                    <p>${errMsg}</p>
                  </div>
                </div>
              </td>
            </tr>`;
        }
      }
    } catch (err) {
      // cancelInFlightQuery() (the Cancel button) has ALREADY fully reset the
      // UI synchronously by the time an aborted fetch's promise rejects -
      // letting this branch also render a "Network Error" tile (or touch
      // any shared state below) on top of that would be wrong. Must be
      // checked before anything else in this branch.
      if (err && err.name === 'AbortError') {
        return;
      }
      setSqlQuery('');

      const errMsg = err.message || "Failed to reach the translation backend server.";
      console.error("Failed to translate prompt:", err);

      if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
      if (resultsHeader) resultsHeader.innerHTML = '';
      if (resultsBody) {
        resultsBody.innerHTML = `
          <tr>
            <td class="error-cell">
              <div class="error-container">
                <span class="error-icon">⚠️</span>
                <div class="error-details">
                  <strong>Translation Network Error</strong>
                  <p>${errMsg}</p>
                </div>
              </div>
            </td>
          </tr>`;
      }
    }
    } finally {
      // Safety net for the network-error path (readTranslateStream()
      // itself throwing, e.g. the connection dropping mid-stream) - the
      // explicit hideRetryStatus() call above only runs once the stream
      // actually finished parsing. Also the single place that clears
      // uiActionBusy - this outer finally covers every exit path from the
      // try above, including the early `if (!promptText) return;`, so the
      // guard can never get stuck "on" after a real turn ends.
      //
      // Guarded by myTurnId === currentTurnId (see that variable's
      // declaration comment) so a CANCELLED or superseded turn's own
      // eventual cleanup - which can still run here well after the Stop
      // click, since aborting doesn't retroactively skip this finally -
      // never clobbers a newer turn's buttons/uiActionBusy state.
      if (myTurnId === currentTurnId) {
        hideRetryStatus();
        setButtonsDisabled(false);
        uiActionBusy = false;
      }
    }
  }

  function normalizeSqlForCompare(sql) {
    return (sql || '').replace(/\s+/g, ' ').trim().replace(/;+\s*$/, '');
  }

  // NOTE: previously capped at 25 rows before entering chatHistory, which
  // meant the model's context silently diverged from what the results table
  // actually showed the user. Sending the full result set now instead, so
  // "what's shown in the UI" and "what the model sees" stay in sync. This can
  // bloat prompt size / token usage for large result sets - revisit with a
  // smarter truncation (e.g. size-based cap with an explicit "...N more rows"
  // marker) if that becomes a problem in practice.
  function summarizeResultForHistory(result) {
    const rows = result.rows || [];
    const summarized = {
      columns: result.columns || [],
      rowCount: result.rowCount !== undefined ? result.rowCount : rows.length,
      rows: rows
    };
    // "All databases" mode results are tagged with which connection they
    // came from (see execute_routes.py and buildResultsTabsNav()'s dbLabel)
    // - preserve that tag so a later history restore can still label each
    // tab by database name instead of a bare "Query N". Server-side history
    // formatting (build_gemini_history_contents et al.) only ever reads
    // columns/rows/rowCount and ignores unknown keys, so this is harmless
    // for what actually reaches the LLM.
    if (result.database) summarized.database = result.database;
    return summarized;
  }

  async function executeSql(customSql = null, { internal = false } = {}) {
    // Synchronous re-entrancy guard - see uiActionBusy's declaration
    // comment above. Skipped when `internal` is true: that's set only by
    // translatePrompt()'s own two `autoSqlExecuteEnabled` call sites
    // (above, in this same file), which are already-awaited, sequential
    // (never concurrent) nested calls made while translatePrompt() itself
    // still holds the flag for this whole turn - re-checking/re-setting/
    // resetting it here would
    // either be a same-call no-op deadlock (guard already true) or, worse,
    // clear the flag out from under translatePrompt() while it still has
    // work left to do (e.g. maybeFinalize()) after this call returns.
    if (!internal) {
      if (uiActionBusy) return;
      uiActionBusy = true;
      setButtonsDisabled(true);
      // See currentAbortController/currentTurnId's declaration comments
      // above - only a NEW, non-internal turn gets a fresh value; an
      // internal call (from translatePrompt()) is part of the SAME turn
      // as its caller, so it must NOT bump/replace either one here.
      currentAbortController = new AbortController();
      currentTurnId += 1;
    }
    // Read (never bump) here so an internal call captures the enclosing
    // turn's own (already-current) id, while a non-internal call captures
    // the value it just bumped above.
    const myTurnId = currentTurnId;

    try {
    await fetchBackendConfig();

    // A live "all databases" mode streaming turn (auto-execute was off,
    // and the user is now clicking Execute manually - see
    // handlePhaseBConnectionDone()'s "Ready to execute" placeholders and
    // this function's own router-route branch below) already has its
    // Summary/Note/generation-failure/placeholder tabs live in
    // currentResultsList - preserve them instead of wiping the results
    // area, since this call only needs to settle whichever placeholders
    // are still pending, not rebuild everything from scratch.
    if (!allModeStreamState) {
      clearResultsDisplay();
    }

    const sql = customSql || getSqlQuery();
    if (!sql) return;

    // Fired on submission, same reasoning as translate_submitted above -
    // "Errors surfaced" is its own separate event, so this doesn't need to
    // thread an outcome back through this function's own many branches.
    // No `sql` field - the generated SQL text itself isn't sent to GA (privacy).
    trackEvent('sql_executed', {
      database_name: connDbName ? connDbName.textContent : '',
      database_type: getActiveDatabaseType(),
      trigger: internal ? 'auto' : 'manual',
    });

    // Single-connection mode's own "fetching" indicator - see
    // showFetchingResultsStatus()'s own declaration comment. Neither "all
    // databases" mode path shows this: a live streaming turn
    // (allModeStreamState) settles each connection's execution
    // individually via executeOneAllModeConnection() rather than through
    // this shared function at all, and the manual-Execute-button fallback
    // (pendingAllModeNotes, auto-execute was off) already has its own
    // Phase-C-oriented banners further down.
    if (!allModeStreamState && !pendingAllModeNotes) showFetchingResultsStatus();

    // See the comment in translatePrompt() above - no database_url
    // override here either, for the same reason.
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({
          sql: sql,
          // Harmless whenever `sql` carries no '-- database: ...' markers
          // (execute_routes.py's marker-free fast path ignores this
          // entirely) - only meaningful for a hand-edited/re-run
          // multi-database script with no markers left at all, where it's
          // the fallback target (see execute_routes.py's module docstring
          // and its resolve_descriptor_by_reference fallback just above).
          pinned_connections: PINNED_CONNECTIONS
        })
      });
  
      const data = await response.json();
      if (response.ok && data.success) {
        // A live "all databases" mode streaming turn (see this function's
        // top comment above) - this batched call only ever carries SQL
        // for connections still sitting in a "Ready to execute"
        // placeholder (every noted/failed connection's own SQL was never
        // part of the editor's `sql` text in the first place - see
        // translate_routes.py's sql_blocks construction), so settle
        // exactly those, matched by the `.database` tag execute_routes.py
        // already attaches to each returned row, then let maybeFinalize()
        // handle Phase C and history exactly as it would for a fully
        // auto-executed turn.
        const streamState = allModeStreamState;
        if (streamState) {
          // Groups data.results by connection and settles each one - a
          // connection whose script had more than one statement gets a
          // tab per statement (see settleAllModeBatchedResults' own
          // docstring), not just its first.
          settleAllModeBatchedResults(streamState, data.results, null);
          rerenderAllModeStream();
          await maybeFinalize();
        } else {
          // pendingAllModeNotes fallback (see its own declaration comment
          // above) - a router_route turn that never got any live
          // "phase_a_route"/"phase_b_connection_done" events, so this is
          // still the ORIGINAL fully-batched render this app used for
          // every router_route turn before progressive streaming existed.
          const allModeNotes = pendingAllModeNotes;
          if (allModeNotes) {
            renderAllModeCombinedResults({
              notes: allModeNotes,
              executeResults: data.results,
              executeFailures: [],
            });
            pendingAllModeNotes = null;
            // Phase C - see requestAllModeResultsSummary's docstring.
            // Awaited (not fire-and-forget) so buttons stay disabled for
            // this extra round trip, same as every other step of this
            // function already does. That call mutates the Summary tab's
            // text in currentResultsList in place, once it resolves - pull
            // the (possibly now Phase-C-augmented) text back out
            // immediately after, so allModeNotes.routingMessage - and
            // therefore whatever gets persisted onto the history entry
            // just below - reflects the FINAL answer, not triage's
            // earlier, data-free guess at it. showAllModeSummarizingStatus()
            // gives this real network round trip a visible indicator -
            // previously there was none at all on this fallback path.
            showAllModeSummarizingStatus();
            await requestAllModeResultsSummary(allModeNotes, data.results, []);
            hideAllModeStreamStatus();
            const summaryEntry = getSummaryTabEntry();
            if (summaryEntry) allModeNotes.routingMessage = summaryEntry.text;
          } else {
            renderMultiTurnResults(data.results);
          }

          const promptText = aiPrompt && aiPrompt.value.trim() ? aiPrompt.value.trim() : "[Direct SQL Execution]";
          const summarizedResults = Array.isArray(data.results) ? data.results.map(summarizeResultForHistory) : [];

          if (chatStore.getPending() && !chatStore.isPendingCurrent()) {
            // Stale reference (e.g. left over from navigating through a no-SQL
            // turn) - drop it rather than risk mutating the wrong turn.
            chatStore.clearPending();
          }

          if (chatStore.isPendingCurrent()) {
            // SQL just generated by translate() and now executed for the first
            // time - fill in its results rather than creating a duplicate turn.
            const pending = chatStore.getPending();
            pending.entry.text = sql;
            pending.entry.results = summarizedResults;
            if (allModeNotes) captureAllModeHistory(pending.entry, allModeNotes, []);
            chatStore.clearPending();
          } else {
            // Any other execution (direct SQL entry, or re-running a query
            // that isn't the pending just-generated one) is its own turn.
            const modelEntry = { role: 'model', text: sql, results: summarizedResults };
            if (allModeNotes) captureAllModeHistory(modelEntry, allModeNotes, []);
            chatStore.pushTurn(promptText, modelEntry);
            updateHistoryTurnsSubtitle();
          }
        }

        if (connDbDot) connDbDot.className = 'status-dot connected';
      } else {
        const errMsg = response.status === 401
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to log in."
          : (data.error || "An error occurred during SQL execution.");

        // "All databases" mode's "route" outcome (see above) takes
        // priority over both existing failure shapes below - it needs the
        // Summary/Note text tabs alongside whatever DID execute, not just
        // the raw execute-failure shape those existing renderers show.
        if (allModeStreamState) {
          const streamState = allModeStreamState;
          // Same per-connection grouping as the success branch above - a
          // failed connection's OWN succeeded-before-the-failure
          // statements (already present in data.results, tagged with
          // that connection - see execute_routes.py's SqlExecutionError
          // handling) get their own tabs too, with the failure tab
          // appended right after them, instead of being discarded.
          settleAllModeBatchedResults(streamState, data.results, data.failures);
          rerenderAllModeStream();
          // Phase C - see requestAllModeResultsSummary's docstring. Still
          // worth attempting even on a partial failure: whatever DID
          // execute successfully is real data worth summarizing, and the
          // failed connection(s) are already fed in as their own entries
          // (see buildAllModeSummaryPayload) so the summary can note that
          // too if it affects the answer. Deliberately NOT routed through
          // maybeFinalize() here, matching this branch's pre-existing
          // behavior (from before this streaming redesign) of never
          // persisting history for a partial execute failure - only the
          // success branch above ever reaches maybeFinalize(). Same
          // "Summarizing…" indicator maybeFinalize() shows for this call -
          // previously this branch left the stale "N of N done" banner
          // (from rerenderAllModeStream() just above) sitting unchanged
          // through this entire extra network round trip.
          showAllModeSummarizingStatus();
          await requestAllModeResultsSummary(
            {
              prompt: streamState.prompt, routingMessage: streamState.routingMessage,
              databaseNotes: streamState.databaseNotes, generationFailures: streamState.generationFailures,
            },
            streamState.executeResults, streamState.executeFailures,
          );
          hideAllModeStreamStatus();
          allModeStreamState = null;
        // pendingAllModeNotes fallback (see its own declaration comment
        // above) - same "never persists history for a partial execute
        // failure" behavior the live-streaming branch just above
        // preserves, from before this streaming redesign.
        } else if (pendingAllModeNotes) {
          const allModeNotes = pendingAllModeNotes;
          const executeResults = Array.isArray(data.results) ? data.results : [];
          const executeFailures = Array.isArray(data.failures) ? data.failures : [];
          renderAllModeCombinedResults({
            notes: allModeNotes,
            executeResults: executeResults,
            executeFailures: executeFailures,
          });
          pendingAllModeNotes = null;
          showAllModeSummarizingStatus();
          await requestAllModeResultsSummary(allModeNotes, executeResults, executeFailures);
          hideAllModeStreamStatus();
        // Multi-database question-answering's own partial-failure shape
        // (see execute_routes.py's module docstring) - `failures` is a
        // LIST (one entry per connection that failed; the others keep
        // running independently), distinct from the single-connection
        // SqlExecutionError shape's one `failedStatement`/`error` pair
        // checked just below. Checked first since a multi-database
        // response's `results` array would otherwise also satisfy that
        // next branch's Array.isArray(data.results) check.
        } else if (Array.isArray(data.failures)) {
          renderResultsWithDatabaseFailures(data);
        // A single-connection multi-statement script that failed partway
        // through carries `results` (the statements that succeeded before
        // the failure) and `failedStatement` (see execute_routes.py's
        // module docstring) - render those as tabs, same as the success
        // case, with the failed one flagged, instead of one generic error
        // that loses track of what did or didn't run. A response with
        // neither key (e.g. a connect() failure, or a single-statement
        // script with nothing to report alongside it) falls back to the
        // original flat block.
        } else if (Array.isArray(data.results) || data.failedStatement !== undefined) {
          renderResultsWithFailedStatement({ ...data, error: errMsg });
        } else if (resultsBody) {
          // Bypasses renderTableResult() entirely (no per-tab result object
          // exists here - e.g. a bare connect() failure with nothing else
          // to show alongside it), so it needs its own setReportContext()
          // call rather than getting one for free, AND its own inline
          // button markup (reportButtonHtml() itself still gates on
          // ISSUE_REPORTING_ENABLED). Both are excluded when the failure
          // was actually an auth problem (401) - not a database error the
          // model's SQL caused, so out of scope for this feature the same
          // way a translation error is.
          const reportable = response.status !== 401;
          resultsBody.innerHTML = `
            <tr>
              <td class="error-cell">
                <div class="error-container">
                  <span class="error-icon">⚠️</span>
                  <div class="error-details">
                    <div class="error-title-row">
                      <strong>Execution Error</strong>
                      ${reportable ? reportButtonHtml('error') : ''}
                    </div>
                    <p>${errMsg}</p>
                  </div>
                </div>
              </td>
            </tr>`;
          // Tracked regardless of `reportable` - a 401 auth failure is out
          // of scope for the Report feature (see the comment above), but
          // it's still an error the user actually saw.
          trackEvent('error_shown', {
            category: 'execution',
            database_name: connDbName ? connDbName.textContent : '',
            database_type: getActiveDatabaseType(),
            message: truncateForAnalytics(errMsg),
          });
          if (reportable) {
            setReportContext({
              category: 'error',
              databaseName: connDbName ? connDbName.textContent : '',
              sql: sql,
              content: errMsg,
            });
          }
        }
      }
    } catch (err) {
      // See translatePrompt()'s identical guard for why - cancelInFlightQuery()
      // has already fully reset the UI synchronously by the time an
      // aborted fetch's promise rejects.
      if (err && err.name === 'AbortError') {
        return;
      }
      const errMsg = err.message || "Failed to reach the execution backend server.";
      console.error("Failed to execute SQL:", err);
    }
    } finally {
      // Clears showFetchingResultsStatus()'s banner (if it was ever shown
      // above) regardless of outcome or of `internal` - unlike the button/
      // flag reset just below, this one always needs to happen here: for
      // an internal call, translatePrompt() itself never shows or expects
      // to clear this particular banner, so nothing else would. Harmless
      // to call when nothing is showing (e.g. an "all databases" mode
      // call, which never sets it in the first place, or a call that hit
      // the early `if (!sql) return;` above).
      hideRetryStatus();
      // Mirrors the `if (!internal)` guard at the top - an internal call
      // (from translatePrompt()) leaves the flag and buttons exactly as
      // translatePrompt() left them, since it still has work left to do
      // (e.g. maybeFinalize()) after this call returns.
      //
      // Also guarded by myTurnId === currentTurnId (see translatePrompt()'s
      // identical guard) - without it, a cancelled-but-still-settling
      // external executeSql() call could re-enable buttons/clear
      // uiActionBusy out from under a NEWER turn the user already started
      // after clicking Cancel.
      if (!internal && myTurnId === currentTurnId) {
        setButtonsDisabled(false);
        uiActionBusy = false;
      }
    }
  }

  // The Cancel button's click handler - see currentAbortController/
  // currentTurnId's declaration comments above for the mechanism this
  // relies on. Does NOT bump currentTurnId itself: the point is to make
  // the CURRENT turn's own eventual (still in-flight, now-aborted)
  // cleanup a no-op via the myTurnId checks those functions already do,
  // not to start a new turn - the very next translatePrompt()/executeSql()
  // call the user makes does that bump itself, same as any other turn.
  //
  // Best-effort by design (see cancel_registry.py's module docstring on
  // the server side): aborting the client's own fetch() calls is
  // immediate and guaranteed - the UI reset below is NOT waiting on
  // anything server-side to confirm before it happens - but the POST to
  // /api/cancel that asks the server to also abandon whatever it's doing
  // (closing the DB connection or LLM client currently in flight for this
  // session) is fired-and-forgotten (`.catch(() => {})`, never awaited):
  // if it's slow, fails, or the server-side work has no cancel handle
  // registered for some reason (see cancel_registry.py for the rare cases
  // that can happen), the UI still resets immediately and correctly
  // either way - the user is never made to wait on it.
  function cancelInFlightQuery() {
    if (!uiActionBusy) return;

    if (currentAbortController) currentAbortController.abort();

    fetch('/api/cancel', {
      method: 'POST',
      headers: getApiHeaders(),
      credentials: 'same-origin',
    }).catch(() => {});

    // Full synchronous UI reset - everything below happens immediately,
    // without waiting on any network call (the /api/cancel POST above, or
    // whichever fetch(es) currentAbortController.abort() just aborted) to
    // settle first. This is what lets a next query start safely right
    // away: uiActionBusy/the buttons/the results area are all back to
    // their idle state before this function even returns.
    allModeStreamState = null;
    pendingAllModeNotes = null;
    chatStore.clearPending();
    clearResultsDisplay();
    setSqlQuery('');

    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) {
      resultsBody.innerHTML = `
        <tr>
          <td class="error-cell">
            <div class="error-container">
              <span class="error-icon">⏹️</span>
              <div class="error-details">
                <strong>Query cancelled</strong>
                <p>You stopped this query. Feel free to try again.</p>
              </div>
            </div>
          </td>
        </tr>`;
    }

    hideRetryStatus();
    hideAllModeStreamStatus();
    setButtonsDisabled(false);
    uiActionBusy = false;
  }

  // ===========================================================================
  // 10. INPUT WIRING: NL PROMPT BOX, TRANSLATE/EXECUTE BUTTONS
  // ===========================================================================
  if (aiPrompt) {
    aiPrompt.addEventListener('input', () => {
      setSqlQuery('');
      clearResultsDisplay();
    });

    aiPrompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        // Guard against double-submission while a translation is already
        // in flight - every other trigger (the Translate button, the
        // quick-prompt chips) is disabled via setButtonsDisabled(true)
        // for the duration of a call, but Enter bypassed that entirely
        // before this check existed. A second concurrent translatePrompt()
        // call resets the shared allModeStreamState (see its own
        // declaration comment) out from under the first call's still-
        // in-flight "all databases" mode streaming updates, and the two
        // calls' eventual setSqlQuery() results race on the same shared
        // SQL editor - whichever call's network round trip finishes last
        // wins, even if that's the accidental second Enter-press rather
        // than the original request. translateBtn.disabled is the same
        // signal setButtonsDisabled() already maintains for every other
        // entry point, so this just extends it to cover this one too.
        if (translateBtn && translateBtn.disabled) return;
        translatePrompt();
      }
    });
  }

  // ===========================================================================
  // 11. QUICK PROMPTS: DISMISS / RESTORE
  // ===========================================================================
  // Example prompt chips: a permanent "Quick prompts" shortcut row, not
  // onboarding-only - it stays in the UI for every visit until the user
  // explicitly dismisses it via dismissExamplePromptsBtn, at which point
  // that choice is remembered on this browser. Distinct from
  // ONBOARDING_SEEN_KEY, which only tracks whether Help has been opened.
  // Once dismissed, restoreQuickPromptsBtn (shown inside the Help modal -
  // see updateRestoreQuickPromptsVisibility(), called from openHelpModal())
  // is the real UI path back, so nobody has to reach for devtools/localStorage.
  const EXAMPLE_PROMPTS_DISMISSED_KEY = 'ydylQuickPromptsDismissed';
  const examplePrompts = document.getElementById('examplePrompts');
  const dismissExamplePromptsBtn = document.getElementById('dismissExamplePromptsBtn');
  const restoreQuickPromptsBtn = document.getElementById('restoreQuickPromptsBtn');
  function hasQuickPromptsDismissed() {
    try {
      return localStorage.getItem(EXAMPLE_PROMPTS_DISMISSED_KEY) === '1';
    } catch (e) {
      return false; // localStorage unavailable - just leave the row showing
    }
  }
  function dismissQuickPrompts() {
    try {
      localStorage.setItem(EXAMPLE_PROMPTS_DISMISSED_KEY, '1');
    } catch (e) { /* ignore */ }
    if (examplePrompts) examplePrompts.classList.add('hidden');
    updateRestoreQuickPromptsVisibility();
  }
  function restoreQuickPrompts() {
    try {
      localStorage.removeItem(EXAMPLE_PROMPTS_DISMISSED_KEY);
    } catch (e) { /* ignore */ }
    if (examplePrompts) examplePrompts.classList.remove('hidden');
    updateRestoreQuickPromptsVisibility();
  }
  // Keeps the "Show quick prompts again" row (inside the Help modal) in
  // sync with actual dismissed state - only relevant while it's dismissed.
  function updateRestoreQuickPromptsVisibility() {
    if (!restoreQuickPromptsBtn) return;
    restoreQuickPromptsBtn.classList.toggle('hidden', !hasQuickPromptsDismissed());
  }
  if (hasQuickPromptsDismissed() && examplePrompts) {
    examplePrompts.classList.add('hidden');
  }
  if (dismissExamplePromptsBtn) {
    dismissExamplePromptsBtn.addEventListener('click', dismissQuickPrompts);
  }
  if (restoreQuickPromptsBtn) {
    restoreQuickPromptsBtn.addEventListener('click', restoreQuickPrompts);
  }

  if (translateBtn) translateBtn.addEventListener('click', translatePrompt);
  if (runBtn) runBtn.addEventListener('click', () => executeSql());
  if (stopBtn) stopBtn.addEventListener('click', cancelInFlightQuery);

  // Example prompt chips (zero-state guidance for first-time users): fill
  // the NL prompt box with a working example and immediately run it, so
  // someone who has never used the app can see the whole prompt -> SQL ->
  // results flow without having to guess what to type first.
  //
  // Each chip's LABEL is fixed (see index.html), but the PROMPT TEXT it
  // submits depends on the mode: data-prompt-all is used instead of
  // data-prompt whenever "All databases" mode is selected (see
  // isAllConnectionsSelected()) - "all" mode routes the question through
  // a triage step that may pick a different connection than the single-
  // connection wording assumes, so the two need independently editable
  // text (see index.html's data-prompt/data-prompt-all comment for where
  // to change the actual wording). Falls back to data-prompt if a chip
  // has no data-prompt-all set at all, so this never regresses to an
  // empty prompt for a chip that hasn't been given "all"-mode wording.
  const examplePromptButtons = document.querySelectorAll('.example-chip');
  if (examplePromptButtons.length && aiPrompt) {
    examplePromptButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const promptText = isAllConnectionsSelected()
          ? (btn.dataset.promptAll || btn.dataset.prompt || '')
          : (btn.dataset.prompt || '');
        trackEvent('quick_prompt_clicked', {
          chip_label: (btn.textContent || '').trim(),
          prompt: truncateForAnalytics(promptText),
        });
        aiPrompt.value = promptText;
        // Setting .value directly doesn't fire the 'input' event, so the
        // listener above (which clears stale SQL as the user types) never
        // runs here - clear it explicitly so a chip click doesn't leave
        // a previous prompt's SQL sitting in the editor.
        setSqlQuery('');
        translatePrompt();
      });
    });
  }

  // ===========================================================================
  // 12. HISTORY NAVIGATION (back/forward through turns), PURGE, FINAL INIT
  // ===========================================================================
  function restoreLatestTurn() {
    const turn = chatStore.lastTurn();
    if (turn) {
      const { userEntry: lastUserEntry, modelEntry: lastModelEntry } = turn;

      if (aiPrompt) {
        aiPrompt.value = (lastUserEntry && lastUserEntry.text !== "[Direct SQL Execution]") ? lastUserEntry.text : '';
      }

      if (lastModelEntry && lastModelEntry.allMode) {
        // "All databases" mode turn (see translatePrompt()'s router_route
        // branch / executeSql()'s captureAllModeHistory() calls) - rebuild
        // the exact same combined Summary/Note/result/failure tabs instead
        // of falling into the plain per-statement branch below, which has
        // no idea what any of those extra tab kinds even are. Always
        // treated as fully "done" (never re-enters the pending/"awaiting
        // first execution" state below) - an all-mode turn is only ever
        // recorded here once every selected database has already either
        // returned real SQL and been executed, or noted/failed outright.
        chatStore.clearPending();
        // modelEntry.text is the real SQL to show in the editor - except
        // when captureAllModeHistory() had to invent a "*** NO SQL ***"
        // placeholder (every database noted/failed, nothing was ever
        // executed) purely so this turn wouldn't vanish from the LLM's
        // history - that placeholder was never meant for the SQL editor.
        const isPlaceholderText = lastModelEntry.text && lastModelEntry.text.startsWith('*** NO SQL ***');
        setSqlQuery(isPlaceholderText ? '' : (lastModelEntry.text || ''));
        renderAllModeCombinedResults({
          notes: {
            routingMessage: lastModelEntry.allMode.routingMessage,
            databaseNotes: lastModelEntry.allMode.databaseNotes,
            generationFailures: lastModelEntry.allMode.generationFailures,
          },
          executeResults: lastModelEntry.results || [],
          executeFailures: lastModelEntry.allMode.executeFailures || [],
        });
        return;
      }

      if (lastModelEntry && lastModelEntry.text) {
        const sqlText = lastModelEntry.text;
        const isNoSql = sqlText.startsWith('*** NO SQL ***');
        
        if (isNoSql) {
          setSqlQuery('');
          chatStore.clearPending();
          // Reached for a saved all-mode "answer" outcome turn too (it
          // never got `.allMode` set - see the `lastModelEntry.allMode`
          // branch above, which only covers "route" outcome turns) -
          // same IN_SCOPE_MODE-based distinction translatePrompt()'s own
          // renderNoSqlResponse() call makes, and equally a heuristic
          // here: a turn recorded under a mode the user has since
          // switched away from would guess wrong, a pre-existing class of
          // minor cosmetic edge case this history-restoration code
          // already accepts elsewhere.
          renderNoSqlResponse(sqlText, { hasLabel: IN_SCOPE_MODE === 'all' });
        } else {
          setSqlQuery(sqlText);

          const alreadyExecuted = lastModelEntry.results && Array.isArray(lastModelEntry.results);
          if (alreadyExecuted) {
            // This turn is done - viewing it again must never let a
            // subsequent Run overwrite its stored results in place.
            chatStore.clearPending();
            renderMultiTurnResults(lastModelEntry.results);
          } else {
            // Genuinely still awaiting its first execution.
            chatStore.setPending(lastModelEntry, normalizeSqlForCompare(sqlText));
            clearResultsDisplay();
          }
        }
      } else {
        setSqlQuery('');
        clearResultsDisplay();
      }
    } else {
      if (aiPrompt) aiPrompt.value = '';
      setSqlQuery('');
      chatStore.clearPending();
      clearResultsDisplay();
    }
  }

  if (goBackBtn) {
    goBackBtn.addEventListener('click', () => {
      if (chatStore.undo()) {
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
        trackEvent('history_nav_clicked', { turn_offset: chatStore.turnOffset() });
      }
    });
  }

  if (goForwardBtn) {
    goForwardBtn.addEventListener('click', () => {
      if (chatStore.redo()) {
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
        trackEvent('history_nav_clicked', { turn_offset: chatStore.turnOffset() });
      }
    });
  }

  if (purgeHistoryBtn) {
    purgeHistoryBtn.addEventListener('click', async () => {
      const confirmed = await showConfirmDialog('Are you sure you want to purge history records within the current scope? This action cannot be undone.');
      if (!confirmed) {
        return;
      }
      const msgEl = document.getElementById('historyActionMsg');
      try {
        const response = await fetch('/api/history/purge', {
          method: 'DELETE',
          headers: getApiHeaders(),
          credentials: 'same-origin'
        });
        const data = await response.json();
        if (response.ok && data.success) {
          if (msgEl) {
            msgEl.textContent = 'Purged successfully.';
            msgEl.style.color = 'var(--primary, #10b981)';
          }
          await loadHistoryData();
        } else {
          const errMsg = response.status === 401 
            ? "Authentication required." 
            : (data.error || "Failed to purge history.");
          if (msgEl) {
            msgEl.textContent = errMsg;
            msgEl.style.color = 'var(--danger, #f87171)';
          }
        }
      } catch (err) {
        console.error("Failed to purge history:", err);
        if (msgEl) {
          msgEl.textContent = 'Network error purging history';
          msgEl.style.color = 'var(--danger, #f87171)';
        }
      }
    });
  }

  await fetchBackendConfig();

  // Brand-new session, nobody's told it what to do yet: walk them through
  // the UI with a short guided tour (prompt box -> SQL/Execute -> results ->
  // DB config -> history -> help). Every later visit (once ONBOARDING_SEEN_KEY
  // is set) leaves this alone.
  if (!hasSeenOnboarding()) {
    startGuidedTour();
    markOnboardingSeen();
  }

  if (aiPrompt) aiPrompt.focus();
});