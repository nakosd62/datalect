const { chromium } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  await page.addInitScript(() => {
    try { localStorage.setItem('ydyl_tour_dismissed', '1'); } catch (e) {}
  });
  await page.goto('http://127.0.0.1:3100/');
  await page.waitForTimeout(500);
  const skipBtn = await page.$('.tour-skip-btn');
  if (skipBtn) { await skipBtn.click(); await page.waitForTimeout(200); }
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await page.waitForTimeout(200);
  await page.screenshot({ path: '/tmp/results_idle_light.png', clip: { x: 0, y: 250, width: 1100, height: 200 } });

  await page.evaluate(() => {
    const status = document.getElementById('resultsRetryStatus');
    const stop = document.getElementById('stopBtn');
    status.innerHTML = '<span class="retry-status-icon animate-spin">⟳</span> The model ran into a transient error - retrying (attempt 2 of 5)...';
    status.classList.remove('hidden');
    stop.classList.remove('hidden');
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: '/tmp/results_retrying_light.png', clip: { x: 0, y: 250, width: 1100, height: 200 } });
  await browser.close();
})();
