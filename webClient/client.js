document.addEventListener('DOMContentLoaded', async () => {
  let chatHistory = [];
  let DEFAULT_DB_URL = "";
  let ACTIVE_DB_URL = "";
  let DEFAULT_MODEL = "";
  let AVAILABLE_MODELS = [""];
  let CONFIGURED_DBS = [];

  // DOM Elements - Primary Controls
  const aiPrompt = document.getElementById('aiPrompt');
  const sqlQueryTextarea = document.getElementById('sqlQuery');
  const translateBtn = document.getElementById('translateBtn');
  const runBtn = document.getElementById('runBtn');
  const luckyBtn = document.getElementById('luckyBtn');
  const clearHistoryBtn = document.getElementById('clearHistoryBtn');
  const micBtn = document.getElementById('micBtn');

  // DOM Elements - Status & Stats
  const transStatus = document.getElementById('transStatus');
  const transTime = document.getElementById('transTime');
  const tokensTotal = document.getElementById('tokensTotal');
  const execStatus = document.getElementById('execStatus');
  const execTime = document.getElementById('execTime');
  const execRows = document.getElementById('execRows');

  // DOM Elements - Config Modal & Connection Status
  const configModal = document.getElementById('configModal');
  const configBtn = document.getElementById('configBtn');
  const modalCloseBtn = document.getElementById('modalCloseBtn');
  const configSaveBtn = document.getElementById('configSaveBtn');
  const connDbName = document.getElementById('connDbName');
  const connDbUser = document.getElementById('connDbUser');

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
  let chartInputTokensInstance = null;

  // Speech Recognition Instance
  let recognition = null;
  let isListening = false;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onstart = () => {
      isListening = true;
      if (micBtn) micBtn.classList.add('listening');
    };

    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      if (aiPrompt) {
        aiPrompt.value = transcript;
        aiPrompt.dispatchEvent(new Event('input'));
      }
    };

    recognition.onerror = (event) => {
      console.error('Speech recognition error:', event.error);
      if (micBtn) micBtn.classList.remove('listening');
      isListening = false;
    };

    recognition.onend = () => {
      if (micBtn) micBtn.classList.remove('listening');
      isListening = false;
    };
  } else if (micBtn) {
    micBtn.style.display = 'none';
  }

  if (micBtn && recognition) {
    micBtn.addEventListener('click', () => {
      if (isListening) {
        recognition.stop();
      } else {
        recognition.start();
      }
    });
  }

  let sqlEditor = null;
  if (sqlQueryTextarea && window.CodeMirror) {
    sqlEditor = window.CodeMirror.fromTextArea(sqlQueryTextarea, {
      mode: 'text/x-sql',
      theme: 'dracula',
      lineNumbers: true,
      lineWrapping: true,
      viewportMargin: Infinity
    });
    sqlEditor.setSize('100%', '100%');
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

  function setButtonsDisabled(disabled) {
    if (translateBtn) translateBtn.disabled = disabled;
    if (luckyBtn) luckyBtn.disabled = disabled;
    if (runBtn) runBtn.disabled = disabled;
    if (micBtn) micBtn.disabled = disabled;
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
        sqlEditor.setSize('100%', '100%');
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
  }

  function resetExecutionStats() {
    if (execStatus) {
      execStatus.textContent = "Ready";
      execStatus.className = "stat-val";
    }
    if (execTime) execTime.textContent = "—";
    if (execRows) execRows.textContent = "—";
  }

  function updateHistoryTurnsSubtitle() {
    const turns = Math.floor(chatHistory.length / 2);
    const clearTitleEl = document.querySelector('.btn-clear-title');
    if (clearTitleEl) {
      clearTitleEl.textContent = `Clear Chat History (${turns})`;
    }
    const clearMsgEl = document.getElementById('clearHistoryMsg');
    if (clearMsgEl) {
      clearMsgEl.textContent = '';
    }
  }

  function updateConnectionDetails(data) {
    if (data && data.database_name && data.username) {
      const dbName = data.database_name;
      const username = data.username;

      if (connDbName) connDbName.textContent = dbName;
      if (connDbUser) connDbUser.textContent = username;

      document.title = `yDyL`;
    }
  }

  function maskConnectionDbUrl(url) {
    if (!url) return "";
    const match = url.match(/^(postgresql:\/\/)([^:]+):([^@]+)(@.+)$/);
    if (match) {
      return `${match[1]}${match[2]}:****${match[4]}`;
    }
    return url;
  }

  function getMatchingPresetUrl(targetUrl) {
    if (!targetUrl || !CONFIGURED_DBS || CONFIGURED_DBS.length === 0) return null;
    const maskedTarget = maskConnectionDbUrl(targetUrl);
    const found = CONFIGURED_DBS.find(db => db.url === targetUrl || maskConnectionDbUrl(db.url) === maskedTarget);
    return found ? found.url : null;
  }

  async function fetchBackendConfig() {
    try {
      const response = await fetch('/api/config', { credentials: 'same-origin' });
      const data = await response.json();

      CONFIGURED_DBS = data.configured_databases || [];
      DEFAULT_DB_URL = data.default_database_url || "";
      ACTIVE_DB_URL = data.active_database_url || maskConnectionDbUrl(DEFAULT_DB_URL);
      DEFAULT_MODEL = data.default_model || "";
      if (Array.isArray(data.available_models) && data.available_models.length > 0) {
        AVAILABLE_MODELS = data.available_models;
      }

      // Sync model state in sessionStorage
      const storedModel = sessionStorage.getItem('crbot_model');
      if (!storedModel || !AVAILABLE_MODELS.includes(storedModel)) {
        sessionStorage.setItem('crbot_model', DEFAULT_MODEL);
      }
      
      // Clean up legacy localStorage items if present
      localStorage.removeItem('crbot_db_url');
      localStorage.removeItem('crbot_model');

      // Sync active database URL with latest backend state
      if (data.active_database_url) {
        sessionStorage.setItem('crbot_db_url', data.active_database_url);
      } else if (!sessionStorage.getItem('crbot_db_url') && DEFAULT_DB_URL) {
        sessionStorage.setItem('crbot_db_url', DEFAULT_DB_URL);
      }

      renderDbRadioButtons();
      renderModelRadioButtons();
      loadConfigIntoUI();
      
      updateConnectionDetails(data);
    } catch (err) {
      console.error("Failed to fetch backend configuration:", err);
    }
  }

  function renderDbRadioButtons(currentDbUrl) {
    const radioGroup = document.getElementById('modalDbRadioGroup');
    if (!radioGroup) return;

    const activeUrl = currentDbUrl || sessionStorage.getItem('crbot_db_url') || ACTIVE_DB_URL || DEFAULT_DB_URL;
    const matchedPresetUrl = getMatchingPresetUrl(activeUrl);

    let html = '';
    
    CONFIGURED_DBS.forEach((db) => {
      const isSelected = Boolean(matchedPresetUrl && db.url === matchedPresetUrl);
      html += `
        <label class="radio-option">
          <input type="radio" name="db_connection_option" value="${db.url}" ${isSelected ? 'checked' : ''}>
          <span class="radio-label">${db.name}</span>
        </label>
      `;
    });

    const isCustom = !matchedPresetUrl && Boolean(activeUrl);
    const customValue = isCustom ? activeUrl : '';

    html += `
      <label class="radio-option" style="display: flex; align-items: center; gap: 0.5rem;">
        <input type="radio" name="db_connection_option" value="custom" id="radioCustomDb" ${isCustom ? 'checked' : ''}>
        <input type="text" id="modalCustomDbUrl" class="config-input" placeholder="postgresql://user:password@host:5432/dbname" value="${customValue}" style="flex: 1;" autocomplete="off">
      </label>
    `;

    radioGroup.innerHTML = html;

    const customInput = document.getElementById('modalCustomDbUrl');
    const customRadio = document.getElementById('radioCustomDb');

    // Add event listeners to radio options to clear custom URL when preset radio buttons are clicked
    const dbRadios = radioGroup.querySelectorAll('input[name="db_connection_option"]');
    dbRadios.forEach(radio => {
      radio.addEventListener('change', () => {
        if (radio.value !== 'custom' && radio.checked) {
          if (customInput) customInput.value = '';
        }
      });
    });

    if (customInput) {
      customInput.addEventListener('focus', () => {
        if (customRadio) customRadio.checked = true;
      });
      customInput.addEventListener('input', () => {
        if (customRadio) customRadio.checked = true;
      });
    }
  }

  function renderModelRadioButtons() {
    const radioGroup = document.getElementById('modalGeminiModelGroup');
    if (!radioGroup) return;

    const currentSelected = sessionStorage.getItem('crbot_model') || DEFAULT_MODEL;

    radioGroup.innerHTML = AVAILABLE_MODELS.map(model => `
      <label class="radio-option">
        <input type="radio" name="gemini_model" value="${model}" ${model === currentSelected ? 'checked' : ''}>
        <span class="radio-label">${model}</span>
      </label>
    `).join('');
  }

  function loadConfig() {
    return {
      model: sessionStorage.getItem('crbot_model') || DEFAULT_MODEL,
      dbUrl: sessionStorage.getItem('crbot_db_url') || ACTIVE_DB_URL || DEFAULT_DB_URL
    };
  }

  function loadConfigIntoUI() {
    const config = loadConfig();

    renderDbRadioButtons(config.dbUrl);
    renderModelRadioButtons();

    const modelRadios = document.querySelectorAll('input[name="gemini_model"]');
    let matchingModelRadio = document.querySelector(`input[name="gemini_model"][value="${config.model}"]`);
    if (matchingModelRadio) {
      matchingModelRadio.checked = true;
    } else if (modelRadios.length > 0) {
      modelRadios[0].checked = true;
    }

    updateHistoryTurnsSubtitle();
  }
  
  async function triggerConfigSave({ closeModal = false, dbUrl = null, model = null } = {}) {
    let dbUrlValue = dbUrl;
    
    if (dbUrlValue === null) {
      const selectedDbRadio = document.querySelector('input[name="db_connection_option"]:checked');
      if (selectedDbRadio) {
        if (selectedDbRadio.value === 'custom') {
          const customInput = document.getElementById('modalCustomDbUrl');
          dbUrlValue = customInput ? customInput.value.trim() : "";
          if (!dbUrlValue && CONFIGURED_DBS.length > 0) {
            dbUrlValue = DEFAULT_DB_URL || CONFIGURED_DBS[0].url;
          }
        } else {
          dbUrlValue = selectedDbRadio.value;
        }
      } else {
        dbUrlValue = DEFAULT_DB_URL;
      }
    }
    
    let selectedModel = model;
    if (!selectedModel) {
      const selectedModelRadio = document.querySelector('input[name="gemini_model"]:checked');
      selectedModel = selectedModelRadio ? selectedModelRadio.value : DEFAULT_MODEL;
    }

    sessionStorage.setItem('crbot_model', selectedModel);

    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          database_url: dbUrlValue,
          model: selectedModel
        })
      });

      if (response.ok) {
        const data = await response.json();
        if (data.active_database_url) {
          ACTIVE_DB_URL = data.active_database_url;
          sessionStorage.setItem('crbot_db_url', ACTIVE_DB_URL);
        }
        
        updateConnectionDetails(data);
      }
    } catch (err) {
      console.error("Failed to save backend configuration:", err);
    }

    if (closeModal) {
      closeConfigModal();
    }
  }

  function closeConfigModal() {
    if (configModal) configModal.classList.add('hidden');
  }

  if (configBtn && configModal) {
    configBtn.addEventListener('click', async () => {
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
    });
  }

  function renderStatisticsCharts(statsData) {
    if (!statsData || statsData.length === 0 || typeof window.Chart === 'undefined') return;

    const dates = statsData.map(item => item.day_date || item.date || 'Unknown');
    const totalTranslations = statsData.map(item => item.total_translations || 0);
    const sumTotalTokens = statsData.map(item => item.sum_total_tokens || 0);
    const sumInputTokens = statsData.map(item => item.sum_input_tokens || 0);

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

    const ctxInputTokens = document.getElementById('chartInputTokensPerDay')?.getContext('2d');
    if (ctxInputTokens) {
      if (chartInputTokensInstance) chartInputTokensInstance.destroy();
      chartInputTokensInstance = new window.Chart(ctxInputTokens, {
        type: 'bar',
        data: {
          labels: dates,
          datasets: [{
            label: 'Sum of Input Tokens',
            data: sumInputTokens,
            backgroundColor: 'rgba(168, 85, 247, 0.6)',
            borderColor: '#a855f7',
            borderWidth: 1
          }]
        },
        options: commonOptions
      });
    }
  }

  async function loadHistoryData() {
    if (!historyTableHeader || !historyTableBody) return;

    historyTableHeader.innerHTML = '';
    historyTableBody.innerHTML = '<tr><td class="text-center text-muted py-8">Loading history...</td></tr>';

    try {
      const response = await fetch('/api/history', { credentials: 'same-origin' });
      const data = await response.json();

      if (response.ok && data.success) {
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

  function renderMultiTurnResults(results) {
    if (!resultsTabsNav) return;
    resultsTabsNav.innerHTML = '';

    if (!results || results.length === 0) {
      resultsTabsNav.classList.add('hidden');
      renderTableResult(null);
      return;
    }

    if (results.length === 1) {
      resultsTabsNav.classList.add('hidden');
      renderTableResult(results[0]);
      return;
    }

    resultsTabsNav.classList.remove('hidden');
    results.forEach((res, idx) => {
      const btn = document.createElement('button');
      btn.className = `result-tab-btn ${idx === 0 ? 'active' : ''}`;
      
      const sqlText = res.query || res.sql || res.statement || '';
      if (sqlText) {
        btn.setAttribute('title', sqlText);
      }

      const count = res.rowCount !== undefined ? res.rowCount : (res.rows ? res.rows.length : 0);
      btn.textContent = `Query ${idx + 1} (${count})`;

      btn.addEventListener('click', () => {
        document.querySelectorAll('.result-tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderTableResult(res);
      });
      resultsTabsNav.appendChild(btn);
    });

    renderTableResult(results[0]);
  }

  async function translatePrompt() {
    clearResultsDisplay();
    resetExecutionStats();

    const promptText = aiPrompt ? aiPrompt.value.trim() : "";
    if (!promptText) return false;

    setButtonsDisabled(true);

    if (transStatus) {
      transStatus.textContent = "Working...";
      transStatus.className = "stat-val status-working";
    }

    let success = false;
    const config = loadConfig();
    try {
      const response = await fetch('/api/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          prompt: promptText,
          history: chatHistory,
          gemini_model: config.model,
          database_url: config.dbUrl
        })
      });

      const data = await response.json();
      if (response.ok && data.sql) {
        setSqlQuery(data.sql);

        chatHistory.push({
          role: 'user',
          text: promptText
        });
        chatHistory.push({
          role: 'model',
          text: data.sql
        });
        chatHistory = chatHistory.slice(-10);
        updateHistoryTurnsSubtitle();

        if (transStatus) {
          transStatus.textContent = "Success";
          transStatus.className = "stat-val status-success";
        }
        if (transTime) transTime.textContent = `${data.duration} ms`;
        if (tokensTotal) tokensTotal.textContent = data.total_tokens || "—";
        success = true;
      } else {
        setSqlQuery('');

        if (transStatus) {
          transStatus.textContent = "Error";
          transStatus.className = "stat-val status-error";
        }
        if (transTime) transTime.textContent = "—";
        if (tokensTotal) tokensTotal.textContent = "—";

        resetExecutionStats();

        const errMsg = data.error || "An error occurred during translation.";
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

      if (transStatus) {
        transStatus.textContent = "Error";
        transStatus.className = "stat-val status-error";
      }
      if (transTime) transTime.textContent = "—";
      if (tokensTotal) tokensTotal.textContent = "—";

      resetExecutionStats();

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
    return success;
  }

  async function executeSql() {
    clearResultsDisplay();

    const sql = getSqlQuery();
    if (!sql) return;

    setButtonsDisabled(true);

    if (execStatus) {
      execStatus.textContent = "Executing...";
      execStatus.className = "stat-val status-working";
    }

    const config = loadConfig();
    try {
      const response = await fetch('/api/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          sql: sql,
          database_url: config.dbUrl
        })
      });

      const data = await response.json();
      if (response.ok && data.success) {
        if (execStatus) {
          execStatus.textContent = "Success";
          execStatus.className = "stat-val status-success";
        }
        if (execTime) execTime.textContent = `${data.executionTimeMs} ms`;
        if (execRows) execRows.textContent = data.rowCount;

        renderMultiTurnResults(data.results);
      } else {
        if (execStatus) {
          execStatus.textContent = "Error";
          execStatus.className = "stat-val status-error";
        }
        const errMsg = data.error || "An error occurred during SQL execution.";
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
      if (execStatus) {
        execStatus.textContent = "Error";
        execStatus.className = "stat-val status-error";
      }
      const errMsg = err.message || "Failed to reach the execution backend server.";
      console.error("Failed to execute SQL:", err);
    } finally {
      setButtonsDisabled(false);
    }
  }

  if (aiPrompt) {
    aiPrompt.addEventListener('input', () => {
      setSqlQuery('');

      if (transStatus) {
        transStatus.textContent = "Ready";
        transStatus.className = "stat-val";
      }
      if (transTime) transTime.textContent = "—";
      if (tokensTotal) tokensTotal.textContent = "—";

      resetExecutionStats();
    });

    aiPrompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        translatePrompt();
      }
    });
  }

  if (translateBtn) translateBtn.addEventListener('click', translatePrompt);
  if (runBtn) runBtn.addEventListener('click', executeSql);

  if (luckyBtn) {
    luckyBtn.addEventListener('click', async () => {
      const translated = await translatePrompt();
      if (translated) {
        await executeSql();
      }
    });
  }

  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener('click', () => {
      try {
        chatHistory = [];
        setSqlQuery('');
        if (aiPrompt) aiPrompt.value = '';
        if (transStatus) transStatus.textContent = "Ready";
        resetExecutionStats();
        clearResultsDisplay();
        if (resultsBody) resultsBody.innerHTML = '<tr><td class="text-center text-muted py-8">The answer will be displayed here.</td></tr>';

        updateHistoryTurnsSubtitle();
      } catch (err) {
        console.error("Failed to clear chat history:", err);
        const clearMsgEl = document.getElementById('clearHistoryMsg');
        if (clearMsgEl) {
          clearMsgEl.textContent = 'Failed to clear chat history';
          clearMsgEl.style.color = 'var(--danger, #f87171)';
        }
      }
    });
  }

  await fetchBackendConfig();

  if (aiPrompt) aiPrompt.focus();
});