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
  // login (i.e. the backend resolved it to the shared "anonymous" user).
  // Anonymous users get full translate/execute functionality, but the DB
  // connection config popup and translation history popup are gated -
  // see updateAnonymousRestrictions() / showLoginRequiredModal().
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

  // DOM Elements - Login Required Modal (shown when an anonymous user
  // clicks the DB config badge or the history button)
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

  // Grays out (and disables the normal behavior of) the history button for
  // anonymous users. The DB config badge stays fully clickable for
  // anonymous users too - they may open the dialog and switch between
  // admin-configured presets, just not save a custom connection (see
  // renderCustomDbRows()'s isAnonymousUser guard and the server-side 403
  // in config_routes.py). Called whenever isAnonymousUser changes (i.e.
  // every time fetchBackendConfig() resolves).
  function updateAnonymousRestrictions() {
    if (configTriggerBadge) {
      configTriggerBadge.title = isAnonymousUser
        ? 'Connection Info (Click to configure - sign in for custom connections)'
        : 'Connection Info (Click to configure)';
    }
    if (historyBtn) {
      historyBtn.classList.toggle('icon-disabled', isAnonymousUser);
      historyBtn.title = isAnonymousUser
        ? 'Log in to view translation history'
        : 'Translation History';
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
    if (resultsTabsNav) resultsTabsNav.classList.add('hidden');
    if (resultsHeader) resultsHeader.innerHTML = '';
    if (resultsBody) resultsBody.innerHTML = '';
    currentResultsList = [];
    activeResultIndex = 0;
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
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          // Deliberately dialect-agnostic: only response.ok/data.success
          // below are ever inspected, never the returned value, so this
          // just needs to be a trivial query every supported backend can
          // run with no special permissions or existing tables/datasets.
          // The previous "SELECT current_user, current_database();" was
          // Postgres-specific - BigQuery Standard SQL has no
          // current_database() function, so it always failed there and
          // permanently showed the badge as disconnected even on a
          // perfectly working BigQuery connection.
          sql: 'SELECT 1;'
        })
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

    if (isAnonymousUser) {
      // The backend withholds usernames/connection strings/custom
      // connections from anonymous requests, but does send back the
      // preset display name (e.g. "Demo") in data.database_name since
      // that's just a label, not a credential.
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
    return type === 'bigquery'
      ? { name: '', type: 'bigquery', url: '', config: { project_id: '', dataset: '', billing_project_id: '', credentials_json: '' } }
      : { name: '', type: 'postgres', url: '', config: {} };
  }

  // Renders every entry in `customDatabases` (including in-progress blank
  // rows added via "+ Add custom connection") as an editable row with a
  // dialect selector. Each row's inputs keep `customDatabases[index]` in
  // sync live via their own 'input' listeners, so by the time
  // triggerConfigSave() runs there's nothing left to harvest from the DOM.
  function renderCustomDbRows(activeUrl) {
    const container = document.getElementById('customDbsContainer');
    if (!container) return;

    // Anonymous (Cloud Run, signed-out) users may only pick from presets -
    // no custom-connection rows, no "+ Add custom connection" button. The
    // server also rejects any custom-connection save from this identity
    // (see config_routes.py's handle_config), so this is belt-and-suspenders
    // rather than the only enforcement, but it keeps the dialog from
    // offering a control that would just come back as an error.
    if (isAnonymousUser) {
      container.innerHTML = '';
      return;
    }

    let html = '';
    customDatabases.forEach((db, index) => {
      const cfg = db.config || {};
      const isBigQuery = db.type === 'bigquery';
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

      html += `
        <div class="custom-db-row" style="display: flex; flex-direction: column; gap: 0.4rem; width: 100%; padding: 0.5rem 0; border-bottom: 1px solid var(--border-color, #333);">
          <div style="display: flex; align-items: center; gap: 0.5rem; width: 100%; flex-wrap: wrap;">
            <input type="radio" name="db_connection_option" value="custom-${index}" data-dbname="${db.name || ''}" ${isSelected ? 'checked' : ''}>
            <select class="config-input custom-db-type-select" data-index="${index}" style="flex: 0 0 auto; width: 8rem;">
              <option value="postgres" ${!isBigQuery ? 'selected' : ''}>PostgreSQL</option>
              <option value="bigquery" ${isBigQuery ? 'selected' : ''}>BigQuery</option>
            </select>
            <input type="text" class="config-input custom-db-name-input" data-index="${index}" placeholder="Name" size="10" value="${db.name || ''}" style="flex: 0 0 auto; width: 10ch;" autocomplete="off">
            ${isBigQuery ? `
            <input type="text" class="config-input custom-db-bq-project" data-index="${index}" placeholder="Project ID" value="${cfg.project_id || ''}" style="flex: 1 1 120px; min-width: 100px;" autocomplete="off">
            <input type="text" class="config-input custom-db-bq-dataset" data-index="${index}" placeholder="Dataset" value="${cfg.dataset || ''}" style="flex: 1 1 120px; min-width: 100px;" autocomplete="off">
            ` : `
            <input type="text" class="config-input custom-db-url-input" data-index="${index}" placeholder="postgresql://user:password@host:5432/dbname" value="${maskConnectionUrl(db.url)}" style="flex: 1 1 200px; min-width: 150px;" autocomplete="off">
            `}
            <button type="button" class="btn btn-secondary custom-db-remove-btn" data-index="${index}" title="Remove this connection" style="padding: 0.15rem 0.6rem; line-height: 1;">&times;</button>
          </div>
          ${isBigQuery ? `
          <div style="padding-left: 1.9rem; display: flex; flex-direction: column; gap: 0.35rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <label for="custom-db-bq-billing-${index}" style="font-size: 0.78rem; width: 7.5rem; flex: 0 0 auto;"><a href="https://cloud.google.com/bigquery/docs/managing-jobs" target="_blank" rel="noopener noreferrer" style="color: inherit; opacity: 0.85; text-decoration: underline dotted;" title="What a billing project is in BigQuery (Google Cloud docs)">Billing Project</a></label>
              <input type="text" id="custom-db-bq-billing-${index}" class="config-input custom-db-bq-billing" data-index="${index}" placeholder="Billing project ID" value="${cfg.billing_project_id || ''}" style="flex: 1 1 auto; min-width: 0;" autocomplete="off">
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <label for="custom-db-bq-creds-${index}" style="font-size: 0.78rem; width: 7.5rem; flex: 0 0 auto;"><a href="https://cloud.google.com/iam/docs/keys-create-delete" target="_blank" rel="noopener noreferrer" style="color: inherit; opacity: 0.85; text-decoration: underline dotted;" title="How to create a service account key (Google Cloud docs)">Service Account Key</a></label>
              <textarea id="custom-db-bq-creds-${index}" class="config-input custom-db-bq-creds" data-index="${index}" placeholder="${db.has_custom_credentials ? 'Key saved - leave blank to keep it, or paste a new one to replace it' : 'Service-account key (JSON)'}" rows="2" style="flex: 1 1 auto; min-width: 0; resize: vertical;" autocomplete="off"></textarea>
            </div>
          </div>
          ` : ``}
        </div>
      `;
    });

    html += `<button type="button" id="addCustomDbBtn" class="btn btn-secondary" style="align-self: flex-start;">+ Add custom connection</button>`;

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

    // Anonymous (Cloud Run, signed-out) users can never be on a custom
    // connection (server-enforced - see config_routes.py) and are matched
    // to a preset by index (ACTIVE_PRESET_INDEX), not URL, since they never
    // receive real preset connection strings (configured_databases is
    // redacted for them - see fetchBackendConfig()). For signed-in users,
    // ACTIVE_IS_CUSTOM (the server's record of what was actually picked)
    // takes priority over the URL match - a saved custom connection can
    // share its URL with a preset, in which case matchedPresetUrl would be
    // found either way and URL matching alone can't tell which is active.
    // Falling back to !matchedPresetUrl covers the ordinary (no collision)
    // case where a custom connection's URL simply isn't one of the presets.
    const isCustom = isAnonymousUser ? false : (ACTIVE_IS_CUSTOM || !matchedPresetUrl);

    let html = '';

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

    html += `<div id="customDbsContainer" style="display: flex; flex-direction: column; gap: 0.5rem; width: 100%;"></div>`;

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
    const isCompletePostgres = (db) => db && db.type !== 'bigquery' && db.url && db.url.trim() !== "";

    const selectedDbRadio = document.querySelector('input[name="db_connection_option"]:checked');
    if (selectedDbRadio) {
      if (selectedDbRadio.value.startsWith('custom-')) {
        isCustomOption = true;
        const index = parseInt(selectedDbRadio.value.split('-')[1]);
        const selectedDb = customDatabases[index];
        const chosen = isCompleteBigQuery(selectedDb) || isCompletePostgres(selectedDb)
          ? selectedDb
          : customDatabases.find(d => isCompleteBigQuery(d) || isCompletePostgres(d));

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
        } else if (isCompletePostgres(chosen)) {
          dbType = 'postgres';
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
        .filter(d => isCompleteBigQuery(d) || isCompletePostgres(d))
        .map(d => isCompleteBigQuery(d)
          ? {
              type: 'bigquery',
              name: d.name,
              project_id: d.config.project_id,
              dataset: d.config.dataset,
              billing_project_id: d.config.billing_project_id,
              credentials_json: d.config.credentials_json || undefined
            }
          : { type: 'postgres', name: d.name, url: d.url }
        ),
      auto_sql_execute: autoSqlExecuteValue
    };
    if (presetIndex !== null) {
      payload.preset_index = presetIndex;
    } else if (dbType === 'bigquery') {
      payload.project_id = dbProjectId;
      payload.dataset = dbDataset;
      if (dbBillingProjectId) payload.billing_project_id = dbBillingProjectId;
      if (dbCredentialsJson) payload.credentials_json = dbCredentialsJson;
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
      // admin-configured presets, just not save a custom connection (the
      // custom-connection UI itself is hidden for them - see
      // renderCustomDbRows()).
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
        title: "You're on a shared demo database",
        body: "It's read-only-friendly and ready to go. Click this badge anytime to switch databases or connect your own."
      },
      {
        target: historyBtn,
        title: 'Past queries, saved',
        body: isAnonymousUser
          ? 'Once signed in, every translation you run is saved here so you can revisit or reuse it later.'
          : 'Every translation you run is saved here so you can revisit or reuse it later.'
      },
      {
        target: authContainer,
        title: isAnonymousUser ? 'Sign in for the full experience' : "You're signed in",
        body: isAnonymousUser
          ? "Sign in with Google here to unlock custom database connections and saved query history."
          : 'Manage your account or sign out from here anytime.'
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
      if (isAnonymousUser) {
        showLoginRequiredModal(
          'Viewing translation history is available to signed-in users only. Please log in with Google to access this feature.'
        );
        return;
      }
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
    let response = null;
    let data = null;
    let attempts = 0;
    const maxAttempts = 5;

    while (attempts < maxAttempts) {
      attempts++;
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

        data = await response.json();

        const errMsg = data.error || (response.ok ? '' : `Server returned status ${response.status}`);
        const errUpper = errMsg.toUpperCase();
        const isResourceExhausted = errUpper.includes('429 RESOURCE_EXHAUSTED');
        const isTemporaryFailure = errUpper.includes('503 UNAVAILABLE');

        if ((!response.ok || !data.sql) && (isResourceExhausted || isTemporaryFailure) && attempts < maxAttempts) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          continue;
        }
        break;
      } catch (err) {
        if (attempts >= maxAttempts) {
          throw err;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }

    try {
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