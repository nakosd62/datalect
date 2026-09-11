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

  // Idle state first - confirm no stray colored bar shows when nothing
  // is in flight.
  await page.screenshot({ path: '/tmp/results_idle.png', clip: { x: 0, y: 250, width: 1100, height: 200 } });

  // Simulate exactly what showRetryStatus()+setButtonsDisabled(true) do
  // to the DOM, to preview the in-flight banner+Cancel state without
  // needing a real backend round trip.
  await page.evaluate(() => {
    const status = document.getElementById('resultsRetryStatus');
    const stop = document.getElementById('stopBtn');
    status.innerHTML = '<span class="retry-status-icon animate-spin">⟳</span> The model ran into a transient error - retrying (attempt 2 of 5)...';
    status.classList.remove('hidden');
    stop.classList.remove('hidden');
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: '/tmp/results_retrying.png', clip: { x: 0, y: 250, width: 1100, height: 200 } });

  // Cancel visible but no status text yet (the real gap this design has
  // to handle - see the CSS comment).
  await page.evaluate(() => {
    const status = document.getElementById('resultsRetryStatus');
    status.classList.add('hidden');
    status.innerHTML = '';
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: '/tmp/results_cancel_only.png', clip: { x: 0, y: 250, width: 1100, height: 200 } });

  // Size comparison: Execute button vs Cancel button bounding boxes.
  const runBox = await page.locator('#runBtn').boundingBox();
  const stopBox = await page.evaluate(() => {
    const el = document.getElementById('stopBtn');
    el.classList.remove('hidden');
    const r = el.getBoundingClientRect();
    return { width: r.width, height: r.height };
  });
  console.log('runBtn box', runBox);
  console.log('stopBtn box', stopBox);

  await browser.close();
})();
