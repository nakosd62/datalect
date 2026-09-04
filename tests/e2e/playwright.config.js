// tests/e2e/playwright.config.js
//
// E2E suite for yDyL's web client. Runs against a REAL Flask server (real
// SqliteStateStore, real /api/config, /api/history, session handling) - the
// only things mocked are /api/translate, /api/execute, and /api/ping,
// intercepted in-browser via page.route() in each spec (or by fixtures.js's
// default mocks), so no real Gemini/BigQuery/Postgres credentials are ever
// needed to run this suite.
//
// This config (and package.json/package-lock.json next to it) live under
// tests/e2e/ rather than the repo root on purpose, to keep the root clean
// for the primary Python project - run npm/playwright commands from this
// directory (`cd tests/e2e && npx playwright test`), or use run_tests.sh
// from the repo root, which does that for you.
//
// The server is launched with its cwd pointed at .e2e-runtime/ (a scratch
// directory next to this file, gitignored) so its relative
// "state/ydyl_state.db" SQLite file never touches a developer's real local
// state - see app_config.py's TRANSLATION_STATS_DB_PATH. It's also
// launched on a dedicated port (CRBOT_PORT=3100) distinct from the normal
// dev-server port (3000) so `run_server.sh` and this suite can't collide.
//
// No auth env vars (GOOGLE_CLIENT_ID / K_SERVICE / GCP_PROJECT_ID) are set,
// so the server runs in local-dev mode: every request resolves to the
// "global" session identity with full read/write access (no anonymous
// restrictions to work around) and state persists to SQLite, not Firestore.

const path = require('path');
const fs = require('fs');

const REPO_ROOT = path.join(__dirname, '..', '..');
const RUNTIME_DIR = path.join(__dirname, '.e2e-runtime');
const PORT = 3100;

if (!fs.existsSync(RUNTIME_DIR)) {
  fs.mkdirSync(RUNTIME_DIR, { recursive: true });
}

// Prefer the project's own venv if it exists (matches run_server.sh), but
// fall back to whatever `python3` resolves to on PATH so this also works
// in a fresh checkout that hasn't run run_server.sh yet.
const venvPython = path.join(REPO_ROOT, 'venv', 'bin', 'python3');
const pythonBin = fs.existsSync(venvPython) ? venvPython : 'python3';
const serverScript = path.join(REPO_ROOT, 'server', 'server.py');

module.exports = {
  testDir: '.',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  // Every spec shares one real Flask dev server process (see webServer
  // below) - server.py runs it with Werkzeug's default single-threaded
  // app.run(), same as production (see Dockerfile), so it only ever
  // handles one request at a time. Each test already gets its own private
  // server-side state via a unique crbot_user_id cookie (see fixtures.js),
  // but concurrent workers still queue up on that single thread, which can
  // trip assertion timeouts under load in a constrained environment.
  // Running fully serially trades a bit of wall-clock time for
  // determinism - worth it for a local-only suite with no CI to parallelize
  // across machines anyway.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],

  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // Some CI/sandbox images ship a pre-provisioned Chromium at this path
    // instead of expecting `npx playwright install` to fetch one - use it
    // when present. No effect on a normal local machine (the path simply
    // won't exist, and Playwright falls back to its own managed browser).
    ...(fs.existsSync('/opt/pw-browsers/chromium')
      ? { launchOptions: { executablePath: '/opt/pw-browsers/chromium' } }
      : {}),
  },

  webServer: {
    command: `${pythonBin} ${serverScript}`,
    cwd: RUNTIME_DIR,
    env: {
      CRBOT_HOSTNAME: '127.0.0.1',
      CRBOT_PORT: String(PORT),
      // Guarantees this server process never loads a real developer .env -
      // see app_config.py's YDYL_SKIP_DOTENV handling. Without this, a
      // real repo-root .env (real Gemini keys, a real DATABASE_PRESETS_FILE)
      // would silently override every env var set here, since
      // load_dotenv() searches by walking up from app_config.py's own
      // location, not from this process's cwd.
      YDYL_SKIP_DOTENV: '1',
      // Deliberately no GOOGLE_CLIENT_ID / K_SERVICE / GCP_PROJECT_ID /
      // DATABASE_PRESETS_FILE - local-dev defaults (SQLite state, single
      // synthetic "Default DB" preset, no auth gating).
      //
      // Playwright merges this object ON TOP OF process.env for the spawned
      // server (it does not replace it) - so YDYL_SKIP_DOTENV above only
      // stops a repo-root .env FILE from being loaded; it does nothing
      // about these same-named variables already exported in whoever's
      // shell runs `npx playwright test` (e.g. for their own everyday use
      // of the app outside this suite). _default_fleet_provider()/
      // LlmProvider.default_model (translate_routes.py) read DEFAULT_MODEL/
      // GOOGLE_MODELS/ANTHROPIC_MODELS/OPENAI_MODELS live from the
      // environment on every request, with no way for a test to override
      // them at the browser layer the way /api/translate etc. are mocked -
      // so model-selection.spec.js's "shows the default model on load"
      // assertions (hardcoded to this app's own fallback, gemini-3.6-flash)
      // would otherwise pass or fail depending on the developer's personal
      // shell config, not on this app's actual code. Blanked here so the
      // suite's declared default is what these vars actually resolve to,
      // for every developer, regardless of what their own shell exports.
      DEFAULT_MODEL: '',
      GOOGLE_MODELS: '',
      ANTHROPIC_MODELS: '',
      OPENAI_MODELS: '',
    },
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 20_000,
    // 'ignore' keeps the Flask dev server's werkzeug access logs and the
    // expected "can't connect to fake test DB" tracebacks (from
    // checkDbStatus's background /api/ping ping) out of normal test
    // output. Playwright still buffers this output internally and prints it
    // if the server fails to start within the timeout above, and it can be
    // re-enabled on demand with `DEBUG=pw:webserver npx playwright test`.
    stdout: 'ignore',
    stderr: 'ignore',
  },
};
