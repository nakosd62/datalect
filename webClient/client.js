document.addEventListener('DOMContentLoaded', async () => {
  let chatHistory = [];
  let futureChatHistory = [];
  // Points at the most recent chatHistory entry that generated SQL which
  // hasn't yet been executed. When that SQL is run, its results get attached
  // to this entry so future chat turns have access to what data actually
  // came back - not just what SQL/text was generated. Cleared once consumed.
  let pendingSqlHistoryEntry = null;
  let DEFAULT_DB_URL = "";
  let ACTIVE_DB_URL = "";
  let CONFIGURED_DBS = [];
  let currentGoogleClientId = null;
  let googleIdToken = null;
  let customDbUrl = "";
  let customDbName = "";
  let customDatabases = [];
  let activeModel = "";
  let geminiPresetKeys = [];

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

  // DOM Elements - Primary Controls
  const aiPrompt = document.getElementById('aiPrompt');
  const sqlQueryTextarea = document.getElementById('sqlQuery');
  const translateBtn = document.getElementById('translateBtn');
  const runBtn = document.getElementById('runBtn');
  const clearHistoryBtn = document.getElementById('clearHistoryBtn');
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
  const connDbName = document.getElementById('connDbName');
  const connDbDot = document.getElementById('connDbDot');
  const connDbUser = document.getElementById('connDbUser');
  const modelSelect = document.getElementById('modelSelect');

  // DOM Elements - Help Modal
  const helpModal = document.getElementById('helpModal');
  const helpBtn = document.getElementById('helpBtn');
  const helpModalCloseBtn = document.getElementById('helpModalCloseBtn');

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

  let isGoogleAuthInitialized = false;

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

        if (!isGoogleAuthInitialized) {
          isGoogleAuthInitialized = true;
          google.accounts.id.prompt();
        }
      }
    }
  }

  function initGoogleAuth(clientId) {
    renderAuthUI(clientId);
  }

  function setButtonsDisabled(disabled) {
    if (translateBtn) translateBtn.disabled = disabled;
    if (runBtn) runBtn.disabled = disabled;
    if (micBtn) micBtn.disabled = disabled;
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
    const turns = Math.floor(chatHistory.length / 2);
    const clearTitleEl = document.querySelector('.btn-clear-title');
    if (clearTitleEl) {
      clearTitleEl.textContent = `(${turns})`;
    }
    const clearMsgEl = document.getElementById('historyActionMsg');
    if (clearMsgEl) {
      clearMsgEl.textContent = '';
    }
    updateHistoryNavButtons();
  }

  function updateHistoryNavButtons() {
    // chatHistory holds [user, model] pairs. When only one turn (2 entries)
    // remains, it's already the oldest turn on screen - going back from
    // there would pop it and leave the UI blank, so disable one step early.
    const atOldestTurn = chatHistory.length <= 2;
    const atNewestTurn = futureChatHistory.length < 2;

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

    const config = loadConfig();
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          sql: 'SELECT current_user, current_database();',
          database_url: config.dbUrl,
          model: config.model
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

    if ((data && data.is_cloud_run && !data.authenticated) || (!data?.database_name && !data?.custom_database_name)) {
      if (badge) badge.style.display = 'none';
      return;
    }

    if (badge) badge.style.display = '';

    const matchedPreset = CONFIGURED_DBS.find(db => db.url === data.active_database_url);
    const dbDisplayName = matchedPreset?.name || data.custom_database_name || data.database_name || "Database";

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

  function renderModelSelect(currentModel) {
    const modelSelectEl = document.getElementById('modelSelect') || modelSelect;
    if (!modelSelectEl) return;
    const models = geminiPresetKeys && geminiPresetKeys.length > 0 ? geminiPresetKeys : [currentModel].filter(Boolean);
    if (models.length === 0) {
      modelSelectEl.innerHTML = '<option value="">Default Model</option>';
      return;
    }
    let html = '';
    models.forEach(m => {
      const isSelected = m === currentModel;
      html += `<option value="${m}" ${isSelected ? 'selected' : ''}>${m}</option>`;
    });
    modelSelectEl.innerHTML = html;
  }

  async function fetchBackendConfig() {
    try {
      const response = await fetch('/api/config', { headers: getApiHeaders(), credentials: 'same-origin' });
      const data = await response.json();

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
        customDatabases = [{ name: customDbName || "Custom", url: customDbUrl }];
      } else {
        customDatabases = [];
      }

      geminiPresetKeys = data.gemini_preset_keys || data.models || [];
      if (data.active_model) {
        activeModel = data.active_model;
      } else if (data.default_model) {
        activeModel = data.default_model;
      } else if (geminiPresetKeys.length > 0 && !activeModel) {
        activeModel = geminiPresetKeys[0];
      }

      if (data.auth_enabled && data.google_client_id) {
        initGoogleAuth(data.google_client_id);
      }

      if (data.active_database_url) {
        ACTIVE_DB_URL = data.active_database_url;
      } else if (!ACTIVE_DB_URL && DEFAULT_DB_URL) {
        ACTIVE_DB_URL = DEFAULT_DB_URL;
      }

      renderDbRadioButtons();
      loadConfigIntoUI();
      
      await updateConnectionDetails(data);
    } catch (err) {
      console.error("Failed to fetch backend configuration:", err);
      if (connDbDot) connDbDot.className = 'status-dot disconnected';
    }
  }

  function renderCustomDbRows(activeUrl) {
    const container = document.getElementById('customDbsContainer');
    if (!container) return;

    const rows = customDatabases.filter(db => db.url && db.url.trim() !== "");

    let html = '';
    rows.forEach((db, index) => {
      const maskedVal = maskConnectionUrl(db.url);
      const isSelected = activeUrl === db.url;
      html += `
        <label class="radio-option" style="display: flex; align-items: center; gap: 0.6rem; width: 100%;">
          <input type="radio" name="db_connection_option" value="custom-${index}" data-dbname="${db.name}" ${isSelected ? 'checked' : ''}>
          <input type="text" class="config-input custom-db-url-input" data-index="${index}" placeholder="postgresql://user:password@host:5432/dbname" value="${maskedVal}" style="flex: 1;" autocomplete="off">
        </label>
      `;
    });

    const nextIndex = rows.length;
    html += `
      <label class="radio-option" style="display: flex; align-items: center; gap: 0.6rem; width: 100%;">
        <input type="radio" name="db_connection_option" value="custom-${nextIndex}" data-dbname="Custom" id="radioNewCustomDb">
        <input type="text" class="config-input custom-db-url-input" data-index="${nextIndex}" placeholder="postgresql://user:password@host:5432/dbname" value="" style="flex: 1;" autocomplete="off">
      </label>
    `;

    container.innerHTML = html;

    const inputs = container.querySelectorAll('.custom-db-url-input');
    inputs.forEach(input => {
      const index = parseInt(input.dataset.index);
      const radio = container.querySelector(`input[value="custom-${index}"]`);

      input.addEventListener('focus', () => {
        if (radio) radio.checked = true;
      });

      input.addEventListener('input', () => {
        if (radio) radio.checked = true;
        const val = input.value.trim();

        if (index < customDatabases.length) {
          if (val === "") {
            customDatabases.splice(index, 1);
            renderCustomDbRows(activeUrl);
            const currentInputs = container.querySelectorAll('.custom-db-url-input');
            if (currentInputs.length > 0) {
              const targetInp = currentInputs[Math.min(index, currentInputs.length - 1)];
              if (targetInp) targetInp.focus();
            }
            return;
          }
          const unmaskedUrl = unmaskConnectionUrl(val, customDatabases[index].url);
          customDatabases[index].url = unmaskedUrl;
          customDatabases[index].name = getDatabaseNameFromUrl(unmaskedUrl);
          if (radio) radio.dataset.dbname = customDatabases[index].name;
        } else {
          const unmaskedUrl = unmaskConnectionUrl(val, "");
          customDatabases.push({
            name: getDatabaseNameFromUrl(unmaskedUrl),
            url: unmaskedUrl
          });
          if (radio) radio.dataset.dbname = customDatabases[index].name;

          renderCustomDbRows(activeUrl);
          const newInputs = container.querySelectorAll('.custom-db-url-input');
          const matchingInput = Array.from(newInputs).find(inp => parseInt(inp.dataset.index) === index);
          if (matchingInput) {
            matchingInput.focus();
            matchingInput.setSelectionRange(val.length, val.length);
          }
        }
      });
    });
  }

  function renderDbRadioButtons(currentDbUrl) {
    const radioGroup = document.getElementById('modalDbRadioGroup');
    if (!radioGroup) return;

    const activeUrl = currentDbUrl || ACTIVE_DB_URL || DEFAULT_DB_URL;
    const matchedPresetUrl = getMatchingPresetUrl(activeUrl);

    const isCustom = !matchedPresetUrl;

    let html = '';
    
    CONFIGURED_DBS.forEach((db) => {
      const isSelected = !isCustom && Boolean(matchedPresetUrl && db.url === matchedPresetUrl);
      html += `
        <label class="radio-option">
          <input type="radio" name="db_connection_option" value="${db.url}" data-dbname="${db.name}" ${isSelected ? 'checked' : ''}>
          <span class="radio-label">${db.name}</span>
        </label>
      `;
    });

    html += `<div id="customDbsContainer" style="display: flex; flex-direction: column; gap: 0.5rem; width: 100%;"></div>`;

    radioGroup.innerHTML = html;

    renderCustomDbRows(activeUrl);
  }

  async function triggerConfigSave({ closeModal = false, dbUrl = null, dbName = null } = {}) {
    let dbUrlValue = dbUrl;
    let dbNameValue = dbName;
    let isCustomOption = false;

    const modelSelectEl = document.getElementById('modelSelect') || modelSelect;
    if (modelSelectEl && modelSelectEl.value) {
      activeModel = modelSelectEl.value.trim();
    }

    const container = document.getElementById('customDbsContainer');
    if (container) {
      const inputs = container.querySelectorAll('.custom-db-url-input');
      inputs.forEach(input => {
        const index = parseInt(input.dataset.index);
        const val = input.value.trim();
        if (val) {
          if (index < customDatabases.length) {
            const unmasked = unmaskConnectionUrl(val, customDatabases[index].url);
            customDatabases[index].url = unmasked;
            customDatabases[index].name = getDatabaseNameFromUrl(unmasked);
          } else {
            const unmasked = unmaskConnectionUrl(val, "");
            customDatabases.push({
              name: getDatabaseNameFromUrl(unmasked),
              url: unmasked
            });
          }
        }
      });
    }

    if (dbUrlValue === null || dbNameValue === null) {
      const selectedDbRadio = document.querySelector('input[name="db_connection_option"]:checked');
      if (selectedDbRadio) {
        if (selectedDbRadio.value.startsWith('custom-')) {
          isCustomOption = true;
          const index = parseInt(selectedDbRadio.value.split('-')[1]);
          const selectedDb = customDatabases[index];
          if (selectedDb && selectedDb.url) {
            dbUrlValue = selectedDb.url;
            dbNameValue = selectedDb.name;
          } else {
            const firstCustom = customDatabases.find(d => d.url && d.url.trim() !== "");
            if (firstCustom) {
              dbUrlValue = firstCustom.url;
              dbNameValue = firstCustom.name;
            } else {
              dbUrlValue = DEFAULT_DB_URL;
              dbNameValue = "Default DB";
            }
          }
          customDbName = dbNameValue;
          customDbUrl = dbUrlValue;
        } else {
          dbUrlValue = selectedDbRadio.value;
          const matchedDb = CONFIGURED_DBS.find(db => db.url === dbUrlValue);
          dbNameValue = matchedDb ? matchedDb.name : "Preset DB";
        }
      } else {
        dbUrlValue = DEFAULT_DB_URL;
        dbNameValue = "Default DB";
      }
    }

    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          database_name: dbNameValue,
          database_url: dbUrlValue,
          is_custom: isCustomOption,
          model: activeModel,
          custom_databases: customDatabases.filter(d => d.url && d.url.trim() !== "")
        })
      });

      if (response.ok) {
        const data = await response.json();
        if (data.active_database_url) {
          ACTIVE_DB_URL = data.active_database_url;
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
        if (data.active_model) {
          activeModel = data.active_model;
        }
        
        await updateConnectionDetails(data);
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
      dbUrl: ACTIVE_DB_URL || DEFAULT_DB_URL,
      model: activeModel
    };
  }

  function loadConfigIntoUI() {
    const config = loadConfig();
    renderDbRadioButtons(config.dbUrl);
    renderModelSelect(config.model);
    updateHistoryTurnsSubtitle();
  }

  function closeConfigModal() {
    if (configModal) configModal.classList.add('hidden');
  }

  if (configTriggerBadge && configModal) {
    configTriggerBadge.addEventListener('click', async () => {
      await fetchBackendConfig();
      configModal.classList.remove('hidden');
    });
  }

  if (modalCloseBtn && configModal) {
    modalCloseBtn.addEventListener('click', closeConfigModal);
  }

  if (helpBtn && helpModal) {
    helpBtn.addEventListener('click', () => {
      helpModal.classList.remove('hidden');
    });
  }

  if (helpModalCloseBtn && helpModal) {
    helpModalCloseBtn.addEventListener('click', () => {
      helpModal.classList.add('hidden');
    });
  }

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

  async function translatePrompt() {
    await fetchBackendConfig();

    clearResultsDisplay();

    const promptText = aiPrompt ? aiPrompt.value.trim() : "";
    if (!promptText) return;

    setButtonsDisabled(true);

    const config = loadConfig();
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
            history: chatHistory,
            database_url: config.dbUrl,
            model: config.model,
            gemini_model: config.model
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
        chatHistory.push({
          role: 'user',
          text: promptText
        });
        chatHistory.push(modelEntry);
        chatHistory = chatHistory.slice(-20);
        futureChatHistory = []; // Clear forward stack on new translation
        updateHistoryTurnsSubtitle();

        if (isOpenHelp) {
          setSqlQuery('');
          pendingSqlHistoryEntry = null;
          clearResultsDisplay();

          if (helpModal) {
            helpModal.classList.remove('hidden');
          }
        } else if (isNoSql) {
          setSqlQuery('');
          pendingSqlHistoryEntry = null;
          renderNoSqlResponse(data.sql);
        } else {
          setSqlQuery(data.sql);
          pendingSqlHistoryEntry = { entry: modelEntry, sql: normalizeSqlForCompare(data.sql) };
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
  
    const config = loadConfig();
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: getApiHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          sql: sql,
          database_url: config.dbUrl,
          model: config.model
        })
      });
  
      const data = await response.json();
      if (response.ok && data.success) {
        renderMultiTurnResults(data.results);

        const promptText = aiPrompt && aiPrompt.value.trim() ? aiPrompt.value.trim() : "[Direct SQL Execution]";
        const summarizedResults = Array.isArray(data.results) ? data.results.map(summarizeResultForHistory) : [];

        const pendingEntryIsCurrent =
          pendingSqlHistoryEntry &&
          pendingSqlHistoryEntry.entry &&
          chatHistory.length >= 1 &&
          chatHistory[chatHistory.length - 1] === pendingSqlHistoryEntry.entry;

        if (pendingSqlHistoryEntry && !pendingEntryIsCurrent) {
          // Stale reference (e.g. left over from navigating through a no-SQL
          // turn) - drop it rather than risk mutating the wrong turn.
          pendingSqlHistoryEntry = null;
        }

        if (pendingEntryIsCurrent) {
          // SQL just generated by translate() and now executed for the first
          // time - fill in its results rather than creating a duplicate turn.
          pendingSqlHistoryEntry.entry.text = sql;
          pendingSqlHistoryEntry.entry.results = summarizedResults;
          pendingSqlHistoryEntry = null;
        } else {
          // Any other execution (direct SQL entry, or re-running a query
          // that isn't the pending just-generated one) is its own turn.
          chatHistory.push({ role: 'user', text: promptText });
          chatHistory.push({ role: 'model', text: sql, results: summarizedResults });
          chatHistory = chatHistory.slice(-20);
          futureChatHistory = [];
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

  if (aiPrompt) {
    aiPrompt.addEventListener('input', () => {
      setSqlQuery('');
    });

    aiPrompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        translatePrompt();
      }
    });
  }

  if (translateBtn) translateBtn.addEventListener('click', translatePrompt);
  if (runBtn) runBtn.addEventListener('click', () => executeSql());

  function restoreLatestTurn() {
    if (chatHistory.length >= 2) {
      const lastUserEntry = chatHistory[chatHistory.length - 2];
      const lastModelEntry = chatHistory[chatHistory.length - 1];
      
      if (aiPrompt) {
        aiPrompt.value = (lastUserEntry && lastUserEntry.text !== "[Direct SQL Execution]") ? lastUserEntry.text : '';
      }
      
      if (lastModelEntry && lastModelEntry.text) {
        const sqlText = lastModelEntry.text;
        const isNoSql = sqlText.startsWith('*** NO SQL ***');
        
        if (isNoSql) {
          setSqlQuery('');
          pendingSqlHistoryEntry = null;
          renderNoSqlResponse(sqlText);
        } else {
          setSqlQuery(sqlText);

          const alreadyExecuted = lastModelEntry.results && Array.isArray(lastModelEntry.results);
          if (alreadyExecuted) {
            // This turn is done - viewing it again must never let a
            // subsequent Run overwrite its stored results in place.
            pendingSqlHistoryEntry = null;
            renderMultiTurnResults(lastModelEntry.results);
          } else {
            // Genuinely still awaiting its first execution.
            pendingSqlHistoryEntry = { 
              entry: lastModelEntry, 
              sql: normalizeSqlForCompare(sqlText) 
            };
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
      pendingSqlHistoryEntry = null;
      clearResultsDisplay();
    }
  }

  if (goBackBtn) {
    goBackBtn.addEventListener('click', () => {
      if (chatHistory.length >= 2) {
        const modelEntry = chatHistory.pop();
        const userEntry = chatHistory.pop();
        futureChatHistory.push(userEntry);
        futureChatHistory.push(modelEntry);
        
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
      }
    });
  }

  if (goForwardBtn) {
    goForwardBtn.addEventListener('click', () => {
      if (futureChatHistory.length >= 2) {
        const modelEntry = futureChatHistory.pop();
        const userEntry = futureChatHistory.pop();
        chatHistory.push(userEntry);
        chatHistory.push(modelEntry);
        
        updateHistoryTurnsSubtitle();
        restoreLatestTurn();
      }
    });
  }

  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener('click', () => {
      try {
        chatHistory = [];
        futureChatHistory = [];
        pendingSqlHistoryEntry = null;
        setSqlQuery('');
        if (aiPrompt) aiPrompt.value = '';
        clearResultsDisplay();
        if (resultsBody) resultsBody.innerHTML = '<tr><td class="text-center text-muted py-8">The answer will appear here...</td></tr>';

        updateHistoryTurnsSubtitle();
        const msgEl = document.getElementById('historyActionMsg');
        if (msgEl) {
          msgEl.textContent = 'Chat history cleared successfully.';
          msgEl.style.color = 'var(--primary, #10b981)';
        }
      } catch (err) {
        console.error("Failed to clear chat history:", err);
        const msgEl = document.getElementById('historyActionMsg');
        if (msgEl) {
          msgEl.textContent = 'Failed to clear chat history';
          msgEl.style.color = 'var(--danger, #f87171)';
        }
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

  if (aiPrompt) aiPrompt.focus();
});