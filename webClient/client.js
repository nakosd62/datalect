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
    const maxEntries = maxTurns * 2;
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
      // What gets sent to /api/translate as `history`.
      toPayload() { return history; },
    };
  }

  const MAX_HISTORY_TURNS = 10;
  const chatStore = createChatHistoryStore(MAX_HISTORY_TURNS);

  let DEFAULT_DB_URL = "";
  let ACTIVE_DB_URL = "";
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
  // Index into CONFIGURED_DBS identifying the active preset, for anonymous
  // (Cloud Run, signed-out) users only - they never receive real preset
  // connection strings (see the redacted configured_databases the server
  // sends them), so URL matching can't tell which preset is active; this
  // index is the anonymous-safe substitute. null/unused for signed-in users,
  // who still match presets by URL as before.
  let ACTIVE_PRESET_INDEX = null;
  let CONFIGURED_DBS = [];
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
  // matched by index (ACTIVE_PRESET_INDEX) rather than by URL, since
  // (unlike their own custom connections) preset connection strings/
  // credentials are still never sent to them - see renderDbRadioButtons().
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

  // Helper function to include Google ID tokens or auth headers in fetch requests
  function getApiHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (googleIdToken) {
      headers['Authorization'] = `Bearer ${googleIdToken}`;
    }
    return headers;
  }

  // ===========================================================================
  // 2. DOM ELEMENT REFERENCES + SMALL MODAL WIRING
  //    (login-required modal, help modal fetch/open logic - full onboarding
  //    wiring for the help button lives further down, in section 6)
  // ===========================================================================
  // DOM Elements - Primary Controls
  const aiPrompt = document.getElementById('aiPrompt');
  const sqlQueryTextarea = document.getElementById('sqlQuery');
  const translateBtn = document.getElementById('translateBtn');
  const runBtn = document.getElementById('runBtn');
  const purgeHistoryBtn = document.getElementById('purgeHistoryBtn');
  const goBackBtn = document.getElementById('goBackBtn');
  const goForwardBtn = document.getElementById('goForwardBtn');
  updateHistoryNavButtons();
  const micBtn = document.getElementById('micBtn');

  // DOM Elements - Config Modal & Connection Status
  const configModal = document.getElementById('configModal');
  const configTriggerBadge = document.getElementById('configTriggerBadge');
  const modalCloseBtn = document.getElementById('modalCloseBtn');
  const configSaveBtn = document.getElementById('configSaveBtn');
  const autoSqlExecuteCheckbox = document.getElementById('autoSqlExecuteCheckbox');
  const connDbName = document.getElementById('connDbName');
  const connDbDot = document.getElementById('connDbDot');

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
      theme: 'dracula',
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
  // 4. SHARED UI HELPERS
  //    (button/textarea state, SQL formatting/display, results-display
  //    resets, history-nav button state, live DB connection status)
  // ===========================================================================
  function setButtonsDisabled(disabled) {
    if (translateBtn) translateBtn.disabled = disabled;
    if (runBtn) runBtn.disabled = disabled;
    if (micBtn) micBtn.disabled = disabled;
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
    const keyNote = rotatedKey ? ', switching to a different API key' : '';
    resultsRetryStatus.innerHTML =
      `<span class="retry-status-icon animate-spin">⟳</span> ` +
      `Gemini had a transient error${keyNote} - retrying (attempt ${attempt} of ${maxAttempts})...`;
    resultsRetryStatus.classList.remove('hidden');
  }

  function hideRetryStatus() {
    if (!resultsRetryStatus) return;
    resultsRetryStatus.classList.add('hidden');
    resultsRetryStatus.innerHTML = '';
  }

  // /api/translate streams newline-delimited JSON (see
  // translate_routes.py's module docstring): zero or more
  // {"status": "retrying", ...} progress lines emitted live as the
  // server's one Gemini-call retry loop runs, followed by exactly one
  // terminal {"status": "done", success, sql/error, ...token usage...}
  // line - the same shape /api/translate used to return as its whole
  // body before streaming existed. A request that never reaches that
  // retry loop at all (missing prompt/API key, a 401 from the auth
  // guard, or a mocked response in tests - see fixtures.js's
  // mockTranslate()) isn't streamed - it's still a single plain JSON
  // object, which this reads exactly the same way: one line, no
  // "status" field, straight into finalData.
  async function readTranslateStream(response) {
    if (!response.body || !response.body.getReader) {
      // No ReadableStream support (very old browser) - fall back to a
      // single json() read. No retry-progress display in that case, but
      // still functionally correct once the whole body has arrived.
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
      if (parsed.status === 'retrying') {
        showRetryStatus(parsed);
      } else {
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

  async function checkDbStatus() {
    if (!connDbDot) return;

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

  async function updateConnectionDetails(data) {
    const badge = document.getElementById('configTriggerBadge');

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
      if (connDbName) connDbName.textContent = data?.database_name || 'Database';
      document.title = `yDyL`;
      await checkDbStatus();
      return;
    }

    if (!data?.database_name && !data?.custom_database_name) {
      if (badge) badge.style.display = 'none';
      return;
    }

    if (badge) badge.style.display = '';

    const matchedPreset = CONFIGURED_DBS.find(db => db.url === data.active_database_url);
    // A custom connection's URL can collide with a preset's, so URL
    // equality alone can't tell them apart - active_is_custom (the server's
    // record of which one the user actually picked) breaks the tie. Without
    // it, a colliding preset match would always win here even when the user
    // explicitly selected their own custom connection with the same URL.
    const dbDisplayName = data.active_is_custom
      ? (data.custom_database_name || data.database_name || "Database")
      : (matchedPreset?.name || data.database_name || "Database");

    if (configTriggerBadge) {
      configTriggerBadge.title = `Connected to: ${dbDisplayName} (Click to configure)`;
    }

    if (connDbName) {
      connDbName.textContent = dbDisplayName;
    }

    document.title = `yDyL`;

    await checkDbStatus();
  }

  function getMatchingPresetUrl(targetUrl) {
    if (!targetUrl || !CONFIGURED_DBS || CONFIGURED_DBS.length === 0) return null;
    const found = CONFIGURED_DBS.find(db => db.url === targetUrl);
    return found ? found.url : null;
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
      ACTIVE_CUSTOM_CONNECTION_KEY = data.active_custom_connection_key || "";
      ACTIVE_USES_CUSTOM_CREDENTIALS = Boolean(data.active_uses_custom_credentials);
      ACTIVE_PRESET_INDEX = typeof data.active_preset_index === 'number' ? data.active_preset_index : null;

      renderDbRadioButtons();
      loadConfigIntoUI();
      
      await updateConnectionDetails(data);
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
    // Postgres and MySQL share the same simple shape (a single URL field,
    // no dialect-specific config) - see backends/mysql.py's module
    // docstring - so both fall through here, preserving whichever of the
    // two was actually selected rather than collapsing MySQL into
    // Postgres. Any other/unrecognized value (there shouldn't be one -
    // the dropdown only ever offers these seven types) also lands on
    // Postgres, matching this function's original default.
    return { name: '', type: (type === 'mysql' ? 'mysql' : 'postgres'), url: '', config: {} };
  }

  // Renders every entry in `customDatabases` (including in-progress blank
  // rows added via "+ Add custom connection") as an editable row with a
  // dialect selector. Each row's inputs keep `customDatabases[index]` in
  // sync live via their own 'input' listeners, so by the time
  // triggerConfigSave() runs there's nothing left to harvest from the DOM.
  function renderCustomDbRows(activeUrl) {
    const container = document.getElementById('customDbsContainer');
    if (!container) return;

    let html = '';
    customDatabases.forEach((db, index) => {
      const cfg = db.config || {};
      const isBigQuery = db.type === 'bigquery';
      const isSnowflake = db.type === 'snowflake';
      const isMySQL = db.type === 'mysql';
      const isDatabricks = db.type === 'databricks';
      const isOracle = db.type === 'oracle';
      const isRedshift = db.type === 'redshift';
      const sfAuthMethod = cfg.auth_method || (cfg.private_key ? 'private_key' : 'password');
      // ACTIVE_IS_CUSTOM gates this, not just URL equality - a custom
      // connection's URL can collide with a preset's, and when the active
      // connection is actually the preset (ACTIVE_IS_CUSTOM false), no
      // custom row should show as selected even if one happens to share
      // that URL (see renderDbRadioButtons()'s matching isCustom check).
      // Beyond that, prefer matching by connection_key over URL whenever
      // the server gave us one - two saved custom connections can
      // themselves share a URL (e.g. two BigQuery connections on the same
      // project/dataset with different service-account keys), so URL
      // matching alone can't tell which specific one is active. Falls back
      // to URL matching only when ACTIVE_CUSTOM_CONNECTION_KEY is blank -
      // a session saved before that field existed.
      const isSelected = ACTIVE_IS_CUSTOM && Boolean(db.url) && (
        ACTIVE_CUSTOM_CONNECTION_KEY
          ? db.connection_key === ACTIVE_CUSTOM_CONNECTION_KEY
          : activeUrl === db.url
      );

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
              <option value="postgres" ${(!isBigQuery && !isSnowflake && !isMySQL && !isDatabricks && !isOracle && !isRedshift) ? 'selected' : ''}>PostgreSQL</option>
              <option value="mysql" ${isMySQL ? 'selected' : ''}>MySQL</option>
              <option value="bigquery" ${isBigQuery ? 'selected' : ''}>BigQuery</option>
              <option value="snowflake" ${isSnowflake ? 'selected' : ''}>Snowflake</option>
              <option value="databricks" ${isDatabricks ? 'selected' : ''}>Databricks</option>
              <option value="oracle" ${isOracle ? 'selected' : ''}>Oracle</option>
              <option value="redshift" ${isRedshift ? 'selected' : ''}>Redshift</option>
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
          ` : `
          <div class="custom-db-field-row">
            <div class="custom-db-field wide">
              <label class="custom-db-field-label" for="custom-db-url-${index}">URL:</label>
              <input type="text" id="custom-db-url-${index}" class="config-input custom-db-url-input" data-index="${index}" placeholder="${isMySQL ? 'mysql://user:password@host:3306/dbname' : 'postgresql://user:password@host:5432/dbname'}" value="${maskConnectionUrl(db.url)}" autocomplete="off">
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
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        customDatabases[index].name = input.value.trim();
        if (radio) radio.dataset.dbname = customDatabases[index].name;
      });
    });

    container.querySelectorAll('.custom-db-url-input').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        const val = input.value.trim();
        const unmaskedUrl = unmaskConnectionUrl(val, customDatabases[index].url);
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

    container.querySelectorAll('.custom-db-bq-project, .custom-db-bq-dataset, .custom-db-bq-billing, .custom-db-bq-creds').forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-bq-project')) db.config.project_id = input.value.trim();
        if (input.classList.contains('custom-db-bq-dataset')) db.config.dataset = input.value.trim();
        if (input.classList.contains('custom-db-bq-billing')) db.config.billing_project_id = input.value.trim();
        if (input.classList.contains('custom-db-bq-creds')) db.config.credentials_json = input.value.trim();
        // Synthetic (non-secret) identifier, kept in sync so radio-selection
        // matching against activeUrl still works the same way it does for
        // Postgres rows.
        db.url = (db.config.project_id && db.config.dataset)
          ? `bigquery://${db.config.project_id}/${db.config.dataset}`
          : '';
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
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
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
        // Synthetic (non-secret) identifier, kept in sync so radio-selection
        // matching against activeUrl still works the same way it does for
        // Postgres/BigQuery rows. Schema is optional on a Snowflake
        // connection (see backends/snowflake.py) - included only when set,
        // mirroring config_routes.py's _snowflake_url.
        db.url = (db.config.account && db.config.database)
          ? `snowflake://${db.config.account}/${db.config.database}${db.config.schema ? '/' + db.config.schema : ''}`
          : '';
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
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-dbx-hostname')) db.config.server_hostname = input.value.trim();
        if (input.classList.contains('custom-db-dbx-path')) db.config.http_path = input.value.trim();
        if (input.classList.contains('custom-db-dbx-catalog')) db.config.catalog = input.value.trim();
        if (input.classList.contains('custom-db-dbx-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-dbx-token')) db.config.access_token = input.value;
        // Synthetic (non-secret) identifier, kept in sync so radio-selection
        // matching against activeUrl still works the same way it does for
        // Postgres/BigQuery/Snowflake rows.
        db.url = (db.config.server_hostname && db.config.http_path)
          ? `databricks://${db.config.server_hostname}${db.config.http_path}`
          : '';
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
        input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      }
      input.addEventListener(isCheckbox ? 'change' : 'input', () => {
        if (radio) radio.checked = true;
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
        // Synthetic (non-secret) identifier, kept in sync so radio-selection
        // matching against activeUrl still works the same way it does for
        // Postgres/BigQuery/Snowflake/Databricks rows. Mirrors
        // config_routes.py's _oracle_url exactly, including the same 1521
        // default port used when the field is left blank, and service_name
        // taking precedence over sid when both are somehow filled in.
        const serviceOrSid = db.config.service_name || db.config.sid;
        db.url = (db.config.host && serviceOrSid)
          ? `oracle://${db.config.host}:${db.config.port || 1521}/${serviceOrSid}`
          : '';
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
      input.addEventListener('focus', () => { if (radio) radio.checked = true; });
      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        const db = customDatabases[index];
        if (!db.config) db.config = {};
        if (input.classList.contains('custom-db-rs-host')) db.config.host = input.value.trim();
        if (input.classList.contains('custom-db-rs-port')) db.config.port = input.value.trim();
        if (input.classList.contains('custom-db-rs-database')) db.config.database = input.value.trim();
        if (input.classList.contains('custom-db-rs-schema')) db.config.schema = input.value.trim();
        if (input.classList.contains('custom-db-rs-user')) db.config.user = input.value.trim();
        if (input.classList.contains('custom-db-rs-password')) db.config.password = input.value;
        // Synthetic (non-secret) identifier, kept in sync so radio-selection
        // matching against activeUrl still works the same way it does for
        // every other structured-descriptor row. Mirrors config_routes.py's
        // _redshift_url exactly, including the same 5439 default port used
        // when the field is left blank.
        db.url = (db.config.host && db.config.database)
          ? `redshift://${db.config.host}:${db.config.port || 5439}/${db.config.database}`
          : '';
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

  function renderDbRadioButtons(currentDbUrl) {
    const radioGroup = document.getElementById('modalDbRadioGroup');
    if (!radioGroup) return;

    const activeUrl = currentDbUrl || ACTIVE_DB_URL || DEFAULT_DB_URL;
    const matchedPresetUrl = getMatchingPresetUrl(activeUrl);

    // ACTIVE_IS_CUSTOM (the server's record of what was actually picked) is
    // the primary signal, taking priority over the URL match - a saved
    // custom connection can share its URL with a preset, in which case
    // matchedPresetUrl would be found either way and URL matching alone
    // can't tell which is active. Falling back to !matchedPresetUrl covers
    // the ordinary (no collision) case where a custom connection's URL
    // simply isn't one of the presets. Anonymous (Cloud Run, signed-out)
    // users need ACTIVE_IS_CUSTOM used alone, without that fallback,
    // though: their admin-configured presets are matched by index
    // (ACTIVE_PRESET_INDEX), not URL, since preset connection
    // strings/credentials are still never sent to them (configured_databases
    // is redacted for them - see fetchBackendConfig()) - so
    // matchedPresetUrl is never found for an anonymous visitor on a preset
    // either, and the !matchedPresetUrl fallback would wrongly read that as
    // "custom" instead of "on a redacted preset".
    const isCustom = isAnonymousUser ? ACTIVE_IS_CUSTOM : (ACTIVE_IS_CUSTOM || !matchedPresetUrl);

    let html = `<div class="radio-group-heading">Pre-configured Database Playgrounds</div>`;

    CONFIGURED_DBS.forEach((db, index) => {
      // Anonymous users' preset objects have no "url" (redacted) - fall
      // back to an index-based value, mirroring the "custom-N" pattern
      // used for custom rows, and resolved server-side via preset_index.
      const value = db.url || `preset-${index}`;
      const isSelected = !isCustom && (
        isAnonymousUser
          ? ACTIVE_PRESET_INDEX === index
          : Boolean(matchedPresetUrl && db.url === matchedPresetUrl)
      );
      html += `
        <label class="radio-option">
          <input type="radio" name="db_connection_option" value="${value}" data-dbname="${db.name}" ${isSelected ? 'checked' : ''}>
          <span class="radio-label">${db.name}</span>
        </label>
      `;
    });

    html += `<div class="radio-group-heading radio-group-heading-custom">Custom Database Connections</div>`;
    html += `<div id="customDbsContainer" class="custom-dbs-list"></div>`;

    radioGroup.innerHTML = html;

    renderCustomDbRows(activeUrl);
  }

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
    let isCustomOption = false;
    // Set only for anonymous users picking a preset by index (see
    // renderDbRadioButtons()) - the server resolves the real connection
    // from this index itself, since anonymous users never receive one.
    let presetIndex = null;

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
    // Postgres and MySQL are both "simple URL" dialects (see
    // backends/mysql.py's module docstring) - a single non-blank url is
    // all either needs to be selectable/saveable. Named generically
    // (not isCompletePostgres) since it now covers both.
    const isCompleteSimpleUrlDb = (db) => db && db.type !== 'bigquery' && db.type !== 'snowflake' && db.type !== 'databricks' && db.type !== 'oracle' && db.type !== 'redshift'
      && db.url && db.url.trim() !== "";

    const selectedDbRadio = document.querySelector('input[name="db_connection_option"]:checked');
    if (selectedDbRadio) {
      if (selectedDbRadio.value.startsWith('custom-')) {
        isCustomOption = true;
        const index = parseInt(selectedDbRadio.value.split('-')[1]);
        const selectedDb = customDatabases[index];
        const isComplete = (d) => isCompleteBigQuery(d) || isCompleteSnowflake(d) || isCompleteDatabricks(d) || isCompleteOracle(d) || isCompleteRedshift(d) || isCompleteSimpleUrlDb(d);
        const chosen = isComplete(selectedDb) ? selectedDb : customDatabases.find(isComplete);

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
          dbUrlValue = `bigquery://${dbProjectId}/${dbDataset}`;
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
          dbUrlValue = `snowflake://${dbAccount}/${dbDatabase}${dbSchema ? '/' + dbSchema : ''}`;
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
          dbUrlValue = `databricks://${dbServerHostname}${dbHttpPath}`;
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
          dbUrlValue = `oracle://${dbHost}:${dbPort || 1521}/${dbServiceName || dbSid}`;
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
          dbUrlValue = `redshift://${dbHost}:${dbPort || 5439}/${dbDatabase}`;
        } else if (isCompleteSimpleUrlDb(chosen)) {
          dbType = chosen.type === 'mysql' ? 'mysql' : 'postgres';
          dbUrlValue = chosen.url;
          dbNameValue = chosen.name;
        } else {
          dbType = 'postgres';
          dbUrlValue = DEFAULT_DB_URL;
          dbNameValue = "Default DB";
          isCustomOption = false;
        }

        customDbName = dbNameValue;
        customDbUrl = dbUrlValue;
      } else if (selectedDbRadio.value.startsWith('preset-')) {
        // Anonymous path: this preset's real URL was withheld from us
        // (see fetchBackendConfig()'s redacted configured_databases), so
        // we can only tell the server which index was picked and let it
        // resolve the actual connection itself.
        presetIndex = parseInt(selectedDbRadio.value.split('-')[1], 10);
        const matchedDb = CONFIGURED_DBS[presetIndex];
        dbType = (matchedDb && matchedDb.type) || 'postgres';
        dbNameValue = matchedDb ? matchedDb.name : "Preset DB";
      } else {
        dbUrlValue = selectedDbRadio.value;
        const matchedDb = CONFIGURED_DBS.find(db => db.url === dbUrlValue);
        dbType = (matchedDb && matchedDb.type) || 'postgres';
        dbNameValue = matchedDb ? matchedDb.name : "Preset DB";
        if (dbType === 'bigquery' && matchedDb) {
          dbProjectId = matchedDb.project_id;
          dbDataset = matchedDb.dataset;
          // No credentials_json for admin presets - they authenticate via
          // the app's own service account (ADC), not a per-connection key.
        } else if (dbType === 'snowflake' && matchedDb) {
          dbAccount = matchedDb.account;
          dbUser = matchedDb.user;
          dbWarehouse = matchedDb.warehouse;
          dbDatabase = matchedDb.database;
          dbSchema = matchedDb.schema || null;
          dbRole = matchedDb.role || null;
          // Unlike BigQuery presets, a Snowflake preset DOES carry its own
          // credential right here (CONFIGURED_DBS - see app_config.py's
          // DATABASE_PRESETS_FILE comment for why Snowflake has no ADC-
          // style ambient identity to authenticate as instead) - without
          // resending it, the server would save a credential-less
          // connection and every subsequent query would fail.
          dbPassword = matchedDb.password || null;
          dbPrivateKey = matchedDb.private_key || null;
          dbPrivateKeyPassphrase = matchedDb.private_key_passphrase || null;
        } else if (dbType === 'databricks' && matchedDb) {
          dbServerHostname = matchedDb.server_hostname;
          dbHttpPath = matchedDb.http_path;
          dbCatalog = matchedDb.catalog || null;
          dbSchema = matchedDb.schema || null;
          // Like Snowflake, a Databricks preset carries its own credential
          // right here (CONFIGURED_DBS) - Databricks has no ADC-style
          // ambient identity either (see backends/databricks.py's module
          // docstring) - without resending it, the server would save a
          // credential-less connection and every subsequent query would
          // fail.
          dbAccessToken = matchedDb.access_token || null;
        } else if (dbType === 'oracle' && matchedDb) {
          dbHost = matchedDb.host;
          dbPort = matchedDb.port || null;
          dbServiceName = matchedDb.service_name || null;
          dbSid = matchedDb.sid || null;
          dbUser = matchedDb.user;
          dbSchema = matchedDb.schema || null;
          // Like Databricks, an Oracle preset carries its own credential
          // right here (CONFIGURED_DBS) - Oracle has no ADC-style ambient
          // identity either (see backends/oracle.py's module docstring) -
          // without resending it, the server would save a credential-less
          // connection and every subsequent query would fail.
          dbPassword = matchedDb.password || null;
          dbSsl = Boolean(matchedDb.ssl);
        } else if (dbType === 'redshift' && matchedDb) {
          dbHost = matchedDb.host;
          dbPort = matchedDb.port || null;
          dbDatabase = matchedDb.database;
          dbUser = matchedDb.user;
          dbSchema = matchedDb.schema || null;
          // Like Oracle, a Redshift preset carries its own credential right
          // here (CONFIGURED_DBS) - Redshift has no ADC-style ambient
          // identity either (see backends/redshift.py's module docstring) -
          // without resending it, the server would save a credential-less
          // connection and every subsequent query would fail.
          dbPassword = matchedDb.password || null;
        }
      }
    } else {
      dbUrlValue = DEFAULT_DB_URL;
      dbNameValue = "Default DB";
    }

    const autoSqlExecuteValue = autoSqlExecuteCheckbox
      ? autoSqlExecuteCheckbox.checked
      : autoSqlExecuteEnabled;

    const payload = {
      database_name: dbNameValue,
      database_type: dbType,
      is_custom: isCustomOption,
      custom_databases: customDatabases
        .filter(d => isCompleteBigQuery(d) || isCompleteSnowflake(d) || isCompleteDatabricks(d) || isCompleteOracle(d) || isCompleteRedshift(d) || isCompleteSimpleUrlDb(d))
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
          return { type: (d.type === 'mysql' ? 'mysql' : 'postgres'), name: d.name, url: d.url };
        }),
      auto_sql_execute: autoSqlExecuteValue
    };
    if (presetIndex !== null) {
      payload.preset_index = presetIndex;
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
    } else {
      payload.database_url = dbUrlValue;
    }

    const configSaveErrorEl = document.getElementById('configSaveError');

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
        // url/is_custom/connection_key/preset_index together are what
        // uniquely identify "the" active connection (presets: url; custom
        // connections: connection_key; anonymous preset picks: preset_index,
        // since url is withheld from them - see "what makes a db connection
        // unique" discussion).
        const previousConnectionIdentity = `${ACTIVE_DB_URL}|${ACTIVE_IS_CUSTOM}|${ACTIVE_CUSTOM_CONNECTION_KEY}|${ACTIVE_PRESET_INDEX}`;
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
        if (data.active_preset_index !== undefined) {
          ACTIVE_PRESET_INDEX = typeof data.active_preset_index === 'number' ? data.active_preset_index : null;
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

        const nextConnectionIdentity = `${ACTIVE_DB_URL}|${ACTIVE_IS_CUSTOM}|${ACTIVE_CUSTOM_CONNECTION_KEY}|${ACTIVE_PRESET_INDEX}`;
        if (nextConnectionIdentity !== previousConnectionIdentity) {
          clearActiveQueryState();
        }

        if (configSaveErrorEl) {
          configSaveErrorEl.style.display = 'none';
          configSaveErrorEl.textContent = '';
        }

        await updateConnectionDetails(data);
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
    if (autoSqlExecuteCheckbox) {
      autoSqlExecuteCheckbox.checked = autoSqlExecuteEnabled;
    }
  }

  function closeConfigModal() {
    if (configModal) configModal.classList.add('hidden');
  }

  if (configTriggerBadge && configModal) {
    configTriggerBadge.addEventListener('click', async () => {
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
        title: "This is the databse you are connected to",
        body: "Click this badge to switch to any pre-configured database or connect to your own."
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
      }
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
    if (!tourOverlay) return;
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

  function renderStatisticsCharts(statsData) {
    if (!statsData || statsData.length === 0 || typeof window.Chart === 'undefined') return;

    const dates = statsData.map(item => item.day_date || item.date || 'Unknown');
    const totalTranslations = statsData.map(item => item.total_translations || 0);
    const sumTotalTokens = statsData.map(item => item.sum_total_tokens || 0);

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { 
        legend: { display: false } 
      },
      scales: {
        x: { ticks: { color: '#94a3b8', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
        y: { ticks: { color: '#94a3b8', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } }
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
            backgroundColor: 'rgba(56, 189, 248, 0.6)',
            borderColor: '#38bdf8',
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
            backgroundColor: 'rgba(16, 185, 129, 0.6)',
            borderColor: '#10b981',
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
      updateHistoryTurnsSubtitle();
      const purgeTitleEl = document.querySelector('.btn-purge-title');
      if (purgeTitleEl) {
        purgeTitleEl.textContent = '(...)';
      }
      historyModal.classList.remove('hidden');
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
  function renderTableResult(result) {
    if (!resultsHeader || !resultsBody) return;
    resultsHeader.innerHTML = '';
    resultsBody.innerHTML = '';

    if (!result || (!result.columns && !result.rows)) {
      resultsBody.innerHTML = `<tr><td class="text-center text-muted py-8">Statement executed successfully. No dataset returned.</td></tr>`;
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
    } else {
      resultsBody.innerHTML = `<tr><td colspan="${result.columns ? result.columns.length : 1}" class="text-center text-muted py-8">0 rows returned.</td></tr>`;
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
      btn.className = `result-tab-btn ${idx === activeResultIndex ? 'active' : ''}`;

      const sqlText = res.query || res.sql || res.statement || '';
      if (sqlText) {
        btn.setAttribute('title', sqlText);
      }

      const count = res.rowCount !== undefined ? res.rowCount : (res.rows ? res.rows.length : 0);
      const rowLabel = count === 1 ? '1 row' : `${count} rows`;
      btn.textContent = `Query ${idx + 1} (${rowLabel})`;

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
  
  function renderNoSqlResponse(rawText) {
    const cleanText = (rawText || '').replace(/^\*\*\*\s*NO\s*SQL\s*\*\*\*\s*/i, '').trim() || rawText || '';

    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) {
      resultsBody.innerHTML = '';
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'response-cell';

      const p = document.createElement('p');
      p.className = 'response-text';
      p.textContent = cleanText;

      td.appendChild(p);
      tr.appendChild(td);
      resultsBody.appendChild(tr);
    }
  }

  // ===========================================================================
  // 9. TRANSLATE (NL -> SQL) AND EXECUTE SQL
  // ===========================================================================
  async function translatePrompt() {
    await fetchBackendConfig();

    clearResultsDisplay();

    const promptText = aiPrompt ? aiPrompt.value.trim() : "";
    if (!promptText) return;

    setButtonsDisabled(true);

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
        body: JSON.stringify({
          prompt: promptText,
          history: chatStore.toPayload()
        })
      });

      data = await readTranslateStream(response);
      hideRetryStatus();

      // A streamed translation failure (every retry exhausted, or a
      // non-retryable error) comes back as HTTP 200 with success:false in
      // the terminal line, not a real error status - see
      // translate_routes.py's module docstring for why. The !data.sql
      // check below already treats that the same as any other failure, so
      // no separate handling is needed here; response.ok only still
      // matters for the auth-guard's real 401 (checked below) and for the
      // early-validation 400s (missing prompt/API key), which return a
      // real error status because they're not streamed at all.
      if (response && response.ok && data && data.sql) {
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
          renderNoSqlResponse(data.sql);
        } else {
          setSqlQuery(data.sql);
          chatStore.setPending(modelEntry, normalizeSqlForCompare(data.sql));

          if (autoSqlExecuteEnabled) {
            await executeSql();
          }
        }
      } else {
        setSqlQuery('');

        const errMsg = response && response.status === 401 
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to log in."
          : (data?.error || "An error occurred during translation.");
        console.error("Translation Error:", errMsg);

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
    } finally {
      // Safety net for the network-error path (readTranslateStream()
      // itself throwing, e.g. the connection dropping mid-stream) - the
      // explicit hideRetryStatus() call above only runs once the stream
      // actually finished parsing.
      hideRetryStatus();
      setButtonsDisabled(false);
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
    return {
      columns: result.columns || [],
      rowCount: result.rowCount !== undefined ? result.rowCount : rows.length,
      rows: rows
    };
  }

  async function executeSql(customSql = null) {
    await fetchBackendConfig();

    clearResultsDisplay();
  
    const sql = customSql || getSqlQuery();
    if (!sql) return;
  
    setButtonsDisabled(true);

    // See the comment in translatePrompt() above - no database_url
    // override here either, for the same reason.
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          sql: sql
        })
      });
  
      const data = await response.json();
      if (response.ok && data.success) {
        renderMultiTurnResults(data.results);

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
          chatStore.clearPending();
        } else {
          // Any other execution (direct SQL entry, or re-running a query
          // that isn't the pending just-generated one) is its own turn.
          chatStore.pushTurn(promptText, { role: 'model', text: sql, results: summarizedResults });
          updateHistoryTurnsSubtitle();
        }

        if (connDbDot) connDbDot.className = 'status-dot connected';
      } else {
        const errMsg = response.status === 401 
          ? "Authentication required. Please click 'Sign in with Google' in the top-right corner to log in."
          : (data.error || "An error occurred during SQL execution.");
        if (resultsBody) {
          resultsBody.innerHTML = `
            <tr>
              <td class="error-cell">
                <div class="error-container">
                  <span class="error-icon">⚠️</span>
                  <div class="error-details">
                    <strong>Execution Error</strong>
                    <p>${errMsg}</p>
                  </div>
                </div>
              </td>
            </tr>`;
        }
      }
    } catch (err) {
      const errMsg = err.message || "Failed to reach the execution backend server.";
      console.error("Failed to execute SQL:", err);
    } finally {
      setButtonsDisabled(false);
    }
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

  // Example prompt chips (zero-state guidance for first-time users): fill
  // the NL prompt box with a working example and immediately run it, so
  // someone who has never used the app can see the whole prompt -> SQL ->
  // results flow without having to guess what to type first.
  const examplePromptButtons = document.querySelectorAll('.example-chip');
  if (examplePromptButtons.length && aiPrompt) {
    examplePromptButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        aiPrompt.value = btn.dataset.prompt || '';
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
      
      if (lastModelEntry && lastModelEntry.text) {
        const sqlText = lastModelEntry.text;
        const isNoSql = sqlText.startsWith('*** NO SQL ***');
        
        if (isNoSql) {
          setSqlQuery('');
          chatStore.clearPending();
          renderNoSqlResponse(sqlText);
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
      }
    });
  }

  if (goForwardBtn) {
    goForwardBtn.addEventListener('click', () => {
      if (chatStore.redo()) {
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
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