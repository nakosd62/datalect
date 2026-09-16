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
  function createChatHistoryStore(maxTurns, onPersist) {
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
      // `onPersist`, when given (see getOrCreateBucketStore()), is called
      // with the resulting (already-trimmed) `history` array right after -
      // this is the ONLY thing that triggers a server-side save of this
      // bucket (see persistChatBucket()); undo()/redo()/setPending() are
      // deliberately NOT persisted - see hydrate()'s own docstring for why
      // that's fine.
      pushTurn(userText, modelEntry) {
        history.push({ role: 'user', text: userText });
        history.push(modelEntry);
        history = history.slice(-maxEntries);
        future = [];
        if (onPersist) onPersist(history);
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
      // Re-persists `history` as it stands RIGHT NOW - for every call site
      // that fills in a turn's results/summary/allMode data AFTER it was
      // already pushed (chatStore.getPending()/isPendingCurrent()'s whole
      // reason for existing: SQL is pushed as its own turn the moment
      // translate() returns, then executeSql() mutates that SAME
      // modelEntry object in place once execution finishes, rather than
      // creating a second turn - see setPending()'s own comment). pushTurn()
      // above is the ONLY thing that calls onPersist, and only at the
      // moment a turn is first pushed - a later in-place mutation of an
      // already-pushed entry (pending.entry.results = ...,
      // captureAllModeHistory(pending.entry, ...), etc.) changes this
      // store's own in-memory `history` array immediately (same object
      // reference), but the copy already sitting in the server's chat
      // history table stays exactly as it was at push time - missing
      // results entirely - until something calls this to send the
      // corrected array back. Previously nothing did, so a turn's results
      // only ever reached the server by accident, if some LATER turn's own
      // pushTurn() happened to re-save the whole (by-then-mutated) history
      // array first - meaning after a page reload, only turns with a
      // later sibling turn showed their results; the newest turn in any
      // bucket always came back with none. Every call site that mutates an
      // already-pushed entry must call this afterward.
      persistCurrent() { if (onPersist) onPersist(history); },
      // What gets sent to /api/translate as `history`.
      toPayload() { return history; },
      // Replaces this store's history wholesale with a previously-
      // persisted turn list (server restore - see
      // hydrateChatHistoryFromServer()), trimmed to the current cap same
      // as pushTurn() does. Redo stack and any "pending" (SQL generated,
      // not yet executed) pointer are reset rather than restored - neither
      // is part of what pushTurn() ever persists (only `history` is, see
      // pushTurn()'s own onPersist call), so there's nothing saved to
      // bring back for them; a turn that was mid-flight (pending) at the
      // moment this browser last closed simply reads back as a normal,
      // already-settled turn. Deliberately does NOT call onPersist itself -
      // this is populating FROM the server, not a new client-side change
      // that needs saving back to it.
      hydrate(turns) {
        history = Array.isArray(turns) ? turns.slice(-maxEntries) : [];
        future = [];
        pending = null;
      },
    };
  }

  // Same default as the server's HISTORY_MAX_TURNS (translate_routes.py) -
  // just a fallback until the first fetchBackendConfig() call reconciles
  // it via chatStore.setMaxTurns(), see createChatHistoryStore() above.
  const FALLBACK_HISTORY_TURNS = 10;
  // Bootstrap value only - reconcileActiveHistoryBucket() below replaces
  // this with the real bucket for the current identity/connection the
  // moment the first /api/config response is in, so nothing meaningful
  // can ever actually accumulate in this particular instance (nothing in
  // this file calls chatStore.pushTurn() before that first fetch resolves
  // and the UI finishes wiring up). `let`, not `const`, precisely because
  // reconcileActiveHistoryBucket() reassigns it - every closure elsewhere
  // in this file that reads `chatStore` does so BY NAME at call time, not
  // by a value captured when it was defined, so they all transparently
  // follow along to whichever bucket is currently active.
  let chatStore = createChatHistoryStore(FALLBACK_HISTORY_TURNS);

  // --- Per-bucket history registry ---------------------------------------
  //
  // One conversation used to mean one chatStore, full stop - switching the
  // active DB connection (or logging in/out) reset it via
  // clearActiveQueryState() below, on the theory that "the conversation"
  // and "the connection" were the same thing. They're not: asking three
  // follow-up questions about the Sales database, checking Marketing for a
  // minute, then coming back to Sales should mean picking the Sales
  // conversation back up, not starting over - and the same identity (this
  // browser's session, or this signed-in user) can reasonably be running a
  // handful of separate conversations at once, one per connection, plus
  // one more for "all databases" mode.
  //
  // So instead of one chatStore, this is a registry of them, keyed by
  // (identity, connection-or-"all") - see computeBucketKey(). Switching to
  // a bucket that already exists (same identity, same connection) picks
  // its chatStore back up exactly where it was left, pending SQL included;
  // switching to one never visited this page-load creates it fresh, same
  // as chatStore always started out. This registry itself is still plain
  // in-memory JS state (a Map, same as chatStore itself always was) - but
  // each bucket it holds is now backed by the server (see
  // getOrCreateBucketStore()'s onPersist callback and
  // hydrateChatHistoryFromServer() below), so a page reload or a server
  // restart no longer starts every bucket over from empty the way it used
  // to. Login/logout/connection-switch still never wipe it out from under
  // you - that was always the point of this registry existing at all.
  const chatStoresByBucket = new Map();
  let activeBucketKey = null;
  // Guards hydrateChatHistoryFromServer() (see its own docstring) so this
  // page-load only ever fetches a given identity's persisted buckets once -
  // fetchBackendConfig() is called far more often (after every save,
  // connection switch, translate/execute) than identity actually changes.
  let chatHistoryHydratedForIdentity = null;
  // True while the prompt/SQL/results area is showing #newTurnBtn's blank
  // slate (see startNewTurn()) rather than any real turn from chatStore -
  // a purely visual "detached from history" position, never itself pushed
  // into chatStore. Exists so #goBackBtn can tell the difference between
  // "step back past the turn already on screen" (the normal chatStore.undo()
  // case) and "reveal the turn I just blanked out, without consuming it"
  // (this flag's whole reason for existing) - without it, pressing back
  // right after #newTurnBtn would call chatStore.undo() against the actual
  // last turn (still sitting untouched at the top of history, since
  // startNewTurn() never pops it) and skip straight past it to the turn
  // BEFORE that. Cleared by pushActiveTurn() below the moment a real new
  // turn actually lands, and by #goBackBtn's own handler once it's used to
  // reveal that real last turn.
  let viewingBlankSlate = false;
  // Mirrors whatever the last real /api/config response's history_max_turns
  // said (see fetchBackendConfig()'s own chatStore.setMaxTurns() call) - so
  // a bucket created well after startup (the first time a given connection
  // is ever visited this page-load) starts with the right cap immediately
  // instead of FALLBACK_HISTORY_TURNS.
  let currentHistoryMaxTurns = FALLBACK_HISTORY_TURNS;
  // The resolved identity this browser is currently making requests as -
  // mirrors auth.py's get_current_user_identity() exactly ("global" when
  // running locally with no auth, "anonymous:<session_id>" for an
  // unauthenticated Cloud Run visitor, the real email once signed in),
  // sourced verbatim from /api/config's own `user_id` field (see
  // fetchBackendConfig()) rather than re-derived here - the server is the
  // one source of truth for what identity a request resolves to. Used
  // ONLY to key chatStoresByBucket; nothing else in this file needs it.
  let CURRENT_USER_IDENTITY = 'global';

  // A single connection's own stable identity for bucketing purposes -
  // "preset:<id>"/"custom:<key>", matching the exact {kind, id} pair
  // resolve_descriptor_by_reference uses server-side, and therefore the
  // exact same pair every all-mode fan-out entry is tagged with (see
  // captureAllModeHistory's databaseSql/notes.connectionPrompts entries,
  // each {kind, id, ...}). Used by computeBucketKey() below for the
  // currently-active single connection, and by pushTurnIntoBucket() below
  // for fanning an all-mode turn out into each of ITS in-scope databases'
  // own buckets - the whole point of Chunk 4 (see that function's own
  // docstring): a database reached either way now lands in the identical
  // bucket, so switching to it directly in single-connection mode picks up
  // history recorded on its behalf while chatting in "all databases" mode,
  // and vice versa.
  function connectionBucketKey(kind, id) {
    return `${kind}:${id}`;
  }

  // The "connection" half of a bucket key - see computeBucketKey() below,
  // which combines this with the current identity. Split out on its own
  // so callers that need to talk to the server about ONE SPECIFIC bucket
  // (persistChatBucket()/persistActiveChatBucket() - see their own
  // docstrings) can send this value alone: the server's chat_history table
  // already partitions by user_id as its own column (see state_store.py),
  // so re-embedding the identity inside this string would just be
  // duplicated information.
  //
  // "All databases" mode is ONE shared conversation regardless of which
  // specific presets/custom connections are currently checked into scope -
  // checking one more database in or out mid-conversation changes who
  // might answer the NEXT question, not which conversation this is.
  // Everything else (a single active connection) is now identified by its
  // own stable (kind, id) pair - see connectionBucketKey's own docstring
  // for why this replaced the old url|is_custom|customKey|presetId tuple:
  // that tuple went stale whenever ACTIVE_DB_URL wasn't reset on a preset
  // switch (see triggerConfigSave()'s own fix earlier this session) and,
  // more fundamentally, could never match the {kind, id} pair an all-mode
  // fan-out entry for the SAME database is tagged with, since a URL alone
  // says nothing about which specific preset/custom connection that URL
  // belongs to. A saved custom connection is identified by its own
  // connection_key, same as resolve_descriptor_by_reference's own "custom"
  // branch; an UNSAVED ad hoc custom URL (typed directly, never given a
  // name/saved - see ACTIVE_CUSTOM_CONNECTION_KEY's own declaration
  // comment) has no connection_key or other server-side identity at all,
  // so this falls back to the raw URL for that one case, same as every
  // bucket key did before this refactor - all-mode's own fan-out never
  // visits an unsaved connection in the first place
  // (resolve_in_scope_descriptors only ever resolves saved presets/custom
  // connections), so there's no fan-out entry this fallback could ever
  // fail to match anyway.
  function computeBucketConnectionSuffix() {
    if (IN_SCOPE_MODE === 'all') {
      return 'all';
    } else if (ACTIVE_IS_CUSTOM) {
      return ACTIVE_CUSTOM_CONNECTION_KEY
        ? connectionBucketKey('custom', ACTIVE_CUSTOM_CONNECTION_KEY)
        : `custom-adhoc:${ACTIVE_DB_URL}`;
    } else {
      return connectionBucketKey('preset', ACTIVE_PRESET_ID);
    }
  }

  // What actually identifies "a conversation" for bucketing purposes -
  // called after anything that could change the answer (see
  // reconcileActiveHistoryBucket()'s own call sites: fetchBackendConfig()
  // for identity changes, triggerConfigSave() for connection/in-scope-mode
  // changes).
  function computeBucketKey() {
    const identity = CURRENT_USER_IDENTITY || 'global';
    return `${identity}::${computeBucketConnectionSuffix()}`;
  }

  // Finds (or creates, starting empty) the chat history store for `key` -
  // shared by reconcileActiveHistoryBucket() below (switching the
  // CURRENTLY ACTIVE bucket), pushTurnIntoBucket() further down (an
  // all-mode turn's own per-database fan-out, appending to a bucket that
  // may or may not be the active one), and hydrateChatHistoryFromServer()
  // (populating a bucket restored from the server) - so bucket-creation is
  // written in exactly one place for all three. `bucketKeySuffix` is
  // `key`'s own connection-only half (see computeBucketConnectionSuffix())
  // - threaded through separately, rather than re-derived by splitting
  // `key` back apart, so a freshly-created store's onPersist callback
  // always sends the server exactly the same value this file uses
  // everywhere else to name this bucket.
  function getOrCreateBucketStore(key, bucketKeySuffix) {
    let store = chatStoresByBucket.get(key);
    if (!store) {
      store = createChatHistoryStore(currentHistoryMaxTurns, (turns) => persistChatBucket(bucketKeySuffix, turns));
      chatStoresByBucket.set(key, store);
    }
    return store;
  }

  // Switches `chatStore` to whichever bucket computeBucketKey() currently
  // names, creating it fresh the first time this page-load visits it. A
  // no-op whenever the key hasn't actually changed - this runs after
  // EVERY config fetch/save, not just ones that changed anything relevant
  // to bucketing, so that has to be cheap and is: no more hand-rolled
  // "did the connection actually change" comparison at each call site the
  // way this used to work (see the old previousConnectionIdentity/
  // nextConnectionIdentity check this replaced in triggerConfigSave) -
  // just compare the one computed key.
  function reconcileActiveHistoryBucket() {
    const suffix = computeBucketConnectionSuffix();
    const key = `${CURRENT_USER_IDENTITY || 'global'}::${suffix}`;
    if (key === activeBucketKey) return;
    activeBucketKey = key;
    chatStore = getOrCreateBucketStore(key, suffix);
    // Switching buckets always lands on a real turn (or a genuinely empty
    // bucket - restoreLatestTurn() below handles both) via restoreLatestTurn(),
    // never on the OLD bucket's own #newTurnBtn blank slate - leaving this
    // true here would misapply that bucket's back/forward rules to a
    // completely different, unrelated bucket (see viewingBlankSlate's own
    // docstring).
    viewingBlankSlate = false;
    // restoreLatestTurn() (defined far below, in section 12 - a plain
    // function declaration, so it's already hoisted and callable from up
    // here) already does exactly the right thing for both an empty bucket
    // (blanks the prompt/SQL/results, same as this used to do
    // unconditionally via clearActiveQueryState()) and a previously-
    // visited one (re-shows its last turn's SQL/results) - "finding the
    // history you left", not just making it reachable via the back arrow.
    // This now applies just as well to a bucket that's "previously
    // visited" only because hydrateChatHistoryFromServer() restored it
    // from an earlier session, not just one touched already this page-load.
    restoreLatestTurn();
    updateHistoryTurnsSubtitle();
    // Best-effort restart-time hint (see persistActiveChatBucket()'s own
    // docstring) - not load-bearing for correctness, since a restart
    // primarily finds its way back to the right bucket by recomputing this
    // same suffix from the user's separately-persisted connection/in-scope
    // selection.
    persistActiveChatBucket(suffix);
  }

  // Appends one (user, model) turn directly into a SPECIFIC database's own
  // bucket - identity + that database's own connectionBucketKey(kind, id) -
  // WITHOUT switching `chatStore`/`activeBucketKey` to it and without any
  // re-render of any kind, even if this happens to be the bucket currently
  // shown on screen (see fanOutAllModeHistoryPerDatabase's own docstring
  // for why that's the deliberate, "never disturb the active view" design
  // for this feature - the turn is simply there, waiting, the next time
  // the user navigates that bucket's own history). This bucket's own
  // pushTurn() still persists it server-side exactly the same way the
  // active bucket's does (see getOrCreateBucketStore()'s onPersist) - only
  // the ACTIVE-bucket pointer is left untouched here.
  function pushTurnIntoBucket(kind, id, userText, modelEntry) {
    const identity = CURRENT_USER_IDENTITY || 'global';
    const suffix = connectionBucketKey(kind, id);
    const key = `${identity}::${suffix}`;
    getOrCreateBucketStore(key, suffix).pushTurn(userText, modelEntry);
  }

  // One-time-per-identity restore of every persisted conversation bucket
  // (see the "Per-bucket history registry" section above) from the
  // server's chat_history table/collection - called from
  // fetchBackendConfig() itself, right before reconcileActiveHistoryBucket()
  // runs, so a fresh page load (or a login/logout that changes identity)
  // finds every bucket already populated instead of
  // reconcileActiveHistoryBucket() switching to, and rendering, an empty
  // one first. Guarded by chatHistoryHydratedForIdentity so this never
  // re-fetches for an identity already hydrated this page-load - every
  // OTHER fetchBackendConfig() call (after a save, a connection switch, a
  // translate/execute) vastly outnumbers actual identity changes. Never
  // overwrites a bucket already present in chatStoresByBucket - defensive
  // only; in practice nothing this page-load could have created one before
  // this ever runs for a identity it hasn't seen yet.
  async function hydrateChatHistoryFromServer() {
    try {
      const response = await fetch('/api/chat-history', { headers: getApiHeaders(), credentials: 'same-origin' });
      const data = await response.json();
      if (!data || !data.success) return;
      const identity = CURRENT_USER_IDENTITY || 'global';
      for (const [bucketKeySuffix, turns] of Object.entries(data.buckets || {})) {
        const fullKey = `${identity}::${bucketKeySuffix}`;
        if (chatStoresByBucket.has(fullKey)) continue;
        getOrCreateBucketStore(fullKey, bucketKeySuffix).hydrate(Array.isArray(turns) ? turns : []);
      }
    } catch (err) {
      console.error('Failed to load persisted chat history:', err);
    }
  }

  // Fire-and-forget persistence for one bucket's full turn list - passed
  // as onPersist to every createChatHistoryStore() call (see
  // getOrCreateBucketStore()), so it runs for whichever bucket just
  // received a turn: the currently active one via chatStore.pushTurn(), or
  // a DIFFERENT database's own bucket via pushTurnIntoBucket()'s all-mode
  // fan-out. Best-effort: a failed save here never blocks or surfaces an
  // error to the user mid-conversation - this page's own in-memory bucket
  // is unaffected either way; the only risk is this turn not being there
  // on some FUTURE restart.
  function persistChatBucket(bucketKeySuffix, turns) {
    fetch('/api/chat-history/save', {
      method: 'POST',
      headers: getApiHeaders(),
      credentials: 'same-origin',
      body: JSON.stringify({ bucket_key: bucketKeySuffix, turns }),
    }).catch((err) => console.error('Failed to persist chat history:', err));
  }

  // Fire-and-forget - records which bucket is "active" server-side, purely
  // as a restart-time hint (see get_chat_history's own docstring in
  // state_store.py) - the bucket a restart ACTUALLY reopens on is whichever
  // one computeBucketKey() recomputes from the user's separately-persisted
  // connection/in-scope-mode selection, which already lands back on the
  // same bucket in the common case.
  function persistActiveChatBucket(bucketKeySuffix) {
    fetch('/api/chat-history/activate', {
      method: 'POST',
      headers: getApiHeaders(),
      credentials: 'same-origin',
      body: JSON.stringify({ bucket_key: bucketKeySuffix }),
    }).catch((err) => console.error('Failed to persist active chat bucket:', err));
  }

  // Chunk 5 of "splitting SQL/summary per in-scope database" (see
  // captureAllModeHistory()/fanOutAllModeHistoryPerDatabase()'s own
  // docstrings for the earlier chunks): builds the per-connection history
  // payload an "all databases" mode /api/translate request sends alongside
  // its own shared `history` field, so Phase B's per-connection SQL-
  // generation call for a given database can be fed THAT SAME database's
  // own FULLY MERGED history - single-connection-mode turns and every
  // prior all-mode turn already fanned out to it, indistinguishably (see
  // connectionBucketKey()'s own docstring for why the two are now one and
  // the same bucket) - instead of no history at all.
  //
  // One entry per connection "all databases" mode could ever actually
  // route Phase B to - EVERY currently configured preset (CONFIGURED_DBS)
  // plus every one of this user's own SAVED custom connections (a row
  // with a real `connection_key` - see ACTIVE_CUSTOM_CONNECTION_KEY's own
  // declaration comment for what distinguishes a saved connection from an
  // unsaved ad hoc one). Deliberately NOT IN_SCOPE_PRESET_IDS/
  // IN_SCOPE_CUSTOM_KEYS - those are the explicit-list arrays "single"
  // mode's own scope uses, but "all" mode's real routing candidate pool
  // ignores them entirely in favor of every configured/saved connection
  // (see db.py's resolve_in_scope_descriptors/_resolve_all_configured_
  // descriptors docstrings) - those two arrays can also simply be stale
  // leftovers from the last time this session was in "single" mode (see
  // config_routes.py's "'all' mode ignores them, leaves the existing
  // scope alone" behavior), so filtering by them here would silently
  // starve Phase B of history for a database "all" mode can plainly still
  // reach. Keyed by the exact same "preset:<id>"/"custom:<key>" string
  // connectionBucketKey() builds, so translate_routes.py's
  // stream_translation() can look each one up by `f"{kind}:{id}"` with
  // zero string-format guessing on the server side.
  //
  // Read-only against chatStoresByBucket - deliberately does NOT call
  // getOrCreateBucketStore() - a connection with no bucket yet (never
  // visited, directly or via fan-out) simply contributes no key at all,
  // rather than a request-build side effect creating an empty bucket
  // nothing will ever populate. Built fresh on every all-mode request
  // (see translatePrompt()'s own call site) rather than kept as standing
  // state, since which connections are even configured/saved can change
  // between turns.
  function buildInScopeConnectionHistories() {
    const identity = CURRENT_USER_IDENTITY || 'global';
    const out = {};
    (CONFIGURED_DBS || []).forEach((db) => {
      const bucketKeySuffix = connectionBucketKey('preset', db.id);
      const store = chatStoresByBucket.get(`${identity}::${bucketKeySuffix}`);
      if (store) out[bucketKeySuffix] = store.toPayload();
    });
    (customDatabases || []).forEach((db) => {
      if (!db.connection_key) return; // unsaved ad hoc row - never part of "all" mode's real candidate pool
      const bucketKeySuffix = connectionBucketKey('custom', db.connection_key);
      const store = chatStoresByBucket.get(`${identity}::${bucketKeySuffix}`);
      if (store) out[bucketKeySuffix] = store.toPayload();
    });
    return out;
  }

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
  // re-deciding from scratch.
  //
  // Deliberately NOT part of the per-bucket history registry above (see
  // reconcileActiveHistoryBucket()) - it stays this one single, currently-
  // active-view variable, reset only when a pinned connection is actually
  // unchecked from scope (triggerConfigSave()'s in-scope-set branch) the
  // same way it always has been. In practice that means switching away
  // from the "all databases" bucket and back no longer clears a pin the
  // way it used to (clearActiveQueryState() isn't called on a bucket
  // switch any more) - a small, deliberate inconsistency with this
  // variable's own "never outlives the conversation it was set for" framing
  // above, accepted for now rather than making pins part of the bucket
  // registry too (a bigger change than asked for).
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
  // Persists a signed-in user's Google ID token across reloads AND across
  // every other tab/window open on this same browser - see
  // persistGoogleIdToken()/clearPersistedGoogleIdToken() below (the only two
  // places that ever write this key) and the restore right below this
  // declaration (the only place that ever reads it). Before this,
  // googleIdToken was populated ONLY by the Google Sign-In callback (see
  // renderAuthUI() below), which never fires again on its own after a
  // reload - so every reload silently dropped back to signed-out/anonymous
  // until the user clicked "Sign in" again, even though their actual
  // Google session (and this token, until it expires) was still perfectly
  // valid. localStorage (not sessionStorage) deliberately: signing in on one
  // tab should make every other open tab of the same browser show signed-in
  // too, rather than each tab tracking its own independent sign-in state -
  // localStorage is shared across all tabs/windows of the same origin,
  // whereas sessionStorage is scoped to just the one tab that wrote it. A
  // real Google ID token is still short-lived (about an hour) regardless of
  // where it's stored, and renderAuthUI()'s existing isExpired check already
  // discards/clears anything stale the moment it's actually used, so
  // restoring the raw stored value here unconditionally (without
  // re-checking `exp` a second time) remains safe. Note that storing the
  // token here doesn't by itself keep every open tab's UI in sync the
  // instant another tab signs in or out - each tab still only re-reads this
  // key on its own load/reload - but it does mean any tab that reloads (or
  // is opened fresh) after a sign-in elsewhere picks up the signed-in state
  // immediately, instead of requiring a fresh sign-in in every tab.
  const GOOGLE_ID_TOKEN_STORAGE_KEY = 'datalectGoogleIdToken';
  let googleIdToken = null;
  try {
    googleIdToken = window.localStorage.getItem(GOOGLE_ID_TOKEN_STORAGE_KEY) || null;
  } catch (e) {
    // localStorage unavailable (private browsing, disabled storage, etc.) -
    // same fallback posture as every other storage read in this file: just
    // start signed out, exactly like every reload did before this fix.
  }

  // The server's own last-known answer to "is this browser actually still
  // signed in", straight from /api/config's authenticated/user_id fields
  // (see fetchBackendConfig()) - NOT re-derived from googleIdToken's own
  // parsed `exp` claim. The two can genuinely disagree now: the server's
  // own long-lived session cookie (see auth.py's
  // refresh_auth_session_cookie()/auth_session.py) can keep resolving this
  // browser to a real signed-in identity for up to 30 days, well past the
  // ~1hr the local Google ID token itself is valid for - without this,
  // renderAuthUI() would flip back to showing a "Sign in" button the
  // moment the LOCAL token's exp passed, even though every other request
  // on the page is still succeeding as that same signed-in user. Reset to
  // null on logout (see handleLogout()) so a stale value can't survive a
  // sign-out and briefly redisplay the old identity before the next
  // fetchBackendConfig() call lands.
  let serverConfirmedAuthEmail = null;

  function persistGoogleIdToken(token) {
    try {
      window.localStorage.setItem(GOOGLE_ID_TOKEN_STORAGE_KEY, token);
    } catch (e) {
      // Storage unavailable - the sign-in still works for this page view,
      // it just won't survive a reload or show up in other tabs.
    }
  }

  function clearPersistedGoogleIdToken() {
    try {
      window.localStorage.removeItem(GOOGLE_ID_TOKEN_STORAGE_KEY);
    } catch (e) {
      // See persistGoogleIdToken()'s identical guard above.
    }
  }

  let customDbUrl = "";
  let customDbName = "";
  let customDatabases = [];
  // Which saved custom connections currently have a "Refresh Schema"
  // request in flight - keyed by connection_key (stable across a row's
  // array index shifting from an add/remove elsewhere), not by button/DOM
  // reference, specifically so renderCustomDbRows() can look this up fresh
  // on every re-render (see its own refresh-button-rendering comment) and
  // handleRefreshSchemaClick (below) can guard against firing a second,
  // fully concurrent request for a connection that's already mid-refresh.
  let refreshingConnectionKeys = new Set();
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

  // In-browser column sort for the currently-rendered results table (see
  // renderTableResult()'s sortable-header wiring and handleSortableColumn
  // Click() below) - purely client-side, re-sorting the already-fetched
  // `result.rows` array in place in the DOM; no server call, and nothing
  // written to disk/localStorage/history, so it's intentionally NOT kept
  // anywhere `result` itself is persisted (chatStore, etc.). Tracks which
  // result object (by reference) and column index is currently sorted, plus
  // the direction, so a second click on the SAME header toggles instead of
  // re-applying the type default. Reset to null at the top of every
  // renderTableResult() call (same "reset first" posture as the other resets
  // there), so switching tabs or running a new query always starts unsorted
  // - only repeated clicks on one already-rendered table's own header
  // accumulate toggling state.
  let currentTableSortState = null;

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
  // readNdjsonStream() (no ReadableStream support), or a test double
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
  // history_viewed, history_nav_clicked, history_purge_clicked,
  // preferences_viewed, login, logout, mic_used, quick_prompt_clicked,
  // tour_exited. Custom, app-specific names
  // throughout (not GA4's own recommended-event vocabulary) - per explicit
  // request. Deliberately kept to this small, fixed set of names, even
  // where a new distinction was worth adding (see trackAllModeFanoutTranslate()/
  // trackAllModeFanoutExecute() below) - one more differently-named event
  // is one more row for anyone building a GA4 report/dashboard to know
  // about, so a new *reason* to fire an existing event reuses its name
  // rather than inventing another.
  //
  // "All databases" mode can fan a single user action out into several
  // REAL per-database requests server/client-side - one LLM translate call
  // per selected connection (translate_routes.py's _run_phase_b_fanout),
  // and, with auto-execute on, one /api/execute call per connection too
  // (client.js's executeOneAllModeConnection()/handlePhaseBConnectionDone()
  // below) - so trackAllModeFanoutTranslate()/trackAllModeFanoutExecute()
  // fire translate_submitted/sql_executed AGAIN, once per connection, right
  // when THAT connection's own translate/execute request is actually
  // dispatched (or, for translate, the moment the client learns the
  // fan-out is starting - see trackAllModeFanoutTranslate()'s own comment).
  // This is ADDITIVE to the existing once-per-user-action call each of
  // these already had (one NL prompt submission, one Execute click/
  // auto-execute) - both still fire under the same name, so a turn against
  // 3 in-scope databases shows up as 4 total translate_submitted events
  // (1 generic "the prompt was submitted" + 3 real per-database calls),
  // not a brand-new event name to track separately. Distinguishable within
  // GA4 by `database_name`/`database_type`: the once-per-action call's
  // database_name is the generic "All databases" badge text (or blank),
  // while each fan-out call's is that one specific database's own name.
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

  // Fires 'error_shown'/"Database Connection" - shared by every place that
  // flips connDbDot to 'status-dot disconnected': checkDbStatus()'s own
  // /api/ping check (both its non-throwing failure and its network-
  // exception catch), fetchBackendConfig()'s catch (the initial/periodic
  // config fetch itself failing), and triggerConfigSave()'s catch (a
  // network exception while saving a DB connection). The dot itself still
  // only ever shows connected/disconnected in the UI - no message is ever
  // rendered for it anywhere - but every time it actually goes down, that's
  // a real error the user is experiencing (their selected database is
  // unreachable), so it's tracked the same way translation/execution
  // failures are - see this section's ANALYTICS comment above. `message`
  // may legitimately be empty (nothing more specific than "it failed" was
  // available) - truncateForAnalytics() already handles that fine.
  function trackDbConnectionError(message) {
    trackEvent('error_shown', {
      category: 'Database Connection',
      database_name: connDbName ? connDbName.textContent : '',
      database_type: getActiveDatabaseType(),
      message: truncateForAnalytics(message || ''),
    });
  }

  // Fires 'translate_submitted' AGAIN, once per connection in
  // `connectionSelection` (the same {kind,id,name,type,prompt} list
  // translate_routes.py's "phase_a_route" event and, for the rare
  // no-live-stream fallback, its terminal line's own `connection_selection`
  // both carry - see that field's own comment in translate_routes.py for
  // the `type` addition this relies on) - additive to, not instead of,
  // translatePrompt()'s own single top-level call for the whole prompt
  // (see this section's header comment above for the full reasoning and
  // the resulting per-turn event count). Called the moment the client
  // learns Phase B's fan-out is happening at all - for the live-streaming
  // path (startAllModeStreaming() below) that's as soon as "phase_a_route"
  // arrives, which is BEFORE any individual connection's own generation
  // call has actually finished, but the fan-out itself (translate_routes.py's
  // _run_phase_b_fanout ThreadPoolExecutor submission) has already started
  // server-side by the time that line is even written - so, same
  // "submission, not completion" semantics the top-level call already
  // uses, just one level down: one event per REAL translate call this
  // turn is about to make. `mode: 'all'` is hardcoded (never 'single') -
  // this only ever fires for "all databases" mode's own fan-out.
  function trackAllModeFanoutTranslate(connectionSelection) {
    (connectionSelection || []).forEach((entry) => {
      trackEvent('translate_submitted', {
        mode: 'all',
        database_name: entry.name || '',
        database_type: entry.type || '',
        provider: ACTIVE_LLM_PROVIDER || '',
        model: ACTIVE_LLM_MODEL || '',
      });
    });
  }

  // Fires 'sql_executed' AGAIN, for a single connection about to have (or
  // already having had - see call sites below) its own SQL sent to
  // /api/execute as part of "all databases" mode's fan-out - additive to,
  // not instead of, executeSql()'s own single top-level call per Execute
  // click/auto-execute (see this section's header comment above). `database`
  // needs at least {name} and ideally {type}; call sites pass whatever they
  // already have on hand (executeOneAllModeConnection()'s own `evt` carries
  // `type` directly - see phase_b_connection_done's own comment in
  // translate_routes.py - while the batched manual-Execute-click path looks
  // it up from allModeStreamState.connectionOrder/
  // pendingAllModeNotes.connectionPrompts, since execute_routes.py's own
  // result/failure `.database` tags carry no dialect at all).
  function trackAllModeFanoutExecute(database, trigger) {
    trackEvent('sql_executed', {
      database_name: (database && database.name) || '',
      database_type: (database && database.type) || '',
      trigger,
    });
  }

  // Looks up a connection's own `type` (set server-side on both
  // connection_selection and phase_b_connection_done - see
  // translate_routes.py) from a {kind,id,...} list, by (kind, id) - shared
  // by both trackAllModeFanoutExecute() call sites below that don't
  // already have `type` sitting on the object they're tracking.
  function findConnectionType(list, kind, id) {
    const match = (list || []).find((e) => e.kind === kind && e.id === id);
    return (match && match.type) || '';
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
  // switching it after load, plus keeping CodeMirror in sync since it
  // doesn't read CSS custom properties on its own.
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
    // Chart.js reads plain CSS custom property VALUES at construction time
    // (see getChartSeriesColors()/getChartAxisColors() below) rather than
    // living CSS variables it can react to on its own - an already-drawn
    // chart would otherwise keep the OLD theme's colors until the user
    // switched tabs. rerenderActiveResultChartIfShowing (defined further
    // down, in the charting section) is a no-op whenever the active tab
    // isn't actually showing a chart right now.
    rerenderActiveResultChartIfShowing();
  }

  // DOM Elements - Primary Controls
  const aiPrompt = document.getElementById('aiPrompt');
  const sqlQueryTextarea = document.getElementById('sqlQuery');
  const translateBtn = document.getElementById('translateBtn');
  const runBtn = document.getElementById('runBtn');
  const stopBtn = document.getElementById('stopBtn');
  const goBackBtn = document.getElementById('goBackBtn');
  const goForwardBtn = document.getElementById('goForwardBtn');
  const newTurnBtn = document.getElementById('newTurnBtn');
  // Declared (as null) here rather than at its original spot right above
  // the CodeMirror.fromTextArea() call further down - hasNothingToClear()
  // (called from updateHistoryNavButtons() immediately below, to set
  // #newTurnBtn's initial disabled state) reads it via getSqlQuery(), and
  // a `let` binding is in the temporal dead zone - accessing it throws -
  // from the top of its enclosing scope until its own declaration line
  // actually runs. Moving the declaration up here (its value is still only
  // ever really assigned down at the original spot) is enough to get it
  // out of the TDZ before this first call needs it; sqlEditor itself stays
  // null until CodeMirror actually initializes either way.
  let sqlEditor = null;
  updateHistoryNavButtons();
  const micBtn = document.getElementById('micBtn');
  // Opens #reportIssueModal in 'wrong_sql' mode (see REPORT_CATEGORY_CONFIG
  // below) - sits beside #runBtn inside the SQL box itself, so it's wired
  // separately from both the resultsBody-delegated error/wrong_result
  // triggers and the header's #sendFeedbackBtn.
  const reportSqlBtn = document.getElementById('reportSqlBtn');
  // Same idea, opposite verdict - opens #reportIssueModal in 'correct_sql'
  // mode instead (see REPORT_CATEGORY_CONFIG below). Tracked as its own
  // element throughout this file (disabled/hidden state, click handler)
  // rather than inferred from reportSqlBtn, so the two stay independently
  // wireable even though today they're always shown/hidden/enabled/disabled
  // in lockstep.
  const reportSqlGoodBtn = document.getElementById('reportSqlGoodBtn');

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

  // DOM Elements - History Modal (see loadChatHistorySummary()/
  // renderChatHistoryBucketList() below - this modal used to show the
  // "translations" audit log's own table/charts/purge button; it now shows
  // chat_history's per-database turn counts instead, with per-database and
  // delete-all controls, per bucket_key)
  const historyModal = document.getElementById('historyModal');
  const historyBtn = document.getElementById('historyBtn');
  const historyModalCloseBtn = document.getElementById('historyModalCloseBtn');
  const chatHistoryBucketList = document.getElementById('chatHistoryBucketList');
  const deleteAllChatHistoryBtn = document.getElementById('deleteAllChatHistoryBtn');

  // DOM Elements - New Version Banner (see fetchClientBuildId()/
  // checkForNewClientVersion() below)
  const newVersionBanner = document.getElementById('newVersionBanner');
  const newVersionReloadBtn = document.getElementById('newVersionReloadBtn');
  const newVersionDismissBtn = document.getElementById('newVersionDismissBtn');

  // DOM Elements - Server Down Banner (see markServerUnreachable()/
  // markServerReachable() below)
  const serverDownBanner = document.getElementById('serverDownBanner');

  // DOM Elements - Results Table & Tabs
  const resultsRetryStatus = document.getElementById('resultsRetryStatus');
  const resultsTabsNav = document.getElementById('resultsTabsNav');
  const resultsHeader = document.getElementById('resultsHeader');
  const resultsBody = document.getElementById('resultsBody');
  // Table/Chart toggle + Chart.js canvas (see renderTableResult()'s own
  // charting branch, near the bottom of section 8, and renderResultChart()
  // just above it) - single-connection mode only, see this feature's own
  // section comment above requestSingleModeResultsSummary().
  const resultsViewToggle = document.getElementById('resultsViewToggle');
  const resultsTableWrapper = document.getElementById('resultsTableWrapper');
  const resultsChartWrapper = document.getElementById('resultsChartWrapper');
  const resultsChartCanvas = document.getElementById('resultsChartCanvas');
  // Visible counterpart to a result's own "truncated" flag (see
  // backends/base.py's EXECUTE_RESULTS_MAX_ROWS/fetch_capped_rows) - a
  // query that genuinely matched more rows than that cap gets its data
  // silently capped server-side (to avoid the out-of-memory crash an
  // unbounded fetch/JSON payload would cause), so this is what tells the
  // user they're looking at a partial result rather than the whole thing.
  const resultsTruncatedNotice = document.getElementById('resultsTruncatedNotice');

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
  // only for categories with previewEditable:true ('wrong_sql' and
  // 'correct_sql' - see REPORT_CATEGORY_CONFIG and openReportIssueModal()).
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

  // sqlEditor itself is declared (as null) much earlier in this file now -
  // see that declaration's own comment for why - this is still where it
  // actually gets a real CodeMirror instance assigned, if one loads.
  if (sqlQueryTextarea && window.CodeMirror) {
    sqlEditor = window.CodeMirror.fromTextArea(sqlQueryTextarea, {
      mode: 'text/x-sql',
      theme: getCurrentTheme() === 'light' ? 'eclipse' : 'dracula',
      lineNumbers: true,
      lineWrapping: true,
      placeholder: sqlQueryTextarea.getAttribute('placeholder') || "You may enter SQL here and execute it..."
    });
  }

  // Keeps Execute/"report wrong SQL" disabled whenever the box is empty -
  // see onSqlContentMaybeChanged()/applySqlActionButtonsContentState()'s
  // own comments further down for the full reasoning. CodeMirror's
  // 'change' event covers typing, pasting, AND a programmatic setValue()
  // call (setSqlQuery() uses exactly that) - the plain-textarea fallback
  // (no CodeMirror loaded) only needs 'input' since nothing in this app
  // calls .value = ... directly on that element. Called once immediately
  // after too, so both buttons start out correctly disabled for the empty
  // box a fresh page load always begins with - the HTML itself has no
  // `disabled` attribute on either button, only relying on this.
  if (sqlEditor) {
    sqlEditor.on('change', onSqlContentMaybeChanged);
  } else if (sqlQueryTextarea) {
    sqlQueryTextarea.addEventListener('input', onSqlContentMaybeChanged);
  }
  applySqlActionButtonsContentState();

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
  let googleIdentityInitializedClientId = null;

  function ensureGoogleIdentityInitialized(clientId) {
    if (!clientId || !window.google || !google.accounts || !google.accounts.id) return;
    // initialize() REPLACES (doesn't merge with) any prior configuration -
    // per Google's own JS reference, it "should be called only once" - so
    // this only actually calls it the first time a given clientId is seen,
    // rather than on every renderAuthUI() render.
    if (googleIdentityInitializedClientId === clientId) return;
    google.accounts.id.initialize({ client_id: clientId, callback: handleGoogleCredentialResponse });
    googleIdentityInitializedClientId = clientId;
  }

  function handleGoogleCredentialResponse(response) {
    if (!response.credential) return;
    googleIdToken = response.credential;
    persistGoogleIdToken(googleIdToken);
    // GA4's own recommended "login" event shape (see
    // https://developers.google.com/analytics/devguides/collection/ga4/reference/events)
    // has exactly one optional parameter, "method" - always
    // "Google" here, since Google Sign-In is the only method this
    // app supports. Still no identity/PII in the params - GA is
    // for usage counts, not a record of who signed in.
    trackEvent('login', { method: 'Google' });
    // A new user logging on takes over what was, until now, an anonymous
    // (or a different user's) identity - the prompt/SQL/results on screen
    // belong to THAT identity's own conversation, not this one. This used
    // to force-clear them (clearActiveQueryState()); now fetchBackendConfig()
    // below picks up the new identity (data.user_id) and calls
    // reconcileActiveHistoryBucket() itself, which switches to (or creates)
    // THIS identity's own bucket for whatever connection is active and
    // restores it - showing this identity's own last turn if it's been
    // seen before this page-load, or a blank slate if not. Nothing to do
    // here directly any more.
    renderAuthUI(currentGoogleClientId);
    fetchBackendConfig();
  }

  async function handleLogout() {
    trackEvent('logout', {});
    googleIdToken = null;
    clearPersistedGoogleIdToken();
    // Reset eagerly (not just left to the next fetchBackendConfig() call
    // below) so a render triggered in between - however unlikely - can't
    // still see the old identity as "server confirmed" for a moment.
    serverConfirmedAuthEmail = null;
    if (window.google && google.accounts && google.accounts.id) {
      google.accounts.id.disableAutoSelect();
    }
    // Render the signed-out UI RIGHT AWAY, synchronously within this same
    // click handler, rather than after the awaited /api/auth/logout call
    // below - two reasons. (1) The obvious one: the user should see
    // "signed out" the instant they click, not once a network round-trip
    // completes. (2) Less obviously: this is what actually detaches
    // #logoutBtn from the DOM before this same click event finishes
    // bubbling up to document - the more-menu's own outside-click handler
    // (see moreMenuBtn's click listener) checks
    // `moreMenuWrapper.contains(e.target)` on every click, and relies on
    // that detachment having already happened synchronously to correctly
    // treat this click as "outside" and auto-close the menu. Deferring
    // this render until after an `await` would let that synchronous
    // bubbling phase finish first with logoutBtn still very much attached,
    // leaving the more menu open when it should have closed.
    renderAuthUI(currentGoogleClientId);
    // Clears the app's OWN long-lived session cookie server-side (see
    // auth.py's refresh_auth_session_cookie()/auth_session.py) - without
    // this, a browser that still carries that cookie would keep resolving
    // to the same signed-in identity via get_current_user_identity()'s
    // session-cookie fallback even after the render above has cleared
    // this tab's own local token, silently undoing the logout on the very
    // next request (or in another tab that shares this same cookie).
    // Awaited so it's guaranteed to land before fetchBackendConfig() below
    // re-syncs config against the server; best-effort otherwise - a
    // network hiccup here still leaves this tab's own local sign-out fully
    // in effect, it just means the server-side cookie lingers until it
    // naturally lapses (SESSION_MAX_AGE_SECONDS) rather than being cleared
    // right away.
    try {
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch (e) {
      // Network error - see comment above; nothing else to do here.
    }
    // Logging out drops back to a (new, distinct) anonymous session - see
    // the sign-in callback's own comment just above for why
    // fetchBackendConfig() here is what actually switches the active
    // history bucket now (via reconcileActiveHistoryBucket()), not a
    // direct clearActiveQueryState() call here.
    fetchBackendConfig();
  }

  function renderAuthUI(clientId) {
    if (clientId) currentGoogleClientId = clientId;
    ensureGoogleIdentityInitialized(currentGoogleClientId);
    const container = document.getElementById('g_id_signin');
    if (!container) return;

    const existingToken = googleIdToken;
    const payload = existingToken ? parseJwt(existingToken) : null;
    const isExpired = payload && payload.exp && (payload.exp * 1000 < Date.now());
    const localSignedIn = !!(existingToken && payload && !isExpired);

    // The local Google ID token's own `exp` is only ever an ahead-of-server
    // best guess - the app's own long-lived session cookie (see auth.py's
    // refresh_auth_session_cookie()/auth_session.py) can keep the SERVER
    // resolving this browser to a real signed-in identity for up to 30
    // days, well past that ~1hr local expiry. Trusting
    // serverConfirmedAuthEmail here (see its own declaration comment) - not
    // just the local JWT - is what stops the avatar from flipping back to
    // a "Sign in" button while the user is actually still signed in as far
    // as every other request on this page is concerned.
    const signedIn = localSignedIn || Boolean(serverConfirmedAuthEmail);
    const displayEmail = localSignedIn ? (payload.email || 'Authenticated') : (serverConfirmedAuthEmail || 'Authenticated');

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
    const authStateKey = signedIn ? `in:${displayEmail}` : `out:${currentGoogleClientId || ''}`;
    if (authStateKey === lastRenderedAuthState) {
      return;
    }
    lastRenderedAuthState = authStateKey;

    if (signedIn) {
      const userEmail = displayEmail;
      // Google's own `picture`/`given_name`/`name` claims inside the local
      // JWT don't need this token to still be TRUSTED for authentication
      // (that's `localSignedIn`/`isExpired` above, superseded by
      // serverConfirmedAuthEmail once the ~1hr local token lapses) - they're
      // just descriptive facts captured at sign-in time that don't become
      // wrong the moment `exp` passes, and the browser never re-verifies
      // the JWT's signature either way (that only ever happens server-side,
      // when this token is sent as a Bearer credential - see
      // getApiHeaders()). Reading `payload` here whenever it exists AT ALL
      // - expired or not - rather than gating on `localSignedIn` the way
      // this used to, is what keeps the avatar looking exactly like
      // Google's own for the user's WHOLE session (up to 30 days, per the
      // session-cookie feature above): without this, it fell back to a
      // plain "first letter of email" circle within about an hour of
      // signing in even though the user was still very much signed in -
      // read as a surprising, faintly suspicious-looking downgrade rather
      // than a normal, expected part of staying logged in. Only a
      // payload-less signed-in state (no local token ever stored on this
      // browser at all - just serverConfirmedAuthEmail on its own, e.g. a
      // browser whose localStorage was cleared without also clearing the
      // session cookie) still falls back to the plain email-initial circle.
      const displayName = (payload && (payload.given_name || payload.name)) || '';
      const initial = (displayName || userEmail).charAt(0).toUpperCase() || 'U';
      const avatarContent = (payload && payload.picture)
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
        clearPersistedGoogleIdToken();
      }

      container.innerHTML = '';
      const targetClientId = clientId || currentGoogleClientId;
      if (window.google && google.accounts && targetClientId) {
        // initialize() itself already happened up top via
        // ensureGoogleIdentityInitialized() - only the visible button is
        // rendered here.
        google.accounts.id.renderButton(container, {
          theme: 'filled_black',
          size: 'medium',
          shape: 'rectangular',
          type: 'standard',
          text: 'signin',
          logo_alignment: 'left'
        });

        // Deliberately no google.accounts.id.prompt() call HERE (or
        // anywhere else in this file) - on Cloud Run the app supports
        // anonymous use, so we don't want the One Tap sign-in prompt
        // popping up unasked on every load for a visitor who was never
        // signed in to begin with. The rendered button above is always
        // available for anyone who wants to log in. This branch (this same
        // "not signedIn" button, no popup involved) is reached only once
        // BOTH the local token has expired AND the server's own long-lived
        // session cookie has too (or was never issued/has been logged out
        // of) - see serverConfirmedAuthEmail's declaration comment. A
        // signed-in user active at least once every 30 days never sees
        // this again until they explicitly log out.
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
    // Execute / "report wrong SQL" also depend on whether there's any SQL
    // text at all - see applySqlActionButtonsContentState()'s own comment
    // above - so, unlike every other control here, they're not simply set
    // to `disabled`: forced off (regardless of content) while a turn
    // starts, matching every other control here, but only turned back on -
    // if the box isn't empty - once a turn ends.
    if (disabled) {
      if (runBtn) runBtn.disabled = true;
      if (reportSqlBtn) reportSqlBtn.disabled = true;
      if (reportSqlGoodBtn) reportSqlGoodBtn.disabled = true;
    } else {
      applySqlActionButtonsContentState();
    }
    if (micBtn) micBtn.disabled = disabled;
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
      if (newTurnBtn) newTurnBtn.disabled = true;
    } else {
      // Re-enabling: defer to the boundary logic rather than unconditionally
      // turning them back on (e.g. stay disabled if already at the oldest turn).
      updateHistoryNavButtons();
    }
  }

  function getSqlQuery() {
    return sqlEditor ? sqlEditor.getValue().trim() : (sqlQueryTextarea ? sqlQueryTextarea.value.trim() : '');
  }

  // Execute (#runBtn) and the SQL box's own "report wrong SQL"/"report
  // accurate SQL" thumbs-down/thumbs-up buttons (#reportSqlBtn/
  // #reportSqlGoodBtn) are all meaningless with an empty box - nothing to
  // execute, nothing to flag either way - so all three stay disabled
  // whenever getSqlQuery() is empty, on top of (never instead of)
  // setButtonsDisabled()'s own "a turn is in flight" disabling (see that
  // function's own call of this, in its `else` branch). Applied directly
  // there for the turn-just-ended case, and separately via a live
  // CodeMirror 'change' listener (see where sqlEditor is constructed,
  // further up) for every other case - typing, pasting, clearing, or a
  // mid-turn setSqlQuery() fill-in - so all three buttons track the box's
  // actual content at all times, not just at turn boundaries.
  function applySqlActionButtonsContentState() {
    const hasSql = !!getSqlQuery();
    if (runBtn) runBtn.disabled = !hasSql;
    if (reportSqlBtn) reportSqlBtn.disabled = !hasSql;
    if (reportSqlGoodBtn) reportSqlGoodBtn.disabled = !hasSql;
  }

  // Wired to the live CodeMirror 'change' event (and, in the no-CodeMirror
  // fallback, the plain textarea's own 'input' event - see below) rather
  // than called directly from applySqlActionButtonsContentState()'s own
  // call sites, so it can add the ONE extra check those don't need: skip
  // entirely while a turn is in flight (uiActionBusy). Without that check,
  // a mid-turn setSqlQuery(data.sql) fill-in - which still fires this same
  // 'change' event, since CodeMirror's readOnly option (set by
  // setButtonsDisabled(true) for the whole turn) blocks USER typing, not a
  // programmatic setValue() call - would prematurely re-enable both
  // buttons before the turn's own setButtonsDisabled(false) call does, the
  // exact "ground shifting under an in-flight turn" problem readOnly
  // itself exists to prevent for typing.
  function onSqlContentMaybeChanged() {
    if (uiActionBusy) return;
    applySqlActionButtonsContentState();
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
    // Belt-and-suspenders alongside the live 'change'/'input' listeners
    // wired where sqlEditor is constructed further up: CodeMirror's
    // setValue() above does fire 'change' on its own, but a plain
    // `element.value = ...` assignment on the no-CodeMirror fallback
    // textarea never fires a DOM 'input' event by itself (only real
    // keystrokes do) - so without this explicit call, Execute/"report
    // wrong SQL" would stay stuck disabled forever after a fallback-mode
    // translation filled the box in. Still gated by uiActionBusy (see
    // onSqlContentMaybeChanged()) so a mid-turn call here (translatePrompt()
    // filling in generated SQL while its own executeSql() hasn't finished
    // yet) still can't prematurely re-enable either button.
    onSqlContentMaybeChanged();
  }

  function clearResultsDisplay() {
    hideRetryStatus();
    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) resultsBody.innerHTML = '';
    currentResultsList = [];
    activeResultIndex = 0;
    setReportContext(null);
    // Charting (see renderTableResult()'s own identical reset, and
    // destroyResultsChart()/resultsViewToggle/resultsChartWrapper further
    // down) - this function bypasses renderTableResult() entirely (it
    // hand-clears resultsHeader/resultsBody instead of calling
    // renderTableResult(null)), so without this a chart left showing from
    // the PREVIOUS turn would keep rendering - stale data, on top of a
    // "cleared" results area - right up until the next renderTableResult()
    // call for whatever this new turn produces. Every call site of this
    // function (translatePrompt() at the start of a new turn included) is
    // exactly the moment a stale chart must not linger.
    if (resultsViewToggle) resultsViewToggle.classList.add('hidden');
    if (resultsChartWrapper) resultsChartWrapper.classList.add('hidden');
    if (resultsTableWrapper) resultsTableWrapper.classList.remove('hidden');
    destroyResultsChart();
    // Same reasoning as the chart reset just above - a truncation notice
    // left over from the PREVIOUS turn's result must not linger through a
    // "cleared" results area either.
    if (resultsTruncatedNotice) resultsTruncatedNotice.classList.add('hidden');
  }

  // Shown at the top of the results area (above the tabs/table, see
  // index.html) while /api/translate - or, via requestAllModeResultsSummary()/
  // requestSingleModeResultsSummary() below, /api/summarize-results/
  // /api/summarize-result - is working through its own server-side retry
  // loop (see the comment above readNdjsonStream() below for why this is
  // the only retry loop left after removing the client-side one that used
  // to duplicate it). Cleared by clearResultsDisplay() so it never lingers
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
      `The model ran into a transient error${keyNote} - retrying (attempt ${attempt} of ${maxAttempts})...`;
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

  // Reads a newline-delimited-JSON (NDJSON) response body, calling
  // `onEvent` live for every progress line as it arrives. Originally
  // written for /api/translate alone (hence still finding its home
  // among this file's other /api/translate-specific helpers) but now
  // shared by /api/summarize-results and /api/summarize-result too (see
  // requestAllModeResultsSummary()/requestSingleModeResultsSummary()
  // below) - those two routes' own retry loops used to be invisible to
  // the client entirely (a plain, one-shot response, no progress of any
  // kind), and streamed NDJSON the same way /api/translate always has is
  // the same fix applied a second and third time, not a new mechanism.
  //
  // /api/translate (see translate_routes.py's module docstring): zero or
  // more {"status": "retrying", ...} progress lines emitted live as the
  // server's one Gemini-call retry loop runs, plus - for the
  // single-connection path only - two {"status": "phase_status", "phase":
  // "schema"|"generating_sql", "message": ...} lines emitted once each,
  // ahead of the schema lookup and the LLM call respectively (see
  // showPhaseStatus() above) - or, for the "all databases" mode "route"
  // outcome only, one {"status": "phase_a_route", ...} line followed by
  // one {"status": "phase_b_connection_done", ...} line per selected
  // connection (see translate_routes.py's stream_translation() docstring).
  // /api/summarize-results and /api/summarize-result (see their own
  // docstrings) only ever emit {"status": "retrying", ...} progress lines
  // ahead of their own terminal line - no phase_status/phase_a_route/
  // phase_b_connection_done lines, those are /api/translate-specific.
  // Followed in every case by exactly one terminal {"status": "done",
  // success, ...} line - the same shape each of these three routes used
  // to return as its whole body before streaming existed. A request that
  // never reaches the retry loop at all (missing prompt/API key, a 401
  // from the auth guard, or a mocked response in tests - see fixtures.js's
  // mockTranslate()) isn't streamed - it's still a single plain JSON
  // object, which this reads exactly the same way: one line, no "status"
  // field, straight into finalData.
  //
  // `onEvent`, if given, is called for every line EXCEPT the terminal
  // 'done' one, in arrival order, as soon as each is parsed - this is
  // what lets a caller react to 'retrying'/'phase_a_route'/
  // 'phase_b_connection_done' lines live rather than only after the
  // whole stream has finished (this function's own return value is
  // still just the terminal line, same as before onEvent existed).
  async function readNdjsonStream(response, onEvent) {
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
        console.warn('Failed to parse a line of an NDJSON response stream:', trimmed, err);
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

  // Wipes the NL prompt, the generated SQL, the results grid, and the
  // CURRENTLY ACTIVE bucket's own turn-navigation history (chatStore) -
  // i.e. actually destroys a conversation, not just navigates away from
  // it. Login, logout, and switching the active connection used to funnel
  // through here (on the theory that "the conversation" and "the
  // connection" were the same thing, so switching one meant discarding the
  // other) - they no longer do; see reconcileActiveHistoryBucket() above,
  // which switches `chatStore` to that identity/connection's OWN bucket
  // and restores it instead of wiping anything. This function is currently
  // unreferenced as a result - kept around rather than deleted, since an
  // explicit "start a new conversation" affordance (distinct from merely
  // switching to a connection that already has one) seems like a very
  // likely next step for this feature, and this is already exactly the
  // right building block for it.
  function clearActiveQueryState() {
    if (aiPrompt) aiPrompt.value = '';
    setSqlQuery('');
    clearResultsDisplay();
    chatStore.clear();
    updateHistoryTurnsSubtitle();
    PINNED_CONNECTIONS = [];
  }

  function updateHistoryTurnsSubtitle() {
    const clearMsgEl = document.getElementById('historyActionMsg');
    if (clearMsgEl) {
      clearMsgEl.textContent = '';
    }
    updateHistoryNavButtons();
  }

  // True if there's currently nothing for #newTurnBtn to clear - the
  // prompt/SQL boxes are both empty AND this bucket has no turns to step
  // back into - in which case it's disabled rather than sitting there as
  // an inert no-op. Also true while already viewing the blank slate itself
  // (clicking it again would do nothing new).
  function hasNothingToClear() {
    if (viewingBlankSlate) return true;
    const promptHasText = !!(aiPrompt && aiPrompt.value.trim());
    const sqlHasText = !!getSqlQuery();
    return !promptHasText && !sqlHasText && chatStore.turnCount() === 0;
  }

  function updateHistoryNavButtons() {
    // While a turn is in flight (uiActionBusy - see its own declaration
    // comment), these three stay forced disabled regardless of undo/redo
    // boundary state below. setButtonsDisabled(true) is what disables them
    // at the very start of a turn, but this function also gets called mid-
    // turn, well before that turn's own setButtonsDisabled(false) runs -
    // e.g. the "route" outcome's pushActiveTurn()/updateHistoryTurnsSubtitle()
    // call fires as soon as the terminal /api/translate line arrives, but
    // translatePrompt() itself may still be awaiting an internal
    // executeSql() call and/or Phase C summarization after that. Recomputing
    // a fresh (by now non-boundary) undo/redo state at that point would
    // re-enable Back/Forward/New well before the turn as a whole is
    // actually done - a real regression, since setButtonsDisabled() alone
    // no longer has the last word once something later in the same turn
    // calls this function again.
    if (uiActionBusy) {
      if (goBackBtn) goBackBtn.disabled = true;
      if (goForwardBtn) goForwardBtn.disabled = true;
      if (newTurnBtn) newTurnBtn.disabled = true;
      return;
    }

    // chatStore holds [user, model] pairs. When only one turn remains,
    // it's already the oldest turn on screen - going back from there would
    // pop it and leave the UI blank, so disable one step early. While
    // viewingBlankSlate (see its own docstring), that's no longer the right
    // test for #goBackBtn - the blank slate itself is already "one step
    // early", so back should be enabled as long as any real turn exists to
    // reveal, and #goForwardBtn has nothing to redo TO from a position that
    // was never pushed into history in the first place.
    const atOldestTurn = viewingBlankSlate ? chatStore.turnCount() === 0 : !chatStore.canUndo();
    const atNewestTurn = viewingBlankSlate ? true : !chatStore.canRedo();

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
    if (newTurnBtn) {
      const nothingToClear = hasNothingToClear();
      newTurnBtn.disabled = nothingToClear;
      newTurnBtn.classList.toggle('is-boundary', nothingToClear);
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
        // `data.error` is the raw exception text /api/ping's own except
        // branch now includes (see that route's own comment for why it's
        // fine to hand back) - see trackDbConnectionError()'s own comment
        // above for the full tracking rationale.
        trackDbConnectionError(data.error);
      }
    } catch (err) {
      connDbDot.className = 'status-dot disconnected';
      // Same tracking as the non-throwing failure branch above, just for
      // the case where the /api/ping fetch itself never came back at all
      // (network drop, etc.) rather than responding with success:false -
      // err.message stands in for data.error here since there's no
      // response body to read one from.
      trackDbConnectionError(err && err.message);
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
  // "All Pre-Configured Datasets" for real "all" mode, "Multiple databases" for
  // the legacy explicit-subset case below) and `names` is the full
  // in-scope name list (resolved via configured_databases/custom_databases,
  // both already present on every /api/config response) for the tooltip.
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
      // Presets only - see db.py's _resolve_all_configured_descriptors'
      // own docstring for why custom connections are deliberately never
      // part of "All Pre-Configured Datasets" mode. customDbs is intentionally
      // NOT included in `names` here (unlike the legacy branch below,
      // which can legitimately include them - it's an explicit, user-
      // picked subset, not "all"). Also excludes any preset the admin has
      // opted out of "all" mode (include_in_all_mode: false in
      // presets.json - see app_config.py's DATABASE_PRESETS_FILE comment
      // and db.py's _resolve_all_configured_descriptors) - configuredDbs
      // only ever carries this key when it's explicitly false (see
      // config_routes.py's _redact_preset_for_client), so `!== false`
      // treats a missing key exactly like an explicit true, matching the
      // server's own default. Without this filter, an opted-out preset
      // would still show up in this badge/tooltip as if it were part of
      // "All", even though the server never actually queries it.
      const names = configuredDbs.filter(db => db.include_in_all_mode !== false).map(db => db.name);
      return { count: names.length, label: names.length > 1 ? 'All Pre-Configured Datasets' : null, names };
    }
    const presetIds = data?.in_scope_preset_ids || [];
    const customKeys = data?.in_scope_custom_connection_keys || [];
    const count = presetIds.length + customKeys.length;
    if (count <= 1) return { count, label: null, names: [] };
    // A legacy session that saved an arbitrary multi-connection subset
    // before the binary single/all choice existed (see
    // resolve_in_scope_descriptors' docstring) - still more than one
    // connection in scope, but deliberately given its OWN label rather
    // than "All Pre-Configured Datasets" too: this subset is explicit and can
    // include custom connections, and isn't necessarily every preset
    // either, so calling it "All" anything would misdescribe it.
    const names = [
      ...presetIds.map(id => configuredDbs.find(db => db.id === id)?.name || id),
      ...customKeys.map(key => customDbs.find(db => db.connection_key === key)?.name || key),
    ];
    return { count, label: 'Multiple databases', names };
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
      // See CURRENT_USER_IDENTITY's own declaration comment - this is the
      // one place it's ever set, straight from the server's own resolved
      // identity, never re-derived here.
      CURRENT_USER_IDENTITY = data.user_id || 'global';
      // See serverConfirmedAuthEmail's own declaration comment - this is
      // the one place it's ever set, straight from the server's own
      // resolved identity (which may now come from its long-lived session
      // cookie rather than a live Bearer token), same as
      // CURRENT_USER_IDENTITY just above. Set BEFORE initGoogleAuth()'s
      // renderAuthUI() call below picks it up.
      serverConfirmedAuthEmail = data.authenticated ? (data.user_id || null) : null;

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
      // Remembered in currentHistoryMaxTurns too (not just applied to
      // today's chatStore) so a bucket created later - the first time a
      // different connection is ever visited this page-load - starts with
      // this same real cap instead of FALLBACK_HISTORY_TURNS.
      if (data.history_max_turns) {
        currentHistoryMaxTurns = data.history_max_turns;
      }
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
      // Same gate for the SQL box's "report wrong SQL"/"report accurate
      // SQL" thumbs-down/thumbs-up buttons - both just as persistent an
      // element as the two above.
      if (reportSqlBtn) reportSqlBtn.classList.toggle('hidden', !ISSUE_REPORTING_ENABLED);
      if (reportSqlGoodBtn) reportSqlGoodBtn.classList.toggle('hidden', !ISSUE_REPORTING_ENABLED);

      IN_SCOPE_PRESET_IDS = data.in_scope_preset_ids || [];
      IN_SCOPE_CUSTOM_KEYS = data.in_scope_custom_connection_keys || [];
      IN_SCOPE_MODE = data.in_scope_mode === 'all' ? 'all' : 'single';
      if (data.max_in_scope_connections) {
        MAX_IN_SCOPE_CONNECTIONS = data.max_in_scope_connections;
      }

      // Restore every persisted bucket for this identity from the server
      // BEFORE reconcileActiveHistoryBucket() switches to one - otherwise
      // that call would create and render an empty bucket first, then
      // this would have to swap in the real data a moment later. Guarded
      // so it only actually fetches once per identity (see
      // chatHistoryHydratedForIdentity's own declaration comment); placed
      // here (not earlier in this function) so currentHistoryMaxTurns has
      // already been reconciled with this response's history_max_turns by
      // the time any restored bucket is trimmed to it.
      if (CURRENT_USER_IDENTITY !== chatHistoryHydratedForIdentity) {
        await hydrateChatHistoryFromServer();
        chatHistoryHydratedForIdentity = CURRENT_USER_IDENTITY;
      }

      // Now that CURRENT_USER_IDENTITY/ACTIVE_*/IN_SCOPE_MODE all reflect
      // this response, switch to whichever bucket they now name - covers
      // login/logout (identity changed) and, on a fresh page load, the
      // very first real bucket (see reconcileActiveHistoryBucket()'s own
      // docstring; a no-op the rest of the time, e.g. every other call
      // this makes before/after each translate/execute).
      reconcileActiveHistoryBucket();

      renderDbRadioButtons();
      loadConfigIntoUI();

      await updateConnectionDetails(data);
      updateModelBadge();
    } catch (err) {
      console.error("Failed to fetch backend configuration:", err);
      if (connDbDot) {
        connDbDot.className = 'status-dot disconnected';
        // See trackDbConnectionError()'s own comment for why this counts -
        // the badge can't confirm the selected database is reachable
        // without a successful config fetch, so it's shown as down, same
        // as checkDbStatus()'s own dedicated liveness check.
        trackDbConnectionError(err && err.message);
      }
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
    // this function's original default. Postgres alone also gets an
    // (optional) "schema" config field, same as Redshift/Oracle/MSSQL above
    // - MySQL has no separate schema concept of its own (see backends/
    // mysql.py's module docstring), so it keeps the empty config object.
    const resolvedType = (type === 'mysql') ? type : 'postgres';
    return {
      name: '',
      type: resolvedType,
      url: '',
      config: (resolvedType === 'mysql') ? {} : { schema: '' },
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

      // Looked up from refreshingConnectionKeys (a Set keyed by
      // connection_key, declared alongside customDatabases) rather than
      // from any per-row/per-index flag, specifically so it survives this
      // function's own re-renders - renderCustomDbRows() rebuilds every
      // row's HTML from scratch on ANY change (toggling a totally
      // unrelated row's expand arrow, removing a different connection,
      // ...), which would otherwise silently wipe out a plain DOM
      // btn.disabled. Used below both to keep the refresh button itself
      // disabled+spinning and to disable this row's OWN remove ("x")
      // button for as long as its schema fetch is in flight - deleting a
      // connection whose refresh is mid-request doesn't corrupt anything
      // (the fetch just keeps running against whatever the server still
      // has persisted, and prime_schema_cache()/get_database_schema() only
      // ever touch the in-memory schema cache - see this feature's own
      // design notes), but it can produce a confusing "Failed to refresh
      // schema for X: Connection not found" popup later for a connection
      // the user already intentionally removed - simplest to just not let
      // the two race in the first place.
      const isRefreshing = Boolean(db.connection_key) && refreshingConnectionKeys.has(db.connection_key);

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
            ${db.connection_key ? `<button type="button" class="btn btn-secondary custom-db-refresh-btn" data-index="${index}" title="${isRefreshing ? 'Refreshing schema…' : 'Refresh Schema'}" ${isRefreshing ? 'disabled' : ''}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${isRefreshing ? 'animate-spin' : ''}">
                <polyline points="23 4 23 10 17 10"></polyline>
                <polyline points="1 20 1 14 7 14"></polyline>
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
              </svg>
            </button>` : ''}
            <button type="button" class="btn btn-secondary custom-db-remove-btn" data-index="${index}" title="${isRefreshing ? 'Wait for the schema refresh to finish before removing this connection' : 'Remove this connection'}" ${isRefreshing ? 'disabled' : ''}>&times;</button>
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
          ${!isMySQL ? `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-pg-schema-${index}">Schema: <span class="optional-hint">(optional)</span></label>
              <input type="text" id="custom-db-pg-schema-${index}" class="config-input custom-db-pg-schema" data-index="${index}" placeholder="Defaults to the connecting user's search_path (usually public)" value="${cfg.schema || ''}" autocomplete="off">
            </div>
          </div>
          ` : ''}
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

    container.querySelectorAll('.custom-db-refresh-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const index = parseInt(btn.dataset.index);
        handleRefreshSchemaClick(customDatabases[index], activeUrl);
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

    container.querySelectorAll('.custom-db-pg-schema').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) selectDbConnectionRow(radio, index); });
      input.addEventListener('input', () => {
        if (radio) selectDbConnectionRow(radio, index);
        if (!customDatabases[index].config) customDatabases[index].config = {};
        // Optional, same treatment as Redshift's/Oracle's/SQL Server's own
        // schema field above - blank means "use Postgres's own ordinary
        // search_path default", not a credential so no masking/"leave
        // blank to keep it" convention applies here.
        customDatabases[index].config.schema = input.value.trim();
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

  // "Refresh Schema" (see renderCustomDbRows()'s new .custom-db-refresh-btn
  // above, only rendered for an already-saved custom connection) - a
  // blocking call scoped to just this one connection, not the whole config
  // modal (no existing modal-wide disable helper covers #configModal; see
  // setButtonsDisabled(), which is scoped to the main chat/translate
  // controls only). On success, just re-renders back to normal - no success
  // message. On failure, shows a popup dialog (showAlertDialog, below) -
  // a deliberate departure from this app's usual inline-#configSaveError
  // convention, per how this feature was specified.
  //
  // In-flight state lives in refreshingConnectionKeys (declared alongside
  // customDatabases), not in a plain btn.disabled flip - a bare DOM flag
  // would get silently reset the moment ANY unrelated change re-renders
  // this row (see renderCustomDbRows()'s refresh-button comment), which is
  // exactly how this used to let a user fire off several fully concurrent
  // refresh requests for the very same connection: click, then toggle/
  // expand a different row (or remove one, or add a new blank one) while
  // the fetch is still pending, and the re-render handed back a fresh,
  // enabled button with no memory of the request still running. Guarding
  // on the Set here - checked BEFORE anything else, and populated before
  // the very first re-render - closes that regardless of how many times
  // this row happens to get rebuilt while a request is outstanding.
  //
  // Refreshing two DIFFERENT connections at once is fine and intentional -
  // each is its own independent /api/config/refresh-schema call against
  // its own connection_key, exactly like clicking "Refresh Schema" on two
  // separate rows always has been. Only a second click on the SAME
  // still-in-flight connection is what this guards against.
  async function handleRefreshSchemaClick(db, activeUrl) {
    if (!db || !db.connection_key || refreshingConnectionKeys.has(db.connection_key)) return;
    const displayName = db.name || 'this connection';
    refreshingConnectionKeys.add(db.connection_key);
    renderCustomDbRows(activeUrl);
    try {
      const response = await fetch('/api/config/refresh-schema', {
        method: 'POST',
        headers: getApiHeaders(),
        body: JSON.stringify({ connection_key: db.connection_key }),
      });
      if (!response.ok) {
        let errorMessage = 'Failed to refresh schema.';
        try {
          const errData = await response.json();
          if (errData && errData.error) errorMessage = errData.error;
        } catch (parseErr) { /* non-JSON error body - keep the generic message */ }
        // Named explicitly - a bare "Failed to refresh schema." gives no
        // way to tell which of several saved connections it was about,
        // especially once the button itself has already gone back to its
        // normal (non-spinning) state by the time this dialog is dismissed.
        await showAlertDialog(`Failed to refresh schema for "${displayName}": ${errorMessage}`);
      }
    } catch (err) {
      console.error(`Failed to refresh schema for "${displayName}":`, err);
      await showAlertDialog(`Failed to refresh schema for "${displayName}". Check your connection and try again.`);
    } finally {
      refreshingConnectionKeys.delete(db.connection_key);
      renderCustomDbRows(activeUrl);
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

    let html = `<div class="radio-group-heading">PRE-CONFIGURED DATASETS (PLAYGROUNDS)</div>`;

    // Two visual columns, purely a layout grouping (no change to what's
    // selectable or how - db_connection_option/preset:<id> works exactly
    // the same either way) - split straight down the middle by COUNT, not
    // by dialect type: an earlier version grouped the 4 "simple
    // credential" dialects (Postgres/MySQL/Oracle/SQL Server/MongoDB) on
    // the left and the structured/cloud ones (BigQuery/Snowflake/
    // Databricks/Redshift/Sheets) on the right, but that left a whole
    // column empty whenever an admin's presets happened to cluster on one
    // side (e.g. two Postgres presets and nothing else - exactly what
    // "balanced, half on the left and half on the right" was reported
    // against). The first (ceil half) of CONFIGURED_DBS's own order goes
    // left, the rest go right, so an odd count leans left by one rather
    // than leaving a column short by more than that.
    const leftCount = Math.ceil(CONFIGURED_DBS.length / 2);
    const leftPresets = CONFIGURED_DBS.slice(0, leftCount);
    const rightPresets = CONFIGURED_DBS.slice(leftCount);

    // Whether "All Pre-Configured Datasets" is even worth offering as a
    // choice - see app_config.py's DATABASE_PRESETS_FILE comment on
    // "include_in_all_mode" and db.py's _resolve_all_configured_descriptors,
    // which now excludes any preset an admin has opted out. Deliberately
    // ">  0", not "> 1": a single eligible preset already renders "All"
    // today (see config-modal.spec.js's "with only one preset configured"
    // test - unchanged, since include_in_all_mode defaults to eligible),
    // so this must stay a no-op for every deployment that's never touched
    // the new field. It only ever hides the option in the NEW case this
    // field introduces: an admin has opted every single configured preset
    // out, leaving nothing for "All" to mean beyond the single fallback
    // default connection - confusing to still offer as if it were a real
    // combined-mode choice. NOTE: this is a display-only check computed
    // fresh every render - a session already saved in_scope_mode "all"
    // from before an admin dropped eligibility to zero simply shows no
    // radio checked next time this dialog opens, rather than something
    // crashing; not solved further here ("for now").
    const allModeEligibleCount = CONFIGURED_DBS.filter(db => db.include_in_all_mode !== false).length;
    const showAllOption = allModeEligibleCount > 0;

    // "All Pre-Configured Datasets" (see db.py's _resolve_all_configured_
    // descriptors - presets only, never custom connections) renders as one
    // more option in the SAME two-column preset list, directly after the
    // very last preset - never its own separate section below the custom
    // connections list the way it used to when it still spanned both
    // lists. Appended to whichever column that last preset itself landed
    // in (the right column whenever there's more than one preset total,
    // so it sits right under the last preset there; the left column in
    // the edge case where there's only 0-1 presets and the right column
    // is empty) - this never disturbs the existing left/right preset
    // split itself (see the count-based comment above), it only adds one
    // extra item to whichever column already ends last. Only relevant at
    // all when showAllOption is true.
    const allOptionGoesInRightColumn = rightPresets.length > 0;

    // Explanation of what this option does - previously a standalone <p>
    // below the two-column grid, now an on-hover title attribute on the
    // option itself instead (per explicit request to get it out of the
    // dialog body), same text unchanged.
    const ALL_OPTION_HINT = "Ask a question without picking a database first - the app figures out which preset "
      + "dataset(s) it applies to, and can query more than one at once when a question genuinely needs it. Only "
      + "presets are eligible here - your own custom connections are never included.";

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

    const allOption = showAllOption ? `
      <label class="radio-option all-databases-option" title="${ALL_OPTION_HINT}">
        <input type="radio" name="db_connection_option" value="all" ${allSelected ? 'checked' : ''}>
        <span class="radio-label">All Pre-Configured Datasets</span>
      </label>
    ` : '';
    const leftColumnHtml = leftPresets.map(renderPresetOption).join('') + (allOptionGoesInRightColumn ? '' : allOption);
    const rightColumnHtml = rightPresets.map(renderPresetOption).join('') + (allOptionGoesInRightColumn ? allOption : '');

    html += `
      <div class="preset-columns">
        <div class="preset-column">${leftColumnHtml}</div>
        <div class="preset-column">${rightColumnHtml}</div>
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
          // schema is Postgres-only - MySQL has no separate schema concept
          // of its own (see backends/mysql.py's module docstring).
          dbSchema = (dbType === 'postgres' && chosen.config && chosen.config.schema) || null;
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
          // schema is Postgres-only (optional) - MySQL has no separate
          // schema concept of its own (see backends/mysql.py's module
          // docstring).
          if (simpleUrlType === 'postgres' && d.config && d.config.schema) {
            simpleUrlOut.schema = d.config.schema;
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
      // schema is Postgres-only (optional) - MySQL has no separate schema
      // concept of its own (see backends/mysql.py's module docstring).
      if (dbSchema && dbType === 'postgres') payload.schema = dbSchema;
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
        // Unconditional, mirroring fetchBackendConfig()'s own
        // `data.active_database_url || DEFAULT_DB_URL` (config_routes.py
        // always sends this field, coalesced to '' rather than omitted -
        // see its own docstring). This USED to be an `if (data.active_
        // database_url)` guard that only ever overwrote ACTIVE_DB_URL,
        // never reset it - harmless before computeBucketKey() existed
        // (nothing else depended on ACTIVE_DB_URL staying in sync after a
        // save), but a real bug for it: switching FROM a custom connection
        // (a real, disclosed URL) TO a preset whose own URL isn't sent to
        // the client left ACTIVE_DB_URL stuck on the custom connection's
        // URL, so computeBucketKey() computed a DIFFERENT key for that
        // preset than the one its very first visit used - "switch away and
        // back" would land in a fresh, blank bucket instead of the
        // preset's real one. Falling back to DEFAULT_DB_URL (not "") keeps
        // this consistent with fetchBackendConfig()'s own convention for
        // "a preset with no separately-disclosed URL of its own".
        ACTIVE_DB_URL = data.active_database_url || DEFAULT_DB_URL;
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

        // Switches to (or creates) whichever bucket the now-current
        // ACTIVE_*/IN_SCOPE_MODE actually names - a real connection change,
        // or flipping between single/all mode, both land here; re-saving
        // the same connection or toggling an unrelated preference (e.g.
        // auto-execute) computes the same key as before and is a no-op
        // (see reconcileActiveHistoryBucket()'s own docstring).
        reconcileActiveHistoryBucket();

        if (PINNED_CONNECTIONS.some(p => (
          p.kind === 'preset' ? !IN_SCOPE_PRESET_IDS.includes(p.id) : !IN_SCOPE_CUSTOM_KEYS.includes(p.id)
        ))) {
          // A connection this "all databases" conversation had pinned (see
          // PINNED_CONNECTIONS' own docstring) was just unchecked from
          // scope - the pin no longer describes a set the user actually
          // wants questions routed to. Unlike a real connection-identity
          // change, this does NOT touch the history bucket or the on-
          // screen prompt/SQL/results any more: "all databases" is one
          // shared conversation regardless of exactly which connections
          // are in scope (see computeBucketKey()), so excluding one from
          // scope doesn't invalidate it. The server independently guards
          // against a stale pin too (see execute_routes.py's
          // resolve_descriptor_by_reference fallback, which is the only
          // place a client-echoed pinned_connections entry is still read
          // at all) - this just keeps PINNED_CONNECTIONS itself from
          // silently pointing at a connection no longer in scope.
          PINNED_CONNECTIONS = [];
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
      if (connDbDot) {
        connDbDot.className = 'status-dot disconnected';
        // See trackDbConnectionError()'s own comment for why this counts -
        // a network exception while saving a DB connection means the
        // badge can't confirm it's reachable either.
        trackDbConnectionError(err && err.message);
      }
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
  // 7. HISTORY MODAL: see loadChatHistorySummary()/renderChatHistoryBucketList()/
  //    the per-row and #deleteAllChatHistoryBtn handlers further down -
  //    this used to be tab-switching + Chart.js setup for the "translations"
  //    audit log's own stats view. That view (and the /api/history +
  //    /api/history/purge endpoints behind it) has since been removed as
  //    dead code - see chat_history_routes.py's module docstring. The
  //    translations log itself is still recorded server-side (write-only,
  //    for aggregate usage/cost visibility), just with no UI or endpoint
  //    surfacing it anymore.
  // ===========================================================================

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

  // OK-only variant of showConfirmDialog() above, for the failure case -
  // this app otherwise shows errors inline (e.g. #configSaveError), but
  // the "Refresh Schema" button (handleRefreshSchemaClick, above) is
  // specified to show a real popup on failure instead. Nothing to
  // confirm/cancel here, only to acknowledge, so OK, the close button,
  // and clicking outside the modal all just dismiss it the same way.
  function showAlertDialog(message) {
    return new Promise((resolve) => {
      const modal = document.getElementById('alertModal');
      const textEl = document.getElementById('alertModalText');
      const okBtn = document.getElementById('alertModalOkBtn');
      const closeBtn = document.getElementById('alertModalCloseBtn');

      if (!modal || !textEl || !okBtn) {
        alert(message);
        resolve();
        return;
      }

      textEl.textContent = message;
      modal.classList.remove('hidden');
      bringModalToFront(modal);

      let cleanedUp = false;
      const cleanup = () => {
        if (cleanedUp) return;
        cleanedUp = true;
        modal.classList.add('hidden');
        okBtn.removeEventListener('click', onOk);
        closeBtn?.removeEventListener('click', onOk);
        modal.removeEventListener('click', onOutside);
        resolve();
      };

      const onOk = () => cleanup();
      const onOutside = (e) => {
        if (e.target === modal) cleanup();
      };

      okBtn.addEventListener('click', onOk);
      closeBtn?.addEventListener('click', onOk);
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
    // The positive counterpart to 'wrong_sql' above, triggered from the SQL
    // box's own thumbs-up button (#reportSqlGoodBtn) - same shape in every
    // respect (previewEditable, same detailsLabel, same buildReportPayload()
    // branch below, same server-side handling per report_routes.py's
    // _EDITABLE_CONTENT_CATEGORIES), just positive copy throughout: the
    // point is to tell the developer this SQL got it right, not to report a
    // problem.
    correct_sql: {
      modalTitle: 'Report Accurate SQL',
      previewLabel: 'Accurate SQL',
      intro: "Review and edit the prompt/SQL below as needed, add any comments, then send. This lets the developer know the generated SQL looks correct - helpful for confirming what's working well.",
      showPreview: true,
      previewEditable: true,
      previewSectionLabel: 'What will be sent (edit as needed)',
      detailsLabel: 'Additional comments (optional)',
      detailsPlaceholder: 'What did this SQL get right, or anything else worth noting?',
      detailsRequired: false,
      sendLabel: 'Report Accurate SQL',
      sendingLabel: 'Sending…',
    },
    // Triggered from the Summary tab's own thumbs-up/thumbs-down buttons
    // (see summaryFeedbackButtonsHtml()) - same "no preview, details box IS
    // the message" shape as 'feedback' above, and for the same reason this
    // one's more strict about: the prompt and the summary text itself are
    // deliberately never captured or sent here at all (see
    // buildReportPayload()), by explicit request, so there's nothing
    // structured left to preview even if this category wanted to.
    summary_thumbs_up: {
      modalTitle: 'Summary Feedback',
      previewLabel: 'Summary Feedback (Helpful)',
      intro: "Glad the summary was useful. The prompt and summary text aren't included here (kept private) - just let the developer know what worked.",
      showPreview: false,
      detailsLabel: 'Your feedback',
      detailsPlaceholder: 'What worked well about this summary?',
      detailsRequired: true,
      sendLabel: 'Send Feedback',
      sendingLabel: 'Sending…',
    },
    summary_thumbs_down: {
      modalTitle: 'Summary Feedback',
      previewLabel: 'Summary Feedback (Not Helpful)',
      intro: "Sorry the summary missed the mark. The prompt and summary text aren't included here (kept private) - just describe what was wrong or missing.",
      showPreview: false,
      detailsLabel: 'Your feedback',
      detailsPlaceholder: 'What was wrong or missing?',
      detailsRequired: true,
      sendLabel: 'Send Feedback',
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
    if (ctx.category === 'feedback' || ctx.category === 'summary_thumbs_up' || ctx.category === 'summary_thumbs_down') {
      // Deliberately just these two fields - see report_routes.py's module
      // docstring on why prompt/sql/database_name/content don't apply to
      // general app feedback the way they do for the other categories.
      // summary_thumbs_up/summary_thumbs_down follow 'feedback's own lead
      // here for the same reason, per explicit request: the prompt and
      // summary text are considered too sensitive to send at all, even
      // though (unlike 'feedback') there IS a specific result on screen
      // this feedback is "about".
      return { category: ctx.category, details: details || '' };
    }
    if (ctx.category === 'wrong_sql' || ctx.category === 'correct_sql') {
      // Unlike error/wrong_result (prompt/sql captured automatically and
      // shown read-only below), these two categories' whole point is that
      // the user can rewrite the captured prompt+SQL text before it's sent
      // - see REPORT_CATEGORY_CONFIG.wrong_sql/correct_sql's
      // previewEditable flag - so `content` is read straight from the
      // editable preview textarea's live value, not from
      // ctx.sql/getReportPromptText() directly. Empty here the first time
      // this runs, at modal-open (before openReportIssueModal() has seeded
      // the textarea via renderWrongSqlPreviewSeed()) - harmless, since
      // that call only uses this to confirm ctx is truthy. `category`
      // passes through ctx.category (not hardcoded) so this one branch
      // correctly serves both the "wrong" and "accurate" verdicts.
      return {
        category: ctx.category,
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

  // Seeds #reportIssuePreviewEditable for the 'wrong_sql'/'correct_sql'
  // categories (both previewEditable:true - see REPORT_CATEGORY_CONFIG) - a
  // plain, deliberately simpler layout than renderReportPreviewText()'s
  // (no "Category:"/"LLM:" header lines, since those are metadata the
  // server derives/sends separately, not part of the editable message
  // itself) since the user is meant to treat this as a starting draft
  // they'll likely trim down, not a fixed record they're just appending
  // to. Called once, at modal-open time - never regenerated afterward, so
  // edits the user makes are never clobbered by a later render. Identical
  // for both categories - the SQL itself is the SQL, whichever verdict the
  // user is reporting on it.
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
    // previewEditable ('wrong_sql'/'correct_sql') swaps in the plain
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
      // The Summary tab's own thumbs-up/thumbs-down buttons (see
      // summaryFeedbackButtonsHtml()) - same delegated-listener reasoning as
      // the Report button above (rebuilt fresh on every render), but always
      // passes an explicit, synthetic context rather than falling back to
      // currentReportContext - this is deliberately NOT "report the current
      // result" (that context carries the summary text as `.content`, which
      // this feedback is never allowed to send - see buildReportPayload()).
      const summaryFeedbackTrigger = e.target.closest('[data-summary-feedback-trigger]');
      if (summaryFeedbackTrigger) {
        const direction = summaryFeedbackTrigger.dataset.summaryFeedbackTrigger;
        openReportIssueModal({ category: direction === 'up' ? 'summary_thumbs_up' : 'summary_thumbs_down' });
      }
      // The Summary tab's "View as chart" callout (see
      // summaryChartCalloutHtml()/jumpToChartableResultTab()) - same
      // delegated-listener reasoning as the two triggers above.
      const viewChartTrigger = e.target.closest('[data-view-chart-trigger]');
      if (viewChartTrigger) jumpToChartableResultTab();
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
  // The SQL box's "report accurate SQL" thumbs-up button - the positive
  // counterpart right beside it, wired identically (same persistent
  // element/gating/fresh-read-at-click-time reasoning as reportSqlBtn
  // above), differing only in `category` - openReportIssueModal() picks up
  // REPORT_CATEGORY_CONFIG.correct_sql's copy from there.
  if (reportSqlGoodBtn) {
    reportSqlGoodBtn.addEventListener('click', () => openReportIssueModal({
      category: 'correct_sql',
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

  // The summary endpoint's own last response - cached so the per-row
  // delete buttons and #deleteAllChatHistoryBtn don't need to re-derive a
  // bucket's display label/turn_count from its bare bucket_key, and so
  // "delete all" knows exactly which bucket_keys currently exist without a
  // second round trip.
  let currentChatHistoryBuckets = [];

  // Mirrors chat_history_routes.py's own _resolve_bucket_display() -
  // "available" false means this bucket's connection could no longer be
  // resolved against this user's CURRENT presets/custom connections (see
  // that function's docstring for why such a bucket is still shown, not
  // hidden). "custom-adhoc" never gets its raw URL rendered here either -
  // the server already withheld it from `name`/`type` for exactly that
  // reason; this function has no more of it to work with than that.
  function bucketDisplayLabel(bucket) {
    if (bucket.kind === 'all') return bucket.name || 'All Pre-Configured Datasets (combined)';
    if (bucket.available && bucket.name) return bucket.name;
    if (bucket.kind === 'preset') return 'Unavailable preset';
    if (bucket.kind === 'custom') return 'Unavailable connection';
    if (bucket.kind === 'custom-adhoc') return 'Unsaved custom connection';
    return 'Unknown connection';
  }

  function renderChatHistoryBucketList(buckets) {
    if (!chatHistoryBucketList) return;
    chatHistoryBucketList.innerHTML = '';
    if (deleteAllChatHistoryBtn) deleteAllChatHistoryBtn.disabled = buckets.length === 0;

    if (buckets.length === 0) {
      const li = document.createElement('li');
      li.className = 'chat-history-bucket-row chat-history-bucket-row--empty text-center text-muted py-8';
      li.textContent = 'No saved conversations yet.';
      chatHistoryBucketList.appendChild(li);
      return;
    }

    // "all" first (a global, not-really-a-database bucket), then every
    // resolvable database alphabetically by name, then unresolvable/
    // orphaned buckets last, grouped together rather than interleaved -
    // there's no name to alphabetize THEM by, and they're the least
    // important entries here.
    const sorted = [...buckets].sort((a, b) => {
      const rank = (x) => (x.kind === 'all' ? 0 : x.available ? 1 : 2);
      const rankDiff = rank(a) - rank(b);
      if (rankDiff !== 0) return rankDiff;
      return (a.name || '').localeCompare(b.name || '') || a.bucket_key.localeCompare(b.bucket_key);
    });

    sorted.forEach((bucket) => {
      const li = document.createElement('li');
      li.className = 'chat-history-bucket-row';
      if (!bucket.available) li.classList.add('chat-history-bucket-row--unavailable');

      const info = document.createElement('div');
      info.className = 'chat-history-bucket-info';

      // Built with textContent, never innerHTML - bucket.name for a
      // "custom" kind is a user-supplied connection name (whichever user
      // saved it), not something this app generated.
      const nameEl = document.createElement('span');
      nameEl.className = 'chat-history-bucket-name';
      nameEl.textContent = bucketDisplayLabel(bucket);
      info.appendChild(nameEl);

      if (bucket.type) {
        const typeEl = document.createElement('span');
        typeEl.className = 'chat-history-bucket-type';
        typeEl.textContent = bucket.type;
        info.appendChild(typeEl);
      }

      const countEl = document.createElement('span');
      countEl.className = 'chat-history-bucket-count';
      countEl.textContent = `${bucket.turn_count} turn${bucket.turn_count === 1 ? '' : 's'}`;
      info.appendChild(countEl);

      li.appendChild(info);

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'btn chat-history-bucket-delete-btn';
      deleteBtn.textContent = 'Delete';
      deleteBtn.dataset.bucketKey = bucket.bucket_key;
      li.appendChild(deleteBtn);

      chatHistoryBucketList.appendChild(li);
    });
  }

  async function loadChatHistorySummary() {
    if (!chatHistoryBucketList) return;
    chatHistoryBucketList.innerHTML = '<li class="chat-history-bucket-row chat-history-bucket-row--empty text-center text-muted py-8">Loading...</li>';
    if (deleteAllChatHistoryBtn) deleteAllChatHistoryBtn.disabled = true;

    try {
      const response = await fetch('/api/chat-history/summary', { headers: getApiHeaders(), credentials: 'same-origin' });
      const data = await response.json();

      if (response.ok && data.success) {
        currentChatHistoryBuckets = data.buckets || [];
        renderChatHistoryBucketList(currentChatHistoryBuckets);
      } else {
        currentChatHistoryBuckets = [];
        const errMsg = response.status === 401
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to authenticate."
          : (data.error || `Server returned status ${response.status}`);
        chatHistoryBucketList.innerHTML = `
          <li class="chat-history-bucket-row chat-history-bucket-row--empty error-cell">
            <div class="error-container">
              <span class="error-icon">⚠️</span>
              <div class="error-details">
                <strong>Error Loading History</strong>
                <p>${errMsg}</p>
              </div>
            </div>
          </li>`;
      }
    } catch (err) {
      console.error("Failed to fetch chat history summary:", err);
      currentChatHistoryBuckets = [];
      chatHistoryBucketList.innerHTML = `
        <li class="chat-history-bucket-row chat-history-bucket-row--empty error-cell">
          <div class="error-container">
            <span class="error-icon">⚠️</span>
            <div class="error-details">
              <strong>Error Loading History</strong>
              <p>${err.message || "Failed to reach the backend service."}</p>
            </div>
          </div>
        </li>`;
    }
  }

  // Clears one bucket's saved turns server-side - just save_chat_bucket()
  // with an empty list (see /api/chat-history/save's own docstring; there's
  // no separate delete endpoint, "cleared" and "deleted" are the same
  // state here). Also evicts this bucket's own IN-MEMORY store if one
  // exists: without this, a bucket cleared here while some OTHER
  // connection is on screen would still show its old (now server-cleared)
  // turns if the user switched to it later this same page-load, since
  // reconcileActiveHistoryBucket()/getOrCreateBucketStore() reuse an
  // existing in-memory store rather than re-fetching it. If the CLEARED
  // bucket is the one currently active, also blanks the visible prompt/
  // SQL/results right away, same as restoring a genuinely empty bucket
  // already looks.
  async function clearChatHistoryBucket(bucketKeySuffix) {
    const response = await fetch('/api/chat-history/save', {
      method: 'POST',
      headers: getApiHeaders(),
      credentials: 'same-origin',
      body: JSON.stringify({ bucket_key: bucketKeySuffix, turns: [] }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) {
      throw new Error(data.error || `Server returned status ${response.status}`);
    }

    const identity = CURRENT_USER_IDENTITY || 'global';
    const fullKey = `${identity}::${bucketKeySuffix}`;
    const store = chatStoresByBucket.get(fullKey);
    if (store) store.clear();

    if (fullKey === activeBucketKey) {
      restoreLatestTurn();
      updateHistoryTurnsSubtitle();
    }
  }

  function setHistoryActionMsg(text, isError) {
    const msgEl = document.getElementById('historyActionMsg');
    if (!msgEl) return;
    msgEl.textContent = text;
    msgEl.style.color = isError ? 'var(--danger, #f87171)' : 'var(--primary, #10b981)';
  }

  if (chatHistoryBucketList) {
    chatHistoryBucketList.addEventListener('click', async (e) => {
      const btn = e.target.closest('.chat-history-bucket-delete-btn');
      if (!btn) return;
      const bucketKey = btn.dataset.bucketKey;
      const bucket = currentChatHistoryBuckets.find((b) => b.bucket_key === bucketKey);
      const label = bucket ? bucketDisplayLabel(bucket) : 'this database';

      // Fired on the click itself, before the confirm dialog - same
      // "measure intent, not just follow-through" posture the old
      // history_purge_clicked event had.
      trackEvent('chat_history_delete_clicked', bucket ? { kind: bucket.kind, turn_count: bucket.turn_count } : {});

      const confirmed = await showConfirmDialog(`Delete all saved turns for "${label}"? This cannot be undone.`);
      if (!confirmed) return;

      btn.disabled = true;
      try {
        await clearChatHistoryBucket(bucketKey);
        setHistoryActionMsg('Deleted successfully.', false);
        await loadChatHistorySummary();
      } catch (err) {
        console.error('Failed to clear chat history bucket:', err);
        setHistoryActionMsg(err.message || 'Failed to delete.', true);
        btn.disabled = false;
      }
    });
  }

  if (deleteAllChatHistoryBtn) {
    deleteAllChatHistoryBtn.addEventListener('click', async () => {
      if (currentChatHistoryBuckets.length === 0) return;
      const bucketCount = currentChatHistoryBuckets.length;
      const turnCount = currentChatHistoryBuckets.reduce((sum, b) => sum + b.turn_count, 0);

      trackEvent('chat_history_delete_all_clicked', { bucket_count: bucketCount, turn_count: turnCount });

      const confirmed = await showConfirmDialog(`Delete ALL saved turns across every database (${bucketCount} in total)? This cannot be undone.`);
      if (!confirmed) return;

      deleteAllChatHistoryBtn.disabled = true;
      try {
        await Promise.all(currentChatHistoryBuckets.map((b) => clearChatHistoryBucket(b.bucket_key)));
        setHistoryActionMsg('Deleted successfully.', false);
        await loadChatHistorySummary();
      } catch (err) {
        console.error('Failed to clear all chat history:', err);
        setHistoryActionMsg(err.message || 'Failed to delete.', true);
        deleteAllChatHistoryBtn.disabled = false;
      }
    });
  }

  if (historyBtn && historyModal) {
    historyBtn.addEventListener('click', () => {
      trackEvent('history_viewed', {});
      updateHistoryTurnsSubtitle();
      historyModal.classList.remove('hidden');
      bringModalToFront(historyModal);
      loadChatHistorySummary();
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

  // Thumbs up/down feedback row - shown under any direct, no-table model
  // response: the Summary tab (see renderTableResult()'s own isText branch)
  // - both "all databases" mode's own Summary tab (triage's routing message
  // + Phase C's own answer) and single-connection mode's equivalent (see
  // prependSingleModeSummaryTab()) render the exact same
  // {isText:true, tabLabel:'Summary'} shape, so this one function covers
  // both - and a "*** NO SQL ***" conversational reply (see
  // renderNoSqlResponse() - the model's own direct answer instead of a
  // query, in either mode). Gated the same way reportButtonHtml() is -
  // nothing rendered at all when the server has no SMTP config for
  // /api/report-issue, since that's what actually delivers this feedback.
  //
  // Deliberately carries NO prompt/summary content of its own - by explicit
  // request, the prompt and the summary text are considered too sensitive
  // to ever leave the browser this way, so clicking either icon opens the
  // report modal in the same "just a free-text box, nothing pre-filled"
  // shape the header's own "Send Feedback" button already uses (see
  // REPORT_CATEGORY_CONFIG.summary_thumbs_up/summary_thumbs_down) - only
  // `category` and whatever the user types travel to the server.
  // Thumbs-up uses --primary (this app's own green), thumbs-down --danger
  // (red) - both SVGs use stroke="currentColor" specifically so that CSS
  // color (see .summary-feedback-btn--up/--down in style.css) actually
  // tints them, unlike an emoji glyph.
  function summaryFeedbackButtonsHtml() {
    if (!ISSUE_REPORTING_ENABLED) return '';
    return `
      <div class="summary-feedback-row">
        <span class="summary-feedback-label text-muted">Was this summary helpful?</span>
        <span class="summary-feedback-btns">
          <button type="button" class="summary-feedback-btn summary-feedback-btn--up" data-summary-feedback-trigger="up" title="This summary was helpful">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.72a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path></svg>
          </button>
          <button type="button" class="summary-feedback-btn summary-feedback-btn--down" data-summary-feedback-trigger="down" title="This summary was not helpful">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"></path></svg>
          </button>
        </span>
      </div>`;
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

  // In-browser column sort for a results table (see currentTableSortState's
  // own comment for the overall design - purely client-side, no server call,
  // nothing persisted). Everything below this comment and above
  // renderTableResult() supports that one feature.

  // Looks at a sample of this column's own actual values (not any
  // server-declared SQL type - none of the four supported result shapes
  // carry one this far) to decide how clicking its header should sort:
  // 'number' and 'datetime' both default to DESC (see
  // handleSortableColumnClick()), everything else ('string', including
  // booleans and anything ambiguous) defaults to ASC. Only ever called when
  // result.rows has at least 2 rows (see isSortable in renderTableResult()),
  // so there's always at least one value to sample.
  function classifySortableColumnType(rows, col) {
    const SAMPLE_LIMIT = 25;
    let sampleCount = 0;
    let numericCount = 0;
    let dateCount = 0;
    // Matches backends/base.py's normalize_cell_value() output shapes:
    // real dates/timestamps arrive as ISO-ish strings (isoformat()), never
    // as a JS Date - so a plain numeric string ("2024") must be checked
    // for FIRST, or every 4-digit year-like number would misclassify as a
    // date column.
    const DATE_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
    for (let i = 0; i < rows.length && sampleCount < SAMPLE_LIMIT; i++) {
      const val = rows[i][col];
      if (val === null || val === undefined || val === '') continue;
      sampleCount++;
      if (typeof val === 'number') {
        numericCount++;
        continue;
      }
      if (typeof val !== 'string') continue; // booleans/objects - counted as sampled, not numeric/date, so they pull toward 'string'
      if (/^-?\d+(\.\d+)?$/.test(val)) {
        numericCount++;
      } else if (DATE_RE.test(val) && !isNaN(Date.parse(val))) {
        dateCount++;
      }
    }
    if (sampleCount === 0) return 'string';
    if (numericCount / sampleCount >= 0.8) return 'number';
    if (dateCount / sampleCount >= 0.8) return 'datetime';
    return 'string';
  }

  // NULL/empty always sort to the bottom regardless of direction (handled by
  // the caller's comparator wrapper, not here) - this only orders two actual
  // values against each other.
  function compareSortableValues(a, b, colType) {
    if (colType === 'number') {
      const an = typeof a === 'number' ? a : parseFloat(a);
      const bn = typeof b === 'number' ? b : parseFloat(b);
      if (Number.isNaN(an) && Number.isNaN(bn)) return 0;
      if (Number.isNaN(an)) return 1;
      if (Number.isNaN(bn)) return -1;
      return an - bn;
    }
    if (colType === 'datetime') {
      const at = Date.parse(a);
      const bt = Date.parse(b);
      return at - bt;
    }
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
  }

  // Sorts a NEW array - never mutates result.rows in place. That array is
  // also what a chart (Table/Chart toggle, above) would render if the user
  // switches views, and a chart - a time series in particular - generally
  // depends on its own natural row order, so a table-only sort must not
  // silently reorder it out from under the chart.
  function sortRowsByColumn(rows, col, colType, direction) {
    const withIndex = rows.map((row, idx) => ({ row, idx }));
    withIndex.sort((a, b) => {
      const av = a.row[col];
      const bv = b.row[col];
      const aEmpty = av === null || av === undefined || av === '';
      const bEmpty = bv === null || bv === undefined || bv === '';
      if (aEmpty && bEmpty) return a.idx - b.idx; // stable
      if (aEmpty) return 1; // NULLs last, regardless of direction
      if (bEmpty) return -1;
      const cmp = compareSortableValues(av, bv, colType);
      if (cmp !== 0) return direction === 'asc' ? cmp : -cmp;
      return a.idx - b.idx; // stable tie-break
    });
    return withIndex.map((entry) => entry.row);
  }

  // Builds one <tr> of data cells - shared by renderTableResult()'s initial
  // render and handleSortableColumnClick()'s re-render below, so the two
  // never drift apart on cell markup.
  function buildResultDataRow(columns, row) {
    const tr = document.createElement('tr');
    tr.classList.add('result-data-row');
    columns.forEach((col) => {
      const td = document.createElement('td');
      const val = row[col];
      td.textContent = val !== null && val !== undefined ? val : 'NULL';
      td.classList.add('cell-multiline');
      if (val === null || val === undefined) td.classList.add('text-null');
      tr.appendChild(td);
    });
    return tr;
  }

  // Updates every sortable header's arrow indicator to reflect which column
  // (if any) is currently sorted - called once right after a sort so
  // exactly one header shows an arrow at a time.
  function updateSortIndicatorArrows(sortedColIndex, direction) {
    if (!resultsHeader) return;
    const headerCells = resultsHeader.querySelectorAll('th.sortable-col');
    headerCells.forEach((th, idx) => {
      const arrow = th.querySelector('.sortable-col-arrow');
      if (!arrow) return;
      th.classList.toggle('sortable-col--active', idx === sortedColIndex);
      arrow.textContent = idx === sortedColIndex ? (direction === 'asc' ? '▲' : '▼') : '';
    });
  }

  // Click handler for a sortable column header (see renderTableResult()'s
  // isSortable branch, which wires this up). Re-sorts and re-renders just
  // this table's own data rows in place - never touches result.rows itself
  // (see sortRowsByColumn()'s own comment on why), never calls the server,
  // and nothing here is written anywhere persistent: currentTableSortState
  // is reset to null at the top of every renderTableResult() call, so this
  // is purely a same-tab, same-render convenience.
  function handleSortableColumnClick(result, colIndex, colType) {
    if (!resultsBody || !result || !result.rows || result.rows.length === 0) return;
    const col = result.columns[colIndex];
    let direction;
    if (currentTableSortState && currentTableSortState.result === result && currentTableSortState.colIndex === colIndex) {
      // Second (or later) click on the SAME header - toggle.
      direction = currentTableSortState.direction === 'asc' ? 'desc' : 'asc';
    } else {
      // First click on this header (or a click on a different header) -
      // the type default: ASC for strings, DESC for numbers/datetimes.
      direction = (colType === 'number' || colType === 'datetime') ? 'desc' : 'asc';
    }
    currentTableSortState = { result, colIndex, direction };

    const sortedRows = sortRowsByColumn(result.rows, col, colType, direction);

    // Replace only the actual data rows - a notices row (if any) sits above
    // them and a report-issue row (if any) sits below them, in the same
    // #resultsBody <tbody>, and both need to stay exactly where they are.
    const anchor = resultsBody.querySelector('tr.report-issue-row');
    const fragment = document.createDocumentFragment();
    sortedRows.forEach((row) => fragment.appendChild(buildResultDataRow(result.columns, row)));
    resultsBody.querySelectorAll('tr.result-data-row').forEach((tr) => tr.remove());
    if (anchor) {
      resultsBody.insertBefore(fragment, anchor);
    } else {
      resultsBody.appendChild(fragment);
    }

    updateSortIndicatorArrows(colIndex, direction);
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

    // Same "reset first, let only the one branch that needs it turn it back
    // on" posture as setReportContext(null) just above - only the
    // successful, non-empty tabular branch at the very bottom of this
    // function ever shows the toggle/chart at all (see this feature's own
    // section comment above requestSingleModeResultsSummary()), so every
    // other branch (isPending/isText/isError/no-dataset/0-rows) simply
    // inherits this hidden-table-wrapper-visible, chart-destroyed default.
    if (resultsViewToggle) resultsViewToggle.classList.add('hidden');
    if (resultsChartWrapper) resultsChartWrapper.classList.add('hidden');
    if (resultsTableWrapper) resultsTableWrapper.classList.remove('hidden');
    destroyResultsChart();
    // Same reset-first posture as the toggle/chart-wrapper lines just
    // above - only the successful, non-empty tabular branch below ever
    // turns this back on, and only when THIS tab's own result was
    // actually truncated; every other branch (and every other tab) must
    // not inherit a previous tab's notice.
    if (resultsTruncatedNotice) resultsTruncatedNotice.classList.add('hidden');
    // Column sort (see currentTableSortState's own comment) - every fresh
    // render (a tab switch, a new query, a re-execute) starts unsorted;
    // only clicks on this render's own header accumulate toggle state.
    currentTableSortState = null;

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

      // Thumbs up/down feedback on the SUMMARY tab specifically (never a
      // per-database "Note" tab) - see summaryFeedbackButtonsHtml()'s own
      // docstring. Rendered directly UNDER the summary text (not as a
      // heading above it), in both "all databases" mode (this tab's
      // routing message + Phase C answer) and single-connection mode (see
      // prependSingleModeSummaryTab()) - both build the exact same
      // {isText:true, tabLabel:'Summary', ...} shape, so one check here
      // covers both. Gated on `!result.summaryPending`: "all databases"
      // mode's Summary tab appears immediately with just triage's routing
      // message, well before Phase C's real answer has actually rendered
      // underneath it (see renderAllModeCombinedResults'/
      // startAllModeStreaming's own `summaryPending` comments and
      // appendPhaseCSummaryToSummaryTab/settleSummaryTabPending, which
      // clear it once Phase C has settled one way or another) - asking
      // "was this summary helpful" before there's even a Results Summary
      // to react to would be premature. Single-connection mode's own
      // Summary tab (prependSingleModeSummaryTab) is only ever created
      // already fully formed, so it never sets this flag at all -
      // `undefined` is falsy, so it's unaffected by this gate.
      // The chart-discoverability callout (see summaryChartCalloutHtml()'s
      // own docstring) - rendered whenever THIS turn has a chartable tab
      // somewhere, primary action first (directly under the summary text,
      // before the secondary thumbs-up/down feedback row), same reasoning
      // as the ordering of every other action in this tab. Gated on
      // `!result.summaryPending` for the same reason as the feedback row
      // above: "all databases" mode's Summary tab can render before Phase
      // C - and therefore before any tab's own visualization - has
      // actually arrived.
      if (result.tabLabel === 'Summary' && !result.summaryPending
        && currentResultsList && currentResultsList.some((r) => r && r.visualization)) {
        td.insertAdjacentHTML('beforeend', summaryChartCalloutHtml());
      }

      if (result.tabLabel === 'Summary' && !result.summaryPending) {
        td.insertAdjacentHTML('beforeend', summaryFeedbackButtonsHtml());
      }

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
      // result.notReportable (set only by executeSql()'s own bare-failure
      // branch, for a 401 - see that branch's comment) excludes the Report
      // button and skips setReportContext below, same as that branch's own
      // hand-rolled markup used to before it was unified onto this shared
      // renderer: an auth-required failure is out of scope for the Report
      // feature, the same way a translation error already is. Absent
      // (falsy) for every other isError entry (renderResultsWithFailed
      // Statement/renderResultsWithDatabaseFailures never set it), so this
      // is a no-op change for them.
      resultsBody.innerHTML = `
        <tr>
          <td class="error-cell">
            <div class="error-container">
              <span class="error-icon">⚠️</span>
              <div class="error-details">
                <div class="error-title-row">
                  <strong>Execution Error</strong>
                  ${result.notReportable ? '' : reportButtonHtml('error')}
                </div>
                ${dbNote}
                <p>${result.error || 'An error occurred during SQL execution.'}</p>
              </div>
            </div>
          </td>
        </tr>`;
      if (!result.notReportable) {
        setReportContext({
          category: 'error',
          databaseName: result.database && result.database.name,
          sql: result.statement || result.query || result.sql || '',
          content: result.error || '',
        });
      }
      // Tracked regardless of notReportable - a 401 auth failure is out of
      // scope for the Report feature (see above), but it's still an error
      // the user actually saw.
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

    // Only worth making headers clickable when there's actually more than
    // one row to reorder - classifySortableColumnType() samples result.rows,
    // so it needs that array to exist and be non-empty anyway.
    const isSortable = !!(result.rows && result.rows.length > 1);

    if (result.columns && result.columns.length > 0) {
      result.columns.forEach((col, colIndex) => {
        const th = document.createElement('th');
        if (isSortable) {
          // Sortable column header (see handleSortableColumnClick() below) -
          // classified once per column from a sample of this result's own
          // rows, not from any server-provided type, since the row-cap/
          // truncation feature above is the only place a backend's own
          // column typing already got threaded this far, and even that's
          // SQL-dialect column names, not JS-usable type tags.
          const colType = classifySortableColumnType(result.rows, col);
          th.classList.add('sortable-col');
          th.title = 'Click to sort';
          const labelSpan = document.createElement('span');
          labelSpan.className = 'sortable-col-label';
          labelSpan.textContent = col;
          const arrowSpan = document.createElement('span');
          arrowSpan.className = 'sortable-col-arrow';
          th.appendChild(labelSpan);
          th.appendChild(arrowSpan);
          th.addEventListener('click', () => {
            handleSortableColumnClick(result, colIndex, colType);
          });
        } else {
          th.textContent = col;
        }
        resultsHeader.appendChild(th);
      });
    }

    if (result.rows && result.rows.length > 0) {
      result.rows.forEach(row => {
        const tr = buildResultDataRow(result.columns, row);
        resultsBody.appendChild(tr);
      });
      setReportContext({
        category: 'wrong_result',
        databaseName: reportDatabaseName,
        sql: reportSql,
        content: (hasNotices ? result.notices.join('\n') + '\n\n' : '') + summarizeTabularResultForReport(result),
      });
      resultsBody.insertAdjacentHTML('beforeend', reportButtonRowHtml('wrong_result', result.columns.length));

      // Truncation notice (see resultsTruncatedNotice's own comment near
      // its DOM ref, and backends/base.py's EXECUTE_RESULTS_MAX_ROWS) -
      // never silent: a result cut off at the cap is shown as exactly
      // that, not as if it were the complete answer.
      if (result.truncated && resultsTruncatedNotice) {
        resultsTruncatedNotice.textContent =
          `⚠️ Showing the first ${result.rowCount.toLocaleString()} rows only — this query matched more rows than that.`;
        resultsTruncatedNotice.classList.remove('hidden');
      }

      // Table/Chart toggle (see this feature's own section comment above
      // requestSingleModeResultsSummary()) - only ever appears for the one
      // result this turn's LLM call decided (and the server re-validated)
      // was chartable; every other successful result reaches this same
      // branch with `result.visualization` simply absent, and stays a
      // plain table, exactly as before this feature existed.
      if (result.visualization && resultsViewToggle && resultsChartWrapper && resultsTableWrapper && typeof Chart !== 'undefined') {
        resultsViewToggle.classList.remove('hidden');
        // Defaults to the chart view the LLM itself decided on - `false`
        // is the only way to land on the table instead, set exclusively by
        // the user's own toggle click (see setActiveResultChartView) and
        // never by anything server-side.
        const showChart = result.chartView !== false;
        resultsViewToggle.querySelectorAll('.results-view-toggle-btn').forEach((btn) => {
          btn.classList.toggle('active', (btn.dataset.view === 'chart') === showChart);
        });
        if (showChart) {
          resultsTableWrapper.classList.add('hidden');
          resultsChartWrapper.classList.remove('hidden');
          renderResultChart(result);
        }
      }
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
      // Chart discoverability (see summaryChartCalloutHtml()'s own
      // docstring above): a subtle, persistent tag on whichever tab
      // actually carries a validated visualization, so a user who never
      // clicks the Summary tab's callout - or comes back to this turn
      // later - can still tell at a glance from the tab strip alone.
      const isChartable = !!res.visualization;
      btn.className = `result-tab-btn ${idx === activeResultIndex ? 'active' : ''} ${isError ? 'result-tab-btn--error' : ''} ${isPending ? 'result-tab-btn--pending' : ''} ${isChartable ? 'result-tab-btn--chartable' : ''}`.trim();

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
        // A "+" after the count is the tab strip's own half of the
        // truncation signal (see EXECUTE_RESULTS_MAX_ROWS/fetch_capped_rows
        // in backends/base.py, and renderTableResult()'s own banner for the
        // other half) - makes it visible even before the user opens this
        // tab, and even if they never read the banner inside it.
        const rowLabel = res.truncated
          ? `${count.toLocaleString()}+ rows`
          : (count === 1 ? '1 row' : `${count} rows`);
        // The chart badge is appended to the label text itself (not just
        // the `.result-tab-btn--chartable` color class) so it survives
        // being read as plain text (title attribute, screen readers,
        // narrow layouts that might otherwise strip a background tint).
        btn.textContent = `${dbLabel}Query ${idx + 1} (${rowLabel})${isChartable ? ' 📊' : ''}`;
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
  //
  // The marked line does NOT always have a body underneath it - a fixed,
  // single-sentence apology (e.g. translate_routes.py's own
  // _TRIAGE_FAILURE_TEXT, "I am not able to respond to your prompt.", used
  // when all-mode triage's response couldn't be parsed at all) is marked
  // by its caller exactly like any other block (see renderNoSqlResponse()),
  // but is just one line with nothing after it - no "\n\n" for the "body
  // follows" branch below to find. The regex used to require that blank
  // line unconditionally, so a label-only block like this never matched at
  // all: the marker's NUL characters (invisible once actually rendered in
  // a browser) were left behind in the output, and the literal word
  // "SUMMARY_BLOCK" showed up glued directly onto the apology text with no
  // space - a real bug report ("SUMMARY_BLOCKI am not able to respond to
  // your prompt."), root-caused and reproduced against this exact string
  // before this fix. Matching `(\n[ \t]*\n|\n?$)` after the label - a real
  // blank line (body follows), OR just running out of string (at most one
  // trailing newline, no body) - covers both shapes with one regex; which
  // alternative matched is what `hasBody` below distinguishes, so the
  // blank-line separator is only re-inserted into the output when there
  // is actually a body underneath it to separate from.
  function renderMarkdownLiteSummaryTab(rawText) {
    let html = escapeHtml(rawText || '');
    html = html.replace(
      new RegExp(SUMMARY_TAB_BLOCK_MARKER + '[ \\t]*([^\\n]+)(\\n[ \\t]*\\n|\\n?$)', 'g'),
      (_match, label, sep) => {
        const hasBody = /\n[ \t]*\n/.test(sep);
        return `<strong><u>${unwrapLabelEmphasis(label)}</u></strong>` + (hasBody ? '\n\n' : '');
      }
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
      // Same thumbs-up/down feedback prompt the Summary tab shows once
      // Phase C's real answer has rendered (see summaryFeedbackButtonsHtml()'s
      // own docstring) - a "*** NO SQL ***" reply IS the model's complete,
      // final answer the moment it renders (no later step patches it in
      // the way Phase C does), so unlike the Summary tab there's no
      // `summaryPending`-style gating needed here at all.
      td.insertAdjacentHTML('beforeend', summaryFeedbackButtonsHtml());
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
  function renderAllModeCombinedResults({ notes, executeResults, executeFailures, summaryPending }) {
    const routingMessage = notes && notes.routingMessage;
    const databaseNotes = (notes && notes.databaseNotes) || [];
    const generationFailures = (notes && notes.generationFailures) || [];

    // `summaryPending` is true only when THIS call's caller is about to
    // follow up with its own requestAllModeResultsSummary() call - i.e.
    // the batched (non-streaming) flows, where this render happens BEFORE
    // Phase C has run at all. It's left falsy (the default) for history
    // restoration and for the "nothing to execute" case, both of which
    // hand this function an already-final routingMessage/no-further-Phase-
    // C-call-coming text - see summaryFeedbackButtonsHtml()'s own gating
    // on `!result.summaryPending`.
    const summaryTab = routingMessage
      ? [{ isText: true, tabLabel: 'Summary', text: SUMMARY_TAB_BLOCK_MARKER + routingMessage, summaryPending: !!summaryPending }]
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
    // `sql` is included for the two shapes that actually had SQL generated
    // and (attempted to be) run for them - execute_routes.py tags every
    // real result with the exact statement that produced it (`.statement`)
    // and every execute failure with the one that failed
    // (`.failedStatement`) - so Phase C's prompt can show each database's
    // own SQL alongside its results/error (see translate_routes.py's
    // _build_summary_prompt: Gap 4 of "Turn History Handling in Datalect",
    // which previously had no SQL in Phase C's prompt at all). A note or
    // generation failure never had any SQL generated for it in the first
    // place, so those two entry shapes below carry no `sql` field, same as
    // they've never carried `columns`/`rows`.
    (Array.isArray(executeResults) ? executeResults : []).forEach((r) => {
      const db = r.database || {};
      entries.push({
        kind: db.kind, id: db.id, name: db.name || 'Unknown database',
        sql: r.statement || '',
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
        sql: f.failedStatement || f.statement || '',
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
    // The Results Summary section has now actually rendered - see
    // summaryFeedbackButtonsHtml()'s own gating on `!result.summaryPending`
    // for why this must stay true (hiding the thumbs-up/down prompt) right
    // up until this exact point, not from the moment the Summary tab first
    // appeared with only triage's routing message.
    summaryEntry.summaryPending = false;
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
    // See appendPhaseCSummaryToSummaryTab's identical line just above -
    // Phase C settled (with an apology instead of an answer, but settled
    // all the same), so the feedback prompt can appear now.
    summaryEntry.summaryPending = false;
    if (currentResultsList[activeResultIndex] === summaryEntry) {
      renderTableResult(summaryEntry);
    }
  }

  // Fire-and-await (not fire-and-forget - see the two call sites in
  // executeSql() below, both already inside an async flow with buttons
  // disabled) request for Phase C's summary. Best-effort: skipped only
  // when there's truly nothing to summarize - every database just noted
  // it had nothing relevant, with no real result AND no error from any of
  // them. A database that failed outright still gets summarized, same as
  // one that returned real data: _SUMMARY_SYSTEM_INSTRUCTION (translate_
  // routes.py) is explicitly asked to explain an error when it sees one,
  // not just acknowledge it, so skipping Phase C entirely whenever no
  // database happened to succeed would throw away exactly the case where
  // an explanation helps the user most - see the regression test covering
  // an all-databases turn where every connection errored out. A failure
  // from the endpoint itself is appended to the Summary tab via
  // appendPhaseCErrorToSummaryTab above (rather than left silent, as it
  // originally was) so the user can see why Phase C didn't produce a
  // summary - see /api/summarize-results' own docstring for the two
  // shapes `data.error` can take.
  //
  // /api/summarize-results streams NDJSON (readNdjsonStream(), same as
  // /api/translate) rather than returning one plain JSON body - its own
  // retry loop (_summarize_with_retry, see translate_routes.py) can take
  // several real seconds, and this is what makes that visible instead of
  // leaving the caller's "Summarizing results…" banner
  // (showAllModeSummarizingStatus(), already shown by every call site
  // before awaiting this) frozen with no indication anything is still
  // happening. showRetryStatus() is reused as-is - it already renders
  // generic "transient error, retrying" wording regardless of which
  // server-side call produced the event.
  //
  // Returns { databaseSummaries, crossDatabaseSummary } on success -
  // translate_routes.py's /api/summarize-results now additionally returns
  // these two structured fields alongside the plain joined `summary` this
  // function has always patched into the Summary tab (see that route's own
  // docstring: `database_summaries` is one {kind, id, name, text} entry per
  // in-scope database, `cross_database_summary` is the separate paragraph
  // spanning more than one database, or null). Every call site forwards
  // this straight into captureAllModeHistory() so it's recorded onto the
  // turn's history entry for a later chunk's per-database fan-out to use -
  // this chunk only records it, nothing here (or in captureAllModeHistory)
  // reads it back yet. Returns null for every case that already returned
  // nothing before this (skipped, aborted, or the request/LLM call itself
  // failed) - there's no structured summary to record in any of those.
  async function requestAllModeResultsSummary(notes, executeResults, executeFailures) {
    if (!notes || !notes.prompt) return null;
    const databaseResults = buildAllModeSummaryPayload(notes, executeResults, executeFailures);
    if (!databaseResults.some((e) => 'columns' in e || 'error' in e)) return null;

    try {
      const response = await fetch('/api/summarize-results', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({ prompt: notes.prompt, database_results: databaseResults }),
      });
      const data = await readNdjsonStream(response, (evt) => {
        if (evt.status === 'retrying') showRetryStatus(evt);
      });
      if (response.ok && data && data.success && data.summary) {
        // The server prefixes this the same "*** NO SQL ***" way any
        // other non-SQL LLM reply is (see translate_routes.py's
        // /api/summarize-results docstring) - an internal convention,
        // never meant to reach the user verbatim.
        appendPhaseCSummaryToSummaryTab(stripNoSqlPrefix(data.summary));
        return {
          databaseSummaries: Array.isArray(data.database_summaries) ? data.database_summaries : [],
          crossDatabaseSummary: data.cross_database_summary || null,
        };
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
        return null;
      }
      console.error('Failed to summarize all-mode results:', err);
    }
    return null;
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

  // Safety net for requestAllModeResultsSummary()'s own early-return cases
  // (nothing worth summarizing - every database noted/failed - or the
  // request itself errored/aborted) - none of those ever call
  // appendPhaseCSummaryToSummaryTab/appendPhaseCErrorToSummaryTab, so
  // without this the Summary tab's `summaryPending` flag would stay true
  // forever even though triage's routing message IS this turn's final,
  // unchanging Summary tab content at that point - permanently hiding the
  // "Was this summary helpful?" prompt for a turn that will never get a
  // real Phase C answer. Called after every requestAllModeResultsSummary()
  // call site, right alongside where each already re-reads
  // getSummaryTabEntry() to persist the (possibly unchanged) text into
  // history. No-ops if Phase C already cleared the flag itself.
  function settleSummaryTabPending() {
    const summaryEntry = getSummaryTabEntry();
    if (!summaryEntry || !summaryEntry.summaryPending) return;
    summaryEntry.summaryPending = false;
    if (currentResultsList[activeResultIndex] === summaryEntry) {
      renderTableResult(summaryEntry);
    }
  }

  // --- Charting: rendering a single-connection result as a Chart.js chart ---
  //
  // Single-connection mode's own post-execution summarization call (below)
  // now rides along a "visualization" decision from the same LLM call -
  // see translate_routes.py's _SINGLE_SUMMARY_SYSTEM_INSTRUCTION and
  // _clean_visualization for the server-side design/validation. By the
  // time a `{chart_type, x_column, y_columns, series_column}` object
  // reaches this file, it has ALREADY been validated against the real
  // executed result set server-side (real columns, real numeric values) -
  // this section trusts it at face value the same way client.js already
  // trusts any other server response shape, rather than re-validating it a
  // second time.
  //
  // Scope (matches the feature's own agreed v1 scope): single-connection
  // mode only. "All databases" mode's own Summary/Note/per-database tabs
  // (renderAllModeCombinedResults et al.) never carry a `.visualization`
  // field at all - summarize_all_mode_results (Phase C) doesn't compute
  // one - so nothing here needs an explicit guard against showing a chart
  // there; it simply never has anything to show.

  // In-flight Chart.js instance for whichever tab is currently showing a
  // chart - at most one at a time, since only the active tab is ever
  // rendered. Chart.js requires destroy()ing an old instance before
  // building a new one on the same <canvas>, or the two silently overlap.
  let resultsChartInstance = null;

  const CHART_MAX_SERIES = 12; // sane cap on `series_column` grouping - see buildResultsChartConfig()'s own comment.

  // Chart.js reads plain color VALUES at construction time, not live CSS
  // variables - it can't react to a theme switch on its own the way CSS
  // itself does (see setTheme()'s own call to rerenderActiveResultChartIfShowing()
  // above). Reading the app's own custom properties here (rather than a
  // separate hardcoded palette) keeps a chart's colors in lockstep with
  // whichever theme is currently active, light or dark, without this file
  // needing its own copy of either palette.
  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value && value.trim()) || fallback;
  }

  function getChartSeriesColors() {
    // --primary/--secondary/--accent-cyan/--warning/--danger plus each
    // color's own --*-hover shade - eight distinct hues before any repeat,
    // which comfortably covers CHART_MAX_SERIES without the same color
    // appearing twice in a typical chart.
    return [
      cssVar('--primary', '#10b981'), cssVar('--secondary', '#6366f1'), cssVar('--accent-cyan', '#38bdf8'),
      cssVar('--warning', '#f59e0b'), cssVar('--danger', '#f87171'), cssVar('--primary-hover', '#34d399'),
      cssVar('--secondary-hover', '#818cf8'),
    ];
  }

  function getChartAxisColors() {
    return { text: cssVar('--text-secondary', '#94a3b8'), grid: cssVar('--surface-3', 'rgba(148, 163, 184, 0.25)') };
  }

  const DUAL_Y_AXIS_RATIO = 5;

  // Multiple y_columns whose real magnitudes differ wildly (e.g. revenue in
  // the thousands charted alongside a unit count in the tens) squash the
  // smaller one flat against zero on a single shared axis before any chart
  // library even gets involved. Measured directly from THIS turn's real row
  // data (never the model's own say-so - same "trust the real data over
  // the model's own judgment" posture as _clean_visualization's own
  // server-side validation, just applied to an axis-layout decision
  // instead of a chartability decision): if the single biggest peak value
  // among all of `yColumns` is at least DUAL_Y_AXIS_RATIO times bigger than
  // a given column's own peak, that column is moved onto a second,
  // right-hand axis (Chart.js's own documented `yAxisID`/second-scale
  // pattern) instead of getting flattened against zero on the shared one.
  // A single y_column never triggers this - there's nothing to compare it
  // against - and `series_column` grouping (multiple categories of the
  // SAME measurement, e.g. one line per region) never does either, since
  // that only ever produces one y_column to begin with; this is strictly
  // about genuinely different measurements sharing a chart.
  function assignYAxisIds(yColumns, rows) {
    const axisIdByColumn = new Map();
    if (yColumns.length < 2) {
      yColumns.forEach((col) => axisIdByColumn.set(col, 'y'));
      return { axisIdByColumn, usesSecondAxis: false };
    }
    const peakByColumn = new Map(yColumns.map((col) => {
      let peak = 0;
      rows.forEach((r) => {
        const v = r[col];
        if (typeof v === 'number' && Number.isFinite(v)) peak = Math.max(peak, Math.abs(v));
      });
      return [col, peak];
    }));
    const overallPeak = Math.max(...peakByColumn.values(), 0);
    yColumns.forEach((col) => {
      const peak = peakByColumn.get(col);
      const ratio = peak > 0 ? overallPeak / peak : Infinity;
      axisIdByColumn.set(col, ratio >= DUAL_Y_AXIS_RATIO ? 'y1' : 'y');
    });
    const usesSecondAxis = yColumns.some((col) => axisIdByColumn.get(col) === 'y1')
      && yColumns.some((col) => axisIdByColumn.get(col) === 'y');
    if (!usesSecondAxis) {
      // The split didn't produce two non-empty groups (e.g. every column
      // read back as all-zero/non-numeric this turn) - fall back to the
      // single shared axis rather than stranding every column alone on
      // 'y1' with nothing on the primary axis at all.
      yColumns.forEach((col) => axisIdByColumn.set(col, 'y'));
    }
    return { axisIdByColumn, usesSecondAxis };
  }

  // Removes duplicates from `values` while keeping first-seen order (unlike
  // a plain Set/sort, which would either lose order or impose one the data
  // never had) - used to build a chart's x-axis categories and its list of
  // series values, both of which should read in the same order the actual
  // rows came back in.
  function uniqueValuesInOrder(values) {
    const seen = new Set();
    const result = [];
    values.forEach((v) => {
      const key = typeof v === 'object' ? JSON.stringify(v) : v;
      if (!seen.has(key)) {
        seen.add(key);
        result.push(v);
      }
    });
    return result;
  }

  // Builds a Chart.js config object ({type, data, options}) from one
  // executed result's real rows plus its validated `visualization`
  // decision. `chart_type` is always one of "bar"/"line"/"scatter" by this
  // point (_clean_visualization never lets anything else through).
  //
  // series_column (optional): when present, rows are grouped by that
  // column's value into one dataset per group instead of one flat dataset
  // per y_column - e.g. "sales over time, one line per region". Capped at
  // CHART_MAX_SERIES distinct values (an adversarial or just very
  // high-cardinality grouping column would otherwise produce an
  // unreadable/unusably slow chart) - extra series beyond the cap are
  // silently dropped rather than erroring, same "degrade gracefully"
  // posture the rest of this feature already takes toward imperfect LLM
  // output.
  function buildResultsChartConfig(result, viz) {
    const rows = result.rows || [];
    const colors = getChartSeriesColors();
    const axisColors = getChartAxisColors();
    let colorIndex = 0;
    const nextColor = () => colors[(colorIndex++) % colors.length];

    const seriesValues = viz.series_column
      ? uniqueValuesInOrder(rows.map((r) => r[viz.series_column])).slice(0, CHART_MAX_SERIES)
      : [undefined];
    const multiSeries = seriesValues.length > 1;
    const multiY = viz.y_columns.length > 1;
    const seriesLabel = (yCol, seriesVal) => {
      if (multiSeries && multiY) return `${yCol} (${seriesVal})`;
      if (multiSeries) return String(seriesVal);
      return yCol;
    };
    // Which y_columns (if any) get split onto a second, right-hand axis -
    // see assignYAxisIds()'s own docstring. `primaryColumns`/
    // `secondaryColumns` partition viz.y_columns rather than re-filtering
    // it repeatedly below.
    const { axisIdByColumn, usesSecondAxis } = assignYAxisIds(viz.y_columns, rows);
    const primaryColumns = viz.y_columns.filter((c) => axisIdByColumn.get(c) === 'y');
    const secondaryColumns = viz.y_columns.filter((c) => axisIdByColumn.get(c) === 'y1');
    // An axis title: unambiguous (just the one column name) when there's
    // only one column on that axis. With more than one, the legend (shown
    // whenever multiY - see commonOptions.plugins.legend below) is what
    // actually distinguishes them per-dataset; the axis title here just
    // names the shared measurement(s) plotted on it, joined rather than
    // picking just the first and silently dropping the rest.
    const axisTitleFor = (cols) => (cols.length === 1 ? cols[0] : cols.join(' / '));
    const yAxisTitle = axisTitleFor(primaryColumns);
    const y1AxisTitle = usesSecondAxis ? axisTitleFor(secondaryColumns) : null;

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false, // instant redraw on tab switch/theme toggle instead of a distracting re-animate
      plugins: { legend: { display: multiSeries || multiY, labels: { color: axisColors.text } } },
    };

    if (viz.chart_type === 'scatter') {
      // Scatter plots pairs of real numeric values directly - no shared
      // x-axis category list to pivot onto, unlike bar/line below - so
      // each series is just that group's own rows mapped to {x, y} points.
      const datasets = [];
      seriesValues.forEach((seriesVal) => {
        const seriesRows = viz.series_column ? rows.filter((r) => r[viz.series_column] === seriesVal) : rows;
        viz.y_columns.forEach((yCol) => {
          const points = seriesRows
            .map((r) => ({ x: r[viz.x_column], y: r[yCol] }))
            .filter((p) => typeof p.x === 'number' && typeof p.y === 'number');
          const color = nextColor();
          datasets.push({
            label: seriesLabel(yCol, seriesVal), data: points, backgroundColor: color, borderColor: color,
            yAxisID: axisIdByColumn.get(yCol),
          });
        });
      });
      return {
        type: 'scatter',
        data: { datasets },
        options: {
          ...commonOptions,
          scales: {
            x: { title: { display: true, text: viz.x_column, color: axisColors.text }, ticks: { color: axisColors.text }, grid: { color: axisColors.grid } },
            y: { title: { display: true, text: yAxisTitle, color: axisColors.text }, position: 'left', ticks: { color: axisColors.text }, grid: { color: axisColors.grid } },
            // Only present when the real data actually calls for it (see
            // assignYAxisIds) - its own grid is suppressed
            // (drawOnChartArea: false) so it doesn't draw a second,
            // misaligned set of gridlines over the primary axis's own.
            ...(usesSecondAxis ? { y1: {
              type: 'linear', position: 'right',
              title: { display: true, text: y1AxisTitle, color: axisColors.text },
              ticks: { color: axisColors.text }, grid: { drawOnChartArea: false },
            } } : {}),
          },
        },
      };
    }

    // bar/line: a shared, deduped x-axis of category labels, with each
    // dataset's values looked up per label (a row missing from a given
    // series/label combination - e.g. a region with no data for one day -
    // becomes a `null` point, which Chart.js simply skips/gaps over via
    // spanGaps rather than plotting as a false zero).
    const xLabels = uniqueValuesInOrder(rows.map((r) => r[viz.x_column]));
    const lookup = new Map();
    rows.forEach((r) => {
      lookup.set(`${r[viz.x_column]}␟${viz.series_column ? r[viz.series_column] : ''}`, r);
    });
    const datasets = [];
    seriesValues.forEach((seriesVal) => {
      viz.y_columns.forEach((yCol) => {
        const data = xLabels.map((x) => {
          const row = lookup.get(`${x}␟${viz.series_column ? seriesVal : ''}`);
          const value = row ? row[yCol] : null;
          return typeof value === 'number' ? value : null;
        });
        const color = nextColor();
        const axisId = axisIdByColumn.get(yCol);
        datasets.push(
          viz.chart_type === 'line'
            ? { label: seriesLabel(yCol, seriesVal), data, borderColor: color, backgroundColor: color, tension: 0.15, spanGaps: true, yAxisID: axisId }
            : { label: seriesLabel(yCol, seriesVal), data, backgroundColor: color, yAxisID: axisId }
        );
      });
    });
    return {
      type: viz.chart_type,
      data: { labels: xLabels, datasets },
      options: {
        ...commonOptions,
        scales: {
          x: { title: { display: true, text: viz.x_column, color: axisColors.text }, ticks: { color: axisColors.text }, grid: { color: axisColors.grid } },
          y: { title: { display: true, text: yAxisTitle, color: axisColors.text }, position: 'left', ticks: { color: axisColors.text }, grid: { color: axisColors.grid }, beginAtZero: true },
          // Only present when the real data actually calls for it (see
          // assignYAxisIds) - its own grid is suppressed
          // (drawOnChartArea: false) so it doesn't draw a second,
          // misaligned set of gridlines over the primary axis's own.
          ...(usesSecondAxis ? { y1: {
            type: 'linear', position: 'right',
            title: { display: true, text: y1AxisTitle, color: axisColors.text },
            ticks: { color: axisColors.text }, grid: { drawOnChartArea: false }, beginAtZero: true,
          } } : {}),
        },
      },
    };
  }

  function destroyResultsChart() {
    if (resultsChartInstance) {
      resultsChartInstance.destroy();
      resultsChartInstance = null;
    }
  }

  // Draws `result.visualization` onto #resultsChartCanvas. Safe to call
  // only when both the canvas element and the Chart.js library itself are
  // actually available - see renderTableResult()'s own guard, which is the
  // only call site (plus rerenderActiveResultChartIfShowing() below, for a
  // theme switch).
  function renderResultChart(result) {
    if (!resultsChartCanvas || typeof Chart === 'undefined' || !result || !result.visualization) return;
    destroyResultsChart();
    const config = buildResultsChartConfig(result, result.visualization);
    resultsChartInstance = new Chart(resultsChartCanvas.getContext('2d'), config);
  }

  // Re-draws the currently active tab's chart in place (new colors only,
  // same data) - a no-op whenever the active tab either has no
  // visualization at all or is currently showing its Table view instead.
  // Called from setTheme() so a live theme switch doesn't leave an
  // already-open chart showing the OLD theme's colors until the user
  // switches tabs and back.
  function rerenderActiveResultChartIfShowing() {
    const result = currentResultsList && currentResultsList[activeResultIndex];
    if (result && result.visualization && result.chartView !== false) {
      renderResultChart(result);
    }
  }

  // Finds, among `list`, the one result entry that visualization's own
  // x_column/y_columns actually belong to, and tags it with `.visualization` -
  // mirrors _pick_chartable_result/_clean_visualization's own server-side
  // matching (there is, by construction, at most one such entry: the
  // single chartable result _pick_chartable_result identified for this
  // turn - see translate_routes.py's own docstring on that function).
  // No-op when `visualization` is null (nothing to attach) or `list` isn't
  // an array (e.g. a bare {error} entry list, which never has `.columns`
  // to match against anyway - this would already no-op via `.find`
  // finding nothing, this guard just skips the work).
  //
  // Called on BOTH the live, on-screen currentResultsList entries AND the
  // separate `summarizedResults` copy executeSql() persists onto the turn
  // (see summarizeResultForHistory()) - two different sets of objects
  // built from the same underlying rows, so the same visualization object
  // is attached to each independently, by each call's own caller.
  function attachVisualizationToResultsList(list, visualization) {
    if (!visualization || !Array.isArray(list)) return;
    const needed = [visualization.x_column, ...visualization.y_columns];
    const match = list.find((r) => r && Array.isArray(r.columns) && needed.every((c) => r.columns.includes(c)));
    if (match) match.visualization = visualization;
  }

  // Keeps the CURRENT turn's own persisted copy (chatStore.lastTurn()'s
  // modelEntry.results - a separate set of objects from currentResultsList,
  // see attachVisualizationToResultsList's own comment) in sync with a
  // live tab's chartView, so a choice survives stepping back and forward
  // through history (chatStore's undo()/redo()) without needing a fresh
  // execution to re-derive it. Matched by object identity: both copies
  // were tagged with the exact same `visualization` object reference (see
  // executeSql()'s own call sites), so this is exact, not a guess. Shared
  // by setActiveResultChartView() (the manual toggle) and
  // jumpToChartableResultTab() (the Summary tab's own callout) below,
  // rather than duplicated between them.
  function syncPersistedChartView(entry, showChart) {
    const turn = chatStore.lastTurn();
    const persistedResults = turn && turn.modelEntry && Array.isArray(turn.modelEntry.results)
      ? turn.modelEntry.results : null;
    if (persistedResults) {
      const persistedMatch = persistedResults.find((r) => r && r.visualization === entry.visualization);
      if (persistedMatch) {
        persistedMatch.chartView = showChart;
        // This turn was already pushed (and already saved server-side)
        // before this toggle click - see chatStore.persistCurrent()'s own
        // docstring for why an in-place edit like this one needs its own
        // explicit re-save, or the server's copy never picks it up.
        chatStore.persistCurrent();
      }
    }
  }

  // Toggles the currently active tab between its Table and Chart views (see
  // renderTableResult()'s own toggle-button rendering, which wires this up
  // once at load - the buttons themselves are static markup, not
  // rebuilt per-render, unlike the tabs strip). No-op if the active
  // result has no visualization at all - the toggle row is hidden in that
  // case anyway, so this should never actually fire then.
  function setActiveResultChartView(showChart) {
    const result = currentResultsList && currentResultsList[activeResultIndex];
    if (!result || !result.visualization) return;
    result.chartView = showChart;
    syncPersistedChartView(result, showChart);
    renderTableResult(result);
  }

  if (resultsViewToggle) {
    resultsViewToggle.querySelectorAll('.results-view-toggle-btn').forEach((btn) => {
      btn.addEventListener('click', () => setActiveResultChartView(btn.dataset.view === 'chart'));
    });
  }

  // --- Chart discoverability: Summary tab callout + tab-strip badge ---
  //
  // The Summary tab becomes the active tab the instant it's created (see
  // prependSingleModeSummaryTab()) - the model's own answer is the first
  // thing shown. That's exactly the problem for charting: a chart sitting
  // on some OTHER, now-inactive tab is otherwise invisible unless the user
  // happens to click around the tab strip on their own. Two things fix
  // that, together: buildResultsTabsNav() below tags that tab's own label
  // with a small chart badge (persistently visible whenever the user DOES
  // look at the tab strip), and summaryChartCalloutHtml()/
  // jumpToChartableResultTab() here put an explicit, clickable nudge
  // directly under the Summary text itself - exactly where the user's
  // attention already is the moment it matters.

  // Jumps straight to whichever result tab in the CURRENT turn carries a
  // validated visualization, and makes sure it lands showing the chart
  // itself (not whatever Table/Chart state was left over from an earlier
  // visit) - that's the whole point of the click. No-op if nothing in
  // currentResultsList is chartable (shouldn't happen - the callout that
  // triggers this is only ever rendered when one exists - but a no-op is
  // cheaper than assuming that invariant always holds).
  function jumpToChartableResultTab() {
    if (!currentResultsList) return;
    const idx = currentResultsList.findIndex((r) => r && r.visualization);
    if (idx < 0) return;
    activeResultIndex = idx;
    const entry = currentResultsList[idx];
    entry.chartView = true;
    syncPersistedChartView(entry, true);
    buildResultsTabsNav();
    renderTableResult(entry);
  }

  // Rendered directly under the Summary tab's own text (see
  // renderTableResult()'s isText branch) whenever this turn's results
  // include a chartable tab. data-view-chart-trigger is handled by the
  // same delegated #resultsBody click listener as the Report/feedback
  // buttons below, for the same "rebuilt fresh on every render, so a
  // persistent per-element listener would never survive a re-render"
  // reason.
  function summaryChartCalloutHtml() {
    return `
      <div class="summary-chart-callout">
        <button type="button" class="summary-chart-callout-btn" data-view-chart-trigger>
          <span aria-hidden="true">📊</span> View as chart
        </button>
      </div>`;
  }

  // --- Single-connection mode's own post-execution results summarization ---
  //
  // The single-connection equivalent of "all databases" mode's Phase C
  // above (see requestAllModeResultsSummary/appendPhaseCSummaryToSummaryTab)
  // - once a single-connection turn's generated SQL has actually been
  // executed, a SEPARATE LLM call asks for a brief answer PLUS actionable
  // insight over the real, untruncated results (server-side: /api/
  // summarize-result, singular - see translate_routes.py's docstring on
  // that route for why it's untruncated, unlike Phase C). Presented as a
  // new LEADING "Summary" tab, same shape/label/marker convention as
  // Phase C's own Summary tab, per this feature's own confirmed design
  // (mirrors all-mode's Phase C presentation exactly).

  // Fire-and-await (see executeSql()'s call site - already inside an
  // async flow with buttons disabled). Best-effort, same posture as
  // requestAllModeResultsSummary: a failure here never fails the turn
  // itself. Returns `{summaryText, visualization}` on success - `summaryText`
  // is the SUMMARY_TAB_BLOCK_MARKER-prefixed summary text (server's own
  // "*** NO SQL ***" convention stripped first - see stripNoSqlPrefix), or
  // the server's own honest error text (also marked, so it reads the same
  // way a real summary would) on failure; `visualization` is the server's
  // already-validated {chart_type, x_column, y_columns, series_column}
  // decision (see translate_routes.py's _clean_visualization), or null
  // when this result set wasn't chartable/the model chose a table. Returns
  // bare `null` (not an object) when there's nothing to show at all
  // (abort, or no usable response) - every call site already guards on
  // truthiness before touching either field, so this asymmetry is safe.
  //
  // /api/summarize-result streams NDJSON the same way /api/summarize-
  // results does now (see that function's identical comment just above)
  // - every call site already shows showAllModeSummarizingStatus() before
  // awaiting this, so a live 'retrying' event just overwrites that same
  // banner with showRetryStatus(), same precedent.
  async function requestSingleModeResultsSummary(prompt, sql, results) {
    try {
      const response = await fetch('/api/summarize-result', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({ prompt: prompt, sql: sql, results: results }),
      });
      const data = await readNdjsonStream(response, (evt) => {
        if (evt.status === 'retrying') showRetryStatus(evt);
      });
      if (response.ok && data && data.success && data.summary) {
        return {
          summaryText: SUMMARY_TAB_BLOCK_MARKER + stripNoSqlPrefix(data.summary),
          visualization: data.visualization || null,
        };
      } else if (data && data.error) {
        return { summaryText: SUMMARY_TAB_BLOCK_MARKER + data.error, visualization: null };
      }
      return null;
    } catch (err) {
      // See requestAllModeResultsSummary's identical guard - an aborted
      // turn's UI has already moved on by the time this rejects.
      if (err && err.name === 'AbortError') {
        return null;
      }
      console.error('Failed to summarize single-connection results:', err);
      return null;
    }
  }

  // Prepends a leading "Summary" tab onto currentResultsList (already
  // built by renderMultiTurnResults() just before this is called) and
  // makes it the active tab - the "new leading Summary tab" placement
  // this feature's design confirmed. Safe to call with a falsy
  // `summaryText` (no-op), so callers don't need their own guard.
  function prependSingleModeSummaryTab(summaryText) {
    if (!summaryText || !currentResultsList) return;
    currentResultsList = [{ isText: true, tabLabel: 'Summary', text: summaryText }, ...currentResultsList];
    activeResultIndex = 0;
    buildResultsTabsNav();
    renderTableResult(currentResultsList[activeResultIndex]);
  }

  // Same idea as prependSingleModeSummaryTab just above, for a FAILED
  // execution (see executeSql()'s two single-connection failure branches)
  // - deliberately does NOT jump the active tab to the new Summary entry
  // the way the success-path helper above does. All-mode's own equivalent
  // (appendPhaseCSummaryToSummaryTab/appendPhaseCErrorToSummaryTab) never
  // disturbs whatever tab the user is currently looking at either - it
  // only re-renders if the Summary tab HAPPENS to already be active - and
  // the same reasoning applies here even more strongly: the tab the user
  // is looking at when this fires is the error itself, which is what
  // needs their attention. Silently swapping that out for an LLM-written
  // apology mid-read the moment the (often very fast, sometimes near-
  // instant) summarization call resolves would be a jarring, timing-
  // dependent surprise, not the helpful addition this feature is meant to
  // be - so this only grows the tab strip with a new (inactive) "Summary"
  // tab the user can click into if they want it, leaving the already-
  // rendered error exactly as it is. Safe to call with a falsy
  // `summaryText` (no-op), same as the function above.
  function prependSingleModeSummaryTabPreservingActiveTab(summaryText) {
    if (!summaryText || !currentResultsList) return;
    currentResultsList = [{ isText: true, tabLabel: 'Summary', text: summaryText }, ...currentResultsList];
    activeResultIndex += 1;
    buildResultsTabsNav();
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
  //
  // `notes.databaseSql` and `summaryResult` (new - see each call site) are
  // recorded here purely for a LATER chunk's use: splitting the SQL text
  // and the summary text per in-scope database, so a per-database turn can
  // eventually be pushed into each database's own single-connection-mode
  // history bucket. `notes.databaseSql` is translate_routes.py's own
  // `sql_blocks` (Chunk 1 - one {kind, id, name, sql} entry per database
  // that actually got real SQL, already threaded through onto `notes`
  // wherever it's built - see startAllModeStreaming()/maybeFinalize() and
  // translatePrompt()/executeSql()'s pendingAllModeNotes construction).
  // `summaryResult` is requestAllModeResultsSummary()'s own returned
  // {databaseSummaries, crossDatabaseSummary} (Chunk 2's structured Phase C
  // response), passed straight through by every call site right after
  // awaiting that call. Nothing in THIS chunk ever reads these three
  // fields back - restoreLatestTurn() still rebuilds the combined view
  // purely from routingMessage/databaseNotes/generationFailures/
  // executeFailures/results, exactly as before - so recording them here has
  // no effect on anything the user sees yet.
  function captureAllModeHistory(modelEntry, notes, executeFailures, summaryResult) {
    modelEntry.allMode = {
      routingMessage: (notes && notes.routingMessage) || null,
      databaseNotes: (notes && notes.databaseNotes) || [],
      generationFailures: (notes && notes.generationFailures) || [],
      executeFailures: executeFailures || [],
      databaseSql: (notes && notes.databaseSql) || [],
      databaseSummaries: (summaryResult && summaryResult.databaseSummaries) || [],
      crossDatabaseSummary: (summaryResult && summaryResult.crossDatabaseSummary) || null,
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

  // Looks up a specific database's own triage-rewritten question -
  // notes.connectionPrompts (new - see maybeFinalize()'s/translatePrompt()'s
  // pendingAllModeNotes' own construction) mirrors connection_selection's
  // own per-entry "prompt" field (translate_routes.py's entry_prompts -
  // that database's own rewrite when triage supplied one, else the
  // original cross-database question unchanged). Falls back to `notes.prompt`
  // (the turn's original question) for a `notes` object that predates this
  // field, or if this database somehow has no matching entry - the same
  // "never worse than what single-connection mode already had" fallback
  // entry_prompts itself uses server-side.
  function findDatabasePrompt(notes, kind, id) {
    const entries = (notes && notes.connectionPrompts) || [];
    const match = entries.find((e) => e.kind === kind && e.id === id);
    return (match && match.prompt) || (notes && notes.prompt) || '';
  }

  // Chunk 4 of "splitting SQL/summary per in-scope database" (see this
  // file's own multi-window design history - Chunks 1-3 recorded this same
  // structured per-database data onto the all-mode turn's OWN shared
  // history entry via captureAllModeHistory() above; this is what actually
  // fans it back OUT). For every in-scope database this turn produced a
  // real outcome for (sql+executed, note, or failed - a database that
  // still sits in an un-executed "Ready to execute" placeholder has no
  // outcome yet and is simply not in any of `notes`' three lists below),
  // reconstructs the exact single-connection-shaped
  // {prompt, text, results, summary} tuple that database would have
  // produced had the user asked it directly in single-connection mode
  // (see restoreLatestTurn()'s own three shapes - '*** NO SQL ***'-prefixed
  // text with no results/summary for note/failed, {text: sql, results:
  // [...], summary?} for a real execution, verified against that
  // function's actual reading behavior), and pushes it into that
  // database's own bucket via pushTurnIntoBucket() - which never touches
  // `chatStore`/`activeBucketKey` or re-renders anything, so this has zero
  // effect on whichever bucket is currently on screen (the all-mode shared
  // one very much included - that bucket already got its OWN turn from
  // captureAllModeHistory() above, unaffected by this).
  //
  // `notes` is the same shape every captureAllModeHistory() call site
  // already builds (routingMessage/databaseNotes/generationFailures/
  // databaseSql/connectionPrompts). `executeResults`/`executeFailures` are
  // this turn's raw (pre-summarizeResultForHistory) execute rows/failures,
  // each tagged with its own `.database` (see execute_routes.py/
  // settleAllModeBatchedResults) - summarizeResultForHistory (below in
  // this file, already hoisted - see this function's own placement
  // comment) is reused here unchanged to build each database's own
  // `results` entries, exactly as maybeFinalize()/executeSql() already use
  // it for the combined all-mode turn. `summaryResult` is
  // requestAllModeResultsSummary()'s own {databaseSummaries,
  // crossDatabaseSummary} (Chunk 2), or null when Phase C never ran (e.g.
  // a request that failed outright) - every per-database summary lookup
  // below already tolerates that.
  function fanOutAllModeHistoryPerDatabase(notes, executeResults, executeFailures, summaryResult) {
    const databaseNotes = (notes && notes.databaseNotes) || [];
    const generationFailures = (notes && notes.generationFailures) || [];
    const databaseSql = (notes && notes.databaseSql) || [];
    const databaseSummaries = (summaryResult && summaryResult.databaseSummaries) || [];
    const results = Array.isArray(executeResults) ? executeResults : [];
    const failures = Array.isArray(executeFailures) ? executeFailures : [];

    function findSummaryText(kind, id) {
      const match = databaseSummaries.find((s) => s.kind === kind && s.id === id);
      return (match && match.text) || undefined;
    }

    // "note" outcome - triage decided this database needed no SQL at all.
    databaseNotes.forEach((n) => {
      pushTurnIntoBucket(n.kind, n.id, findDatabasePrompt(notes, n.kind, n.id), {
        role: 'model',
        text: `*** NO SQL *** ${n.text || ''}`,
      });
    });

    // "failed" outcome - Phase B's own SQL generation call errored for
    // this database, so (like "note" above) nothing ever executed.
    generationFailures.forEach((f) => {
      pushTurnIntoBucket(f.kind, f.id, findDatabasePrompt(notes, f.kind, f.id), {
        role: 'model',
        text: `*** NO SQL *** ${f.error || 'Failed to generate SQL for this database.'}`,
      });
    });

    // "sql" (executed) outcome - one turn per database that actually got
    // real SQL, joining that database's own generated text (databaseSql)
    // with whichever of its own rows/errors came back (matched by the
    // same `.database` tag every other consumer in this file already
    // relies on) and its own Phase C paragraph, if Phase C ran.
    databaseSql.forEach((entry) => {
      const ownResults = results
        .filter((r) => r.database && r.database.kind === entry.kind && r.database.id === entry.id)
        .map(summarizeResultForHistory);
      const ownFailures = failures
        .filter((f) => f.database && f.database.kind === entry.kind && f.database.id === entry.id)
        .map(summarizeResultForHistory);
      const modelEntry = { role: 'model', text: entry.sql || '', results: [...ownResults, ...ownFailures] };
      const summaryText = findSummaryText(entry.kind, entry.id);
      if (summaryText) modelEntry.summary = summaryText;
      pushTurnIntoBucket(entry.kind, entry.id, findDatabasePrompt(notes, entry.kind, entry.id), modelEntry);
    });
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
    // GA fan-out tracking (see trackAllModeFanoutTranslate's own comment) -
    // one real translate call is about to happen (or has just been kicked
    // off server-side) per connection here, regardless of how each one
    // eventually turns out (sql/note/failed).
    trackAllModeFanoutTranslate(connectionSelection);
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
      // being parsed - see readNdjsonStream()'s docstring for why that
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

    // Always pending here (unlike renderAllModeCombinedResults' own
    // history-restoration/"nothing to execute" cases) - the live streaming
    // path always eventually reaches maybeFinalize()'s own
    // requestAllModeResultsSummary() call, and settleSummaryTabPending()
    // there catches even the case where Phase C ends up with nothing to
    // summarize - see that function's own docstring.
    const summaryTab = allModeStreamState.routingMessage
      ? [{ isText: true, tabLabel: 'Summary', text: SUMMARY_TAB_BLOCK_MARKER + allModeStreamState.routingMessage, summaryPending: true }]
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
    // GA fan-out tracking (see trackAllModeFanoutExecute's own comment) -
    // fired on submission, same "not completion" reasoning as
    // sql_executed's own top-level call, and evt.type (see
    // phase_b_connection_done's own comment in translate_routes.py) means
    // this needs no separate lookup the way the batched manual-click path
    // below does.
    trackAllModeFanoutExecute({ name: evt.name, type: evt.type }, 'auto');
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        signal: currentAbortController ? currentAbortController.signal : undefined,
        body: JSON.stringify({ sql: evt.sql, pinned_connections: PINNED_CONNECTIONS }),
      });
      const data = await response.json();
      // Got a real response back at all (whatever it says) - proves the
      // server is reachable, so any lingering server-down banner (raised
      // by the periodic poll, or by an earlier request that genuinely
      // couldn't reach the server) is stale. See markServerReachable()'s
      // own comment for why request successes clear it in addition to the
      // periodic poll.
      markServerReachable('execute_all_mode');
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
        // The one statement that actually failed when there's a specific
        // one (a script that failed partway through); otherwise (a bare
        // connect() failure - nothing ran at all) the whole marked SQL
        // this call sent is the closest thing to "what failed" - either
        // way, this is what buildAllModeSummaryPayload/_build_summary_
        // prompt now show Phase C alongside this database's error (Gap 4).
        const failedStatement = (failureInfo && failureInfo.failedStatement) || evt.sql || '';
        const errorTab = {
          isError: true, error: errMsg,
          statement: failedStatement,
          database: dbRef,
        };
        replaceAllModePlaceholderWithMany(dbRef, [...succeeded, errorTab]);
        state.executeResults.push(...succeeded);
        state.executeFailures.push({ database: dbRef, error: errMsg, failedStatement });
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
      // A genuinely unreachable server, not this connection's SQL - see
      // markServerUnreachable()'s own comment on why request failures (in
      // addition to the periodic poll) raise the server-down banner.
      markServerUnreachable('execute_all_mode');
      const errMsg = err.message || 'Failed to reach the execution backend server.';
      replaceAllModePlaceholder(dbRef, { isError: true, error: errMsg, database: dbRef });
      // Never even reached the server, so evt.sql (the marked SQL this
      // call attempted to send) is the only "what failed" text available -
      // still worth showing Phase C, same reasoning as the response-based
      // failure branch above.
      state.executeFailures.push({ database: dbRef, error: errMsg, failedStatement: evt.sql || '' });
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
      // Chunk 1's per-database sql_blocks, threaded through onto `notes` so
      // captureAllModeHistory() below can record it - see that function's
      // own docstring for why (a later chunk's per-database history
      // fan-out). state.terminalData is /api/translate's own terminal
      // line, already stashed here by translatePrompt()'s router_route
      // branch before this function could ever run.
      databaseSql: (state.terminalData && state.terminalData.sql_blocks) || [],
      // Chunk 4's per-database triage-rewritten questions - the SAME
      // connection_selection array startAllModeStreaming() stashed as
      // state.connectionOrder, now additionally carrying each entry's own
      // "prompt" field (see translate_routes.py's connection_selection/
      // entry_prompts docstrings) - threaded through so
      // fanOutAllModeHistoryPerDatabase() below (via findDatabasePrompt())
      // can record each database's OWN question onto its own fanned-out
      // turn, not the original cross-database one.
      connectionPrompts: state.connectionOrder || [],
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
    const summaryResult = await requestAllModeResultsSummary(notes, state.executeResults, state.executeFailures);
    hideAllModeStreamStatus();
    settleSummaryTabPending();
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
        captureAllModeHistory(pending.entry, notes, state.executeFailures, summaryResult);
        chatStore.clearPending();
      } else {
        modelEntry.results = summarizedResults;
        captureAllModeHistory(modelEntry, notes, state.executeFailures, summaryResult);
      }
      // Both branches above just mutated an ALREADY-PUSHED turn's own
      // modelEntry in place (pushActiveTurn() put it in `history` back in
      // translatePrompt(), before this SQL was even executed) - see
      // chatStore.persistCurrent()'s own docstring for why that mutation
      // needs its own explicit re-save, rather than trusting it'll reach
      // the server eventually.
      chatStore.persistCurrent();
      // Chunk 4 - see fanOutAllModeHistoryPerDatabase's own docstring: does
      // NOT touch chatStore/activeBucketKey, so this runs regardless of
      // which branch above just fired.
      fanOutAllModeHistoryPerDatabase(notes, state.executeResults, state.executeFailures, summaryResult);
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
          // Chunk 5 (see buildInScopeConnectionHistories()'s own
          // docstring) - "all databases" mode only; JSON.stringify simply
          // omits an `undefined`-valued key, so a single-connection-mode
          // request's body carries no connection_histories field at all,
          // same as before this existed. isAllConnectionsSelected() is
          // exactly IN_SCOPE_MODE === 'all', matching
          // stream_translation()'s own router_only_all_mode condition
          // (this function never sends a database_url override - see the
          // comment above - so IN_SCOPE_MODE alone decides this the same
          // way server-side).
          connection_histories: isAllConnectionsSelected() ? buildInScopeConnectionHistories() : undefined,
          // translate_routes.py's /api/translate handler doesn't read this
          // key at all - the only server-side consumer of a client-echoed
          // pinned_connections entry today is execute_routes.py's
          // marker-free fallback (see below). Sent here anyway since it's
          // harmless and keeps this payload shape consistent with
          // /api/execute's - see PINNED_CONNECTIONS' docstring.
          pinned_connections: PINNED_CONNECTIONS
        })
      });

      data = await readNdjsonStream(response, (evt) => {
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
      // The stream read to completion - the server is reachable, whatever
      // this particular translation itself turned out to say. See
      // executeOneAllModeConnection()'s identical call for why any real
      // response clears the down banner, not just an outright success.
      markServerReachable('translate');

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
        pushActiveTurn(promptText, modelEntry);
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
            // Chunk 1's per-database sql_blocks, threaded through so
            // executeSql()'s own pendingAllModeNotes fallback branches
            // (below in this file) can pass it on to captureAllModeHistory
            // too - see that function's own docstring for why (a later
            // chunk's per-database history fan-out). Necessarily empty
            // here whenever `data.sql` itself is (see the `else` branch
            // just below, the only place this object is used with no SQL
            // at all) - server-side, sql_blocks only ever contains entries
            // that actually got real SQL.
            databaseSql: data.sql_blocks || [],
            // Chunk 4's per-database triage-rewritten questions - this
            // fallback's own terminal line already carries
            // connection_selection (same field phase_a_route's live event
            // would have, for a turn that never emitted one - see this
            // object's own declaration comment above) with each entry's
            // own "prompt" field. Threaded through for
            // fanOutAllModeHistoryPerDatabase()/findDatabasePrompt()'s use
            // below, same as databaseSql just above.
            connectionPrompts: data.connection_selection || [],
          };
          // GA fan-out tracking (see trackAllModeFanoutTranslate's own
          // comment) - this is the rare no-live-stream fallback (never
          // happens in real production traffic - see pendingAllModeNotes'
          // own declaration comment), but the fan-out still genuinely
          // happened server-side for this turn, so it still needs
          // counting the same way the live-streaming branch above does.
          trackAllModeFanoutTranslate(data.connection_selection || []);

          if (data.sql) {
            if (autoSqlExecuteEnabled) {
              await executeSql(null, { internal: true });
            }
          } else {
            // Nothing to execute at all - no /api/execute call. Phase C is
            // still worth attempting here: a generation failure is real
            // information it can help explain (see
            // requestAllModeResultsSummary's own docstring - it now runs
            // whenever at least one database has a real result OR an
            // error to report, not only when one succeeded) - this used
            // to be the one router_route shape that never even tried
            // Phase C at all, leaving the Summary tab stuck at triage's
            // bare routing message even when every selected database
            // failed outright. Mirrors maybeFinalize()'s own ordering for
            // the live-streaming path: run Phase C, let it patch the
            // Summary tab in place, THEN capture history off the tab's
            // own final text (routingMessage plus whatever Phase C added)
            // rather than off the bare pre-Phase-C routing message.
            const allModeNotes = pendingAllModeNotes;
            renderAllModeCombinedResults({
              notes: allModeNotes,
              executeResults: [],
              executeFailures: [],
              // Phase C hasn't run yet at this point - see the identical
              // flag on every other router_route render call site.
              summaryPending: true,
            });
            pendingAllModeNotes = null;
            showAllModeSummarizingStatus();
            const summaryResult = await requestAllModeResultsSummary(allModeNotes, [], []);
            hideAllModeStreamStatus();
            settleSummaryTabPending();
            const summaryEntry = getSummaryTabEntry();
            if (summaryEntry) allModeNotes.routingMessage = summaryEntry.text;
            captureAllModeHistory(modelEntry, allModeNotes, [], summaryResult);
            // Chunk 4 - see fanOutAllModeHistoryPerDatabase's own
            // docstring. Nothing was executed at all here (this whole
            // branch is guarded on `!data.sql`), so only the note/failed
            // outcomes in `allModeNotes` can ever produce a fanned-out
            // turn.
            fanOutAllModeHistoryPerDatabase(allModeNotes, [], [], summaryResult);
          }
        }
      } else if (response && response.ok && data && data.sql) {
        const trimmedSql = data.sql.trim();
        const isOpenHelp = trimmedSql.toUpperCase().includes('OPEN HELP POPUP');
        const isNoSql = trimmedSql.startsWith('*** NO SQL ***');

        const modelEntry = { role: 'model', text: data.sql };
        pushActiveTurn(promptText, modelEntry);
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
      // A genuinely unreachable server, not this prompt - see
      // markServerUnreachable()'s own comment on why request failures (in
      // addition to the periodic poll) raise the server-down banner.
      markServerUnreachable('translate');
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
      // Safety net for the network-error path (readNdjsonStream()
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
        // Cleared BEFORE setButtonsDisabled(false), not after: that call's
        // own re-enable branch calls updateHistoryNavButtons(), which now
        // checks uiActionBusy itself (see its own comment) to stay
        // disabled mid-turn - if the flag were still true here, that same
        // guard would keep Back/Forward/New force-disabled at the exact
        // moment they're supposed to finally come back.
        uiActionBusy = false;
        setButtonsDisabled(false);
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
    // A failed statement/connection is shaped {error, ...} rather than
    // {columns, rows, rowCount}. Previously this function ignored that and
    // always built the columns/rows/rowCount shape regardless, which
    // silently collapsed a real error into a fake "0-row success" (empty
    // columns, 0 rows) once it reached history - the error text itself was
    // just dropped. Preserve the error shape instead so a failed turn's
    // history entry actually carries what went wrong; build_gemini_history_
    // contents et al. (server/translate_routes.py) now render an `error`
    // key as real error text instead of a blank block.
    if (result && result.error !== undefined) {
      // isError/statement (not just the error text) must survive too -
      // renderTableResult()'s isError branch (what actually draws the red
      // "Execution Error" box) checks result.isError specifically, not
      // just whether `.error` is present. Without this, a restored failed
      // turn silently fell through to the "No dataset returned" branch
      // instead - the tab was there, but showed the wrong thing entirely,
      // with the real error text nowhere on screen. Mirrors the exact
      // shape renderResultsWithFailedStatement()'s own failedEntry and
      // executeSql()'s own bare-failure currentResultsList entry already
      // use live, on-screen, before this function ever sees them.
      const summarizedError = { isError: true, error: result.error };
      if (result.statement) summarizedError.statement = result.statement;
      if (result.database) summarizedError.database = result.database;
      return summarizedError;
    }
    const rows = result.rows || [];
    const summarized = {
      columns: result.columns || [],
      rowCount: result.rowCount !== undefined ? result.rowCount : rows.length,
      rows: rows
    };
    // EXECUTE_RESULTS_MAX_ROWS's own truncation flag (backends/base.py) -
    // without this, stepping back to a turn whose result set was cut off
    // silently lost that fact: the restored tab looked like a complete,
    // untruncated result instead of the same capped preview the user was
    // actually shown at execution time.
    if (result.truncated) summarized.truncated = true;
    // "All databases" mode results are tagged with which connection they
    // came from (see execute_routes.py and buildResultsTabsNav()'s dbLabel)
    // - preserve that tag so a later history restore can still label each
    // tab by database name instead of a bare "Query N". Server-side history
    // formatting (build_gemini_history_contents et al.) only ever reads
    // columns/rows/rowCount/error and ignores unknown keys, so this is
    // harmless for what actually reaches the LLM.
    if (result.database) summarized.database = result.database;
    // Same idea, for charting (see attachVisualizationToResultsList's own
    // docstring) - a defensive belt-and-suspenders copy for whichever call
    // site happens to attach `.visualization` onto `result` BEFORE this
    // function runs over it; every current call site also re-attaches it
    // onto the summarized copy explicitly afterward regardless of this
    // function's own timing, so this is never the only path that matters,
    // just one more place it can't be silently lost. Also unknown/ignored
    // server-side, same as `.database` above.
    if (result.visualization) summarized.visualization = result.visualization;
    if (result.chartView !== undefined) summarized.chartView = result.chartView;
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

    // GA fan-out tracking (see trackAllModeFanoutExecute's own comment) -
    // this is "all databases" mode's manual-Execute-button path (auto-
    // execute streams its own per-connection events straight from
    // executeOneAllModeConnection() instead, never through this shared
    // function at all - see this function's own top comment), so `sql`
    // above is really every still-pending connection's own marked SQL,
    // joined into one string for a SINGLE batched /api/execute call - one
    // real per-database execution is still about to happen per connection
    // underneath that, so this fires one event per connection rather than
    // one for the whole batch, mirroring trackAllModeFanoutTranslate's own
    // "count the real requests, not the user action" reasoning.
    if (allModeStreamState) {
      currentResultsList
        .filter((r) => r.isPending && r.database)
        .forEach((r) => {
          const type = findConnectionType(allModeStreamState.connectionOrder, r.database.kind, r.database.id);
          trackAllModeFanoutExecute({ name: r.database.name, type }, 'manual');
        });
    } else if (pendingAllModeNotes) {
      // Rare no-live-stream fallback (see pendingAllModeNotes' own
      // declaration comment) - databaseSql is that turn's own per-database
      // sql_blocks list; connectionPrompts (the fallback's own copy of
      // connection_selection) is where `type` comes from, same lookup
      // reasoning as the streaming branch above.
      (pendingAllModeNotes.databaseSql || []).forEach((entry) => {
        const type = findConnectionType(pendingAllModeNotes.connectionPrompts, entry.kind, entry.id);
        trackAllModeFanoutExecute({ name: entry.name, type }, 'manual');
      });
    }

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
      // See executeOneAllModeConnection()'s identical call for why any real
      // response (not just a fully successful one) clears the down banner.
      markServerReachable('execute');
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
          // Set only inside the `if (allModeNotes)` branch below - read
          // further down by both captureAllModeHistory() calls, which are
          // themselves already gated on `allModeNotes` being truthy, so
          // staying null here for the `else` (plain single-connection)
          // branch is never actually read.
          let allModeSummaryResult = null;
          if (allModeNotes) {
            renderAllModeCombinedResults({
              notes: allModeNotes,
              executeResults: data.results,
              executeFailures: [],
              // Phase C hasn't run yet at this point - see the awaited
              // requestAllModeResultsSummary() call just below - so the
              // feedback prompt must stay hidden until it (or
              // settleSummaryTabPending()'s safety net) clears this.
              summaryPending: true,
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
            allModeSummaryResult = await requestAllModeResultsSummary(allModeNotes, data.results, []);
            hideAllModeStreamStatus();
            settleSummaryTabPending();
            const summaryEntry = getSummaryTabEntry();
            if (summaryEntry) allModeNotes.routingMessage = summaryEntry.text;
          } else {
            renderMultiTurnResults(data.results);
          }

          const promptText = aiPrompt && aiPrompt.value.trim() ? aiPrompt.value.trim() : "[Direct SQL Execution]";
          const summarizedResults = Array.isArray(data.results) ? data.results.map(summarizeResultForHistory) : [];

          // Single-connection mode's own post-execution summarization (see
          // requestSingleModeResultsSummary's own section comment above) -
          // skipped for "all databases" mode (allModeNotes truthy - that
          // already got its own Phase C summary above) and for direct SQL
          // entry/re-run (no real NL question to summarize against - see
          // aiPrompt's own fallback to "[Direct SQL Execution]" just above).
          // Awaited, same as Phase C's own call sites, so buttons stay
          // disabled for this extra round trip rather than re-enabling
          // before the Summary tab is actually ready.
          let singleModeSummary = null;
          if (!allModeNotes && promptText !== "[Direct SQL Execution]" && Array.isArray(data.results) && data.results.length) {
            showAllModeSummarizingStatus();
            const summaryResult = await requestSingleModeResultsSummary(promptText, sql, data.results);
            hideAllModeStreamStatus();
            if (summaryResult) {
              singleModeSummary = summaryResult.summaryText;
              // Tags whichever one of THIS turn's own result tabs the
              // visualization actually belongs to - see
              // attachVisualizationToResultsList's own docstring. Applied to
              // both the live, on-screen entries (data.results - the same
              // objects currentResultsList already points at, via
              // renderMultiTurnResults(data.results) just above) and the
              // separate summarizedResults copy below that actually gets
              // persisted onto the turn, since the two are different
              // objects built from the same underlying rows.
              attachVisualizationToResultsList(data.results, summaryResult.visualization);
              attachVisualizationToResultsList(summarizedResults, summaryResult.visualization);
            }
            if (singleModeSummary) prependSingleModeSummaryTab(singleModeSummary);
          }

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
            if (allModeNotes) captureAllModeHistory(pending.entry, allModeNotes, [], allModeSummaryResult);
            if (singleModeSummary) pending.entry.summary = singleModeSummary;
            // This turn was already pushed (as bare SQL, before it was
            // executed) - see chatStore.persistCurrent()'s own docstring
            // for why filling in its results afterward needs its own
            // explicit re-save.
            chatStore.persistCurrent();
            chatStore.clearPending();
          } else {
            // Any other execution (direct SQL entry, or re-running a query
            // that isn't the pending just-generated one) is its own turn.
            const modelEntry = { role: 'model', text: sql, results: summarizedResults };
            if (allModeNotes) captureAllModeHistory(modelEntry, allModeNotes, [], allModeSummaryResult);
            if (singleModeSummary) modelEntry.summary = singleModeSummary;
            pushActiveTurn(promptText, modelEntry);
            updateHistoryTurnsSubtitle();
          }
          // Chunk 4 - see fanOutAllModeHistoryPerDatabase's own docstring.
          // Runs regardless of which pending/new-turn branch above just
          // fired, same as maybeFinalize()'s identical call - a no-op
          // (undefined notes/executeResults, both defaulted inside) for a
          // plain single-connection execution, where `allModeNotes` is
          // null.
          if (allModeNotes) fanOutAllModeHistoryPerDatabase(allModeNotes, data.results, [], allModeSummaryResult);
        }

        if (connDbDot) connDbDot.className = 'status-dot connected';
      } else {
        const errMsg = response.status === 401
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to log in."
          : (data.error || "An error occurred during SQL execution.");
        // Computed once here (mirrors the success branch's identical
        // expression above) rather than per-branch below - both
        // single-connection failure branches now need it for their own
        // requestSingleModeResultsSummary() call (see each branch's own
        // comment for why summarization now runs on a failed execution
        // too, mirroring "all databases" mode's Phase C).
        const promptText = aiPrompt && aiPrompt.value.trim() ? aiPrompt.value.trim() : "[Direct SQL Execution]";

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
          // Previously this branch ran Phase C inline and then discarded
          // the result without ever persisting history (see this
          // function's now-removed comment explaining that as deliberate,
          // pre-streaming behavior) - a turn that failed partway through
          // was simply never recorded, so a later turn's LLM-1 call had no
          // idea it had even been asked. settleAllModeBatchedResults above
          // already brought state.settledCount up to state.expectedTotal
          // (a failed connection settles exactly like a succeeded one -
          // see its own docstring), and state.terminalData/.modelEntry
          // were already attached by translatePrompt() before this
          // internal executeSql() call was even made - so simply routing
          // through the same maybeFinalize() the success branch above
          // uses is enough to run Phase C AND persist history, with the
          // failure(s) visible to it via state.executeFailures exactly as
          // before, instead of hand-duplicating that logic here.
          await maybeFinalize();
        // pendingAllModeNotes fallback (see its own declaration comment
        // above) - mirrors the success branch's identical fallback a few
        // dozen lines above: Phase C still runs, and (unlike before) the
        // turn is now persisted to history afterward too, since a partial
        // execute failure here is exactly the kind of "turn concluded with
        // an error" the design doc says must still be recorded.
        } else if (pendingAllModeNotes) {
          const allModeNotes = pendingAllModeNotes;
          const executeResults = Array.isArray(data.results) ? data.results : [];
          const executeFailures = Array.isArray(data.failures) ? data.failures : [];
          renderAllModeCombinedResults({
            notes: allModeNotes,
            executeResults: executeResults,
            executeFailures: executeFailures,
            // See the identical comment on this same flag a few dozen
            // lines above (the sibling non-partial-failure fallback
            // branch) - Phase C hasn't run yet at this point.
            summaryPending: true,
          });
          pendingAllModeNotes = null;
          showAllModeSummarizingStatus();
          const summaryResult = await requestAllModeResultsSummary(allModeNotes, executeResults, executeFailures);
          hideAllModeStreamStatus();
          settleSummaryTabPending();
          const summaryEntry = getSummaryTabEntry();
          if (summaryEntry) allModeNotes.routingMessage = summaryEntry.text;

          // Persist history - mirrors the success branch's identical
          // pending-entry-vs-new-turn logic further up in this same
          // function (see its own comments for why each check exists).
          // The modelEntry/pending entry for this turn was already
          // created and (if data.sql was non-empty) marked pending by
          // translatePrompt() before this internal executeSql() call was
          // even made.
          const summarizedResults = executeResults.map(summarizeResultForHistory);
          if (chatStore.getPending() && !chatStore.isPendingCurrent()) {
            chatStore.clearPending();
          }
          if (chatStore.isPendingCurrent()) {
            const pending = chatStore.getPending();
            pending.entry.results = summarizedResults;
            captureAllModeHistory(pending.entry, allModeNotes, executeFailures, summaryResult);
            // Already-pushed turn, filled in afterward - see
            // chatStore.persistCurrent()'s own docstring.
            chatStore.persistCurrent();
            chatStore.clearPending();
          } else {
            const modelEntry = { role: 'model', text: sql, results: summarizedResults };
            captureAllModeHistory(modelEntry, allModeNotes, executeFailures, summaryResult);
            pushActiveTurn(promptText, modelEntry);
            updateHistoryTurnsSubtitle();
          }
          // Chunk 4 - see fanOutAllModeHistoryPerDatabase's own docstring.
          fanOutAllModeHistoryPerDatabase(allModeNotes, executeResults, executeFailures, summaryResult);
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
          // Single-connection mode's own post-execution summarization
          // (see requestSingleModeResultsSummary's section comment above)
          // now runs on a FAILED execution too, mirroring "all databases"
          // mode's Phase C, which already summarizes over whatever DID
          // execute alongside any failures (see e.g. the
          // requestAllModeResultsSummary call a few lines up, made even
          // when data.failures is non-empty). statementResults mirrors
          // _build_single_summary_prompt's own {"columns","rows",
          // "rowCount"}|{"note"}|{"error"} shape - every statement that
          // succeeded before the failure (data.results), plus one final
          // {error} entry for the one that didn't, so the model can
          // reason over (and mention) both, same as it already does for
          // an all-succeeded turn. Computed unconditionally (not just
          // inside the summarization guard below) since history
          // persistence below needs it too, regardless of whether a
          // summary was requested.
          const statementResults = [
            ...(Array.isArray(data.results) ? data.results : []),
            { error: errMsg },
          ];
          // Same "no real question to summarize against" guard the
          // success branch uses.
          let singleModeSummary = null;
          let singleModeVisualization = null;
          if (promptText !== "[Direct SQL Execution]") {
            showAllModeSummarizingStatus();
            const summaryResult = await requestSingleModeResultsSummary(promptText, sql, statementResults);
            hideAllModeStreamStatus();
            if (summaryResult) {
              singleModeSummary = summaryResult.summaryText;
              singleModeVisualization = summaryResult.visualization;
              // Tags whichever succeeded-before-the-failure statement this
              // belongs to (the failed statement itself is never chartable -
              // it's an {error} entry, with no `.columns` to match against)
              // - see attachVisualizationToResultsList's own docstring.
              // statementResults shares its succeeded entries' object
              // references with currentResultsList (both were built from
              // data.results by renderResultsWithFailedStatement just
              // above), so this alone is enough to make the live tab
              // chartable too, not just the persisted copy below.
              attachVisualizationToResultsList(statementResults, singleModeVisualization);
            }
            // Preserves the active tab (the just-rendered failure, which
            // is what needs the user's attention) rather than stealing
            // focus to the new Summary tab - see that helper's own
            // docstring for why this differs from the success path's
            // prependSingleModeSummaryTab.
            if (singleModeSummary) prependSingleModeSummaryTabPreservingActiveTab(singleModeSummary);
          }

          // Persist history - previously this entire branch never called
          // chatStore.pushTurn()/mutated the pending entry at all, so a
          // multi-statement script that failed partway through was simply
          // never recorded (see translate_routes.py's history-builder
          // functions and the design doc: a concluded turn's "results or
          // errors" must be added to history, and this is exactly such a
          // turn). Mirrors the success branch's identical pending-entry-
          // vs-new-turn logic further up this same function.
          const summarizedResults = statementResults.map(summarizeResultForHistory);
          // Separate objects from statementResults' own (see
          // summarizeResultForHistory) - re-tag this copy too, same as the
          // success branch does for its own summarizedResults.
          attachVisualizationToResultsList(summarizedResults, singleModeVisualization);
          if (chatStore.getPending() && !chatStore.isPendingCurrent()) {
            chatStore.clearPending();
          }
          if (chatStore.isPendingCurrent()) {
            const pending = chatStore.getPending();
            pending.entry.text = sql;
            pending.entry.results = summarizedResults;
            if (singleModeSummary) pending.entry.summary = singleModeSummary;
            // Already-pushed turn, filled in afterward - see
            // chatStore.persistCurrent()'s own docstring.
            chatStore.persistCurrent();
            chatStore.clearPending();
          } else {
            const modelEntry = { role: 'model', text: sql, results: summarizedResults };
            if (singleModeSummary) modelEntry.summary = singleModeSummary;
            pushActiveTurn(promptText, modelEntry);
            updateHistoryTurnsSubtitle();
          }
        } else if (resultsBody) {
          // A bare execute failure with nothing else to show alongside it
          // (a connect() failure, or a single-statement script's own
          // error - see execute_routes.py's module docstring on when this
          // flat shape, as opposed to the SqlExecutionError shape the
          // branch above handles, is what comes back). Used to bypass
          // renderTableResult()/the tab system entirely and hand-build
          // this same markup inline - now goes through the same one-entry
          // currentResultsList + renderTableResult() path every other
          // failure shape already uses, so this can also grow a leading
          // Summary tab below exactly the way a successful execution's
          // single result tab does (via prependSingleModeSummaryTab), and
          // so this markup isn't maintained in two places. The 401
          // exclusion this branch has always had (an auth-required
          // failure is out of scope for the Report feature, same as a
          // translation error) is preserved via result.notReportable -
          // see renderTableResult()'s isError branch for how that's used.
          const reportable = response.status !== 401;
          currentResultsList = [{ isError: true, error: errMsg, statement: sql, notReportable: !reportable }];
          activeResultIndex = 0;
          buildResultsTabsNav();
          renderTableResult(currentResultsList[0]);

          // Single-connection mode's own post-execution summarization
          // (see requestSingleModeResultsSummary's section comment above)
          // now runs here too, mirroring "all databases" mode's Phase C -
          // see the SqlExecutionError branch above for the fuller
          // reasoning. Skipped for the same reason the Report button/
          // context are skipped just above (a 401 auth failure, nothing
          // meaningful happened for the model to reason over), in
          // addition to the existing "no real question" guard.
          let singleModeSummary = null;
          if (reportable && promptText !== "[Direct SQL Execution]") {
            showAllModeSummarizingStatus();
            // No `.columns` anywhere in a bare `[{error}]` result set, so
            // this is never chartable - only `summaryText` is ever
            // meaningful here, unlike the two branches above.
            const summaryResult = await requestSingleModeResultsSummary(promptText, sql, [{ error: errMsg }]);
            hideAllModeStreamStatus();
            if (summaryResult) singleModeSummary = summaryResult.summaryText;
            // Preserves the active (error) tab - see that helper's own
            // docstring for why this differs from the success path's
            // prependSingleModeSummaryTab.
            if (singleModeSummary) prependSingleModeSummaryTabPreservingActiveTab(singleModeSummary);
          }

          // Persist history - previously this bare-failure branch never
          // called chatStore.pushTurn()/mutated the pending entry at all
          // (see the multi-statement partial-failure branch above for the
          // fuller reasoning on why a failed turn still needs to be
          // recorded). Skipped only for a 401 (nothing real was attempted -
          // matches the summarization/Report-button guards above), NOT
          // for "[Direct SQL Execution]" (direct SQL re-runs still get
          // their own history turn elsewhere in this function, just
          // without a summary - same convention here).
          if (reportable) {
            const summarizedResults = [summarizeResultForHistory({ error: errMsg })];
            if (chatStore.getPending() && !chatStore.isPendingCurrent()) {
              chatStore.clearPending();
            }
            if (chatStore.isPendingCurrent()) {
              const pending = chatStore.getPending();
              pending.entry.text = sql;
              pending.entry.results = summarizedResults;
              if (singleModeSummary) pending.entry.summary = singleModeSummary;
              // Already-pushed turn, filled in afterward - see
              // chatStore.persistCurrent()'s own docstring.
              chatStore.persistCurrent();
              chatStore.clearPending();
            } else {
              const modelEntry = { role: 'model', text: sql, results: summarizedResults };
              if (singleModeSummary) modelEntry.summary = singleModeSummary;
              pushActiveTurn(promptText, modelEntry);
              updateHistoryTurnsSubtitle();
            }
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
      // A genuinely unreachable server, not this query - see
      // markServerUnreachable()'s own comment on why request failures (in
      // addition to the periodic poll) raise the server-down banner.
      markServerUnreachable('execute');
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
        // Cleared BEFORE setButtonsDisabled(false) - see translatePrompt()'s
        // identical ordering/comment; its re-enable branch calls
        // updateHistoryNavButtons(), which stays force-disabled while
        // uiActionBusy is still true.
        uiActionBusy = false;
        setButtonsDisabled(false);
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
    // Cleared BEFORE setButtonsDisabled(false) - see translatePrompt()'s
    // identical ordering/comment; its re-enable branch calls
    // updateHistoryNavButtons(), which stays force-disabled while
    // uiActionBusy is still true.
    uiActionBusy = false;
    setButtonsDisabled(false);
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
      // Keyboard shortcut for #newTurnBtn (see startNewTurn()) - scoped to
      // this textarea's own keydown, not a document-wide listener, so it
      // can never fight with some other, unrelated Escape behavior
      // elsewhere in the page.
      if (e.key === 'Escape') {
        e.preventDefault();
        startNewTurn();
        return;
      }
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
            // Replays a previously-computed single-connection summary (see
            // executeSql()'s own `.summary` persist above) without a new
            // network call - same "no re-fetch on back/forward" posture
            // every other piece of a saved turn already has.
            if (lastModelEntry.summary) prependSingleModeSummaryTab(lastModelEntry.summary);
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

  // Thin wrapper around chatStore.pushTurn() for every "a real turn just
  // landed in the ACTIVE bucket" call site in this file (translatePrompt()/
  // executeSql()'s several branches) - clears viewingBlankSlate as a side
  // effect, so submitting a genuinely new question from #newTurnBtn's
  // blank slate correctly exits it rather than leaving the flag stuck true
  // once a real turn is back on screen. Deliberately NOT used by
  // pushTurnIntoBucket() (all-mode's fan-out into OTHER databases' own
  // buckets) - a background bucket receiving a turn says nothing about
  // whether the bucket currently on screen is still blank.
  function pushActiveTurn(promptText, modelEntry) {
    viewingBlankSlate = false;
    chatStore.pushTurn(promptText, modelEntry);
  }

  // #newTurnBtn: blanks the prompt/SQL/results so the user can ask a new
  // question, WITHOUT touching this bucket's actual turn history - see
  // viewingBlankSlate's own docstring for why this has to be more than
  // just clearing three fields. Deliberately does not persist anything
  // server-side either: nothing about this bucket's saved turns has
  // changed, so a reload before a real new turn is submitted correctly
  // shows the last real turn again, same as reloading ever did.
  function startNewTurn() {
    if (aiPrompt) aiPrompt.value = '';
    setSqlQuery('');
    chatStore.clearPending();
    clearResultsDisplay();
    // The last turn's all-mode routing pinned specific databases (see
    // PINNED_CONNECTIONS' own declaration) - a genuinely new question
    // should triage across every in-scope database again, not stay
    // artificially narrowed to wherever the turn being cleared landed.
    PINNED_CONNECTIONS = [];
    viewingBlankSlate = true;
    updateHistoryTurnsSubtitle();
    if (aiPrompt) aiPrompt.focus();
    trackEvent('new_turn_clicked', { turn_offset: chatStore.turnOffset() });
  }

  if (newTurnBtn) {
    newTurnBtn.addEventListener('click', startNewTurn);
  }

  if (goBackBtn) {
    goBackBtn.addEventListener('click', () => {
      if (viewingBlankSlate) {
        // Nothing to undo() - the blank slate was never pushed into
        // history - just reveal the real last turn that's still sitting
        // there untouched.
        if (chatStore.turnCount() === 0) return;
        viewingBlankSlate = false;
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
        trackEvent('history_nav_clicked', { turn_offset: chatStore.turnOffset() });
        return;
      }
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

  // --- Server-down banner ------------------------------------------------
  // Before this existed, a genuinely unreachable server just surfaced as
  // whatever generic "Network Error" text each individual call site (see
  // translatePrompt()'s/executeSql()'s own catch blocks below) happened to
  // render inline - accurate in isolation, but easy to misread as "my
  // request/SQL is broken" rather than "the whole server is down", and
  // gives no signal at all until the user actually tries something. This
  // gives that specific situation one unmissable, unambiguous banner.
  //
  // Two independent things can raise it: the SAME periodic
  // /api/client-version poll used for the new-version nudge below (so the
  // banner can appear even before the user does anything - see that
  // poll's own comment), and any of translatePrompt()'s/executeSql()'s/
  // executeOneAllModeConnection()'s own request failures (so the user
  // doesn't have to wait for the next poll tick to get an accurate
  // explanation for a request that just failed in front of them).
  //
  // Symmetrically, it's cleared both by that same periodic poll succeeding
  // AND by any translate/execute request succeeding (see those call sites'
  // own markServerReachable() calls) - "succeeding" here means the request
  // actually reached the server and got a real response back, whatever
  // that response said, since that alone already proves the server (as
  // opposed to this particular query/prompt) is fine. A user actively
  // getting real responses is at least as strong a "the server is up"
  // signal as the next scheduled poll tick, so there's no reason to make
  // them wait for it once they've already seen one succeed.
  // Also reported to GA as 'server_down'/'server_up' - fired only on the
  // actual false->true/true->false EDGE, not on every call (markServerReachable()
  // in particular is called from every successful translate/execute, which
  // would otherwise fire an event on nearly every ordinary query). `source`
  // identifies which of the several call sites made the detection (the
  // periodic poll, or a specific request type) - not part of the ask, but
  // cheap to include and useful if these ever need debugging.
  let serverUnreachable = false;

  function markServerUnreachable(source) {
    const wasReachable = !serverUnreachable;
    serverUnreachable = true;
    if (serverDownBanner) serverDownBanner.classList.remove('hidden');
    if (wasReachable) trackEvent('server_down', { source });
  }

  function markServerReachable(source) {
    const wasUnreachable = serverUnreachable;
    serverUnreachable = false;
    if (serverDownBanner) serverDownBanner.classList.add('hidden');
    if (wasUnreachable) trackEvent('server_up', { source });
  }

  // --- New version (reload nudge) -------------------------------------
  // Lets the user know when the SERVER's client-facing code (index.html/
  // client.js/style.css) has changed since this page loaded - see server/
  // app_config.py's CLIENT_BUILD_ID and config_routes.py's GET /api/
  // client-version for the backend half. Deliberately never forces a
  // reload - restarting the server after a backend-only change (nothing
  // under webClient/ touched) returns the SAME build id, so an already-
  // open tab stays quiet; only a real frontend change trips this.
  //
  // This same request doubles as the server-down liveness check above:
  // /api/client-version needs no session/auth/database resolution at all
  // (see its own docstring), so - unlike /api/translate or /api/execute -
  // any non-2xx response from it (not just a thrown fetch exception) really
  // does mean something is wrong at the infra level, not just "this
  // particular request hit a normal, expected error".
  let startupClientBuildId = null;
  let newVersionBannerDismissed = false;

  async function fetchClientBuildId() {
    try {
      const response = await fetch('/api/client-version', { credentials: 'same-origin' });
      if (!response.ok) {
        markServerUnreachable('poll');
        return null;
      }
      const data = await response.json();
      markServerReachable('poll');
      return data.client_build_id || null;
    } catch (e) {
      markServerUnreachable('poll');
      return null;
    }
  }

  function showNewVersionBanner() {
    if (newVersionBannerDismissed || !newVersionBanner) return;
    newVersionBanner.classList.remove('hidden');
  }

  function hideNewVersionBanner() {
    if (newVersionBanner) newVersionBanner.classList.add('hidden');
  }

  async function checkForNewClientVersion() {
    const currentId = await fetchClientBuildId();
    if (!currentId) return;
    if (!startupClientBuildId) {
      // No baseline yet - most likely the server was down (or this poll's
      // own startup fetch hadn't resolved yet) when the page first loaded,
      // so there was nothing to compare against then. Adopt this as the
      // baseline now instead of treating "we finally reached it" as "the
      // version changed".
      startupClientBuildId = currentId;
      return;
    }
    if (currentId !== startupClientBuildId) {
      showNewVersionBanner();
    }
  }

  if (newVersionReloadBtn) {
    newVersionReloadBtn.addEventListener('click', () => window.location.reload());
  }
  if (newVersionDismissBtn) {
    newVersionDismissBtn.addEventListener('click', () => {
      newVersionBannerDismissed = true;
      hideNewVersionBanner();
    });
  }

  // Captured once, in the background - deliberately NOT awaited here so it
  // never delays startup (fetchBackendConfig() and everything else below
  // proceeds regardless of whether/when this resolves). If the server is
  // down right at page load, this same call already shows the server-down
  // banner immediately, via fetchClientBuildId()'s own markServerUnreachable()
  // call above - no need to wait for the first interval tick.
  fetchClientBuildId().then((id) => { startupClientBuildId = id; });

  // 5 minutes: frequent enough to catch a deploy (or an outage) during a
  // long-idle open tab, infrequent enough that it's not worth bothering
  // with visibility-change-aware pausing.
  setInterval(checkForNewClientVersion, 5 * 60 * 1000);

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