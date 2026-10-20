/* Client-only controls; the target handler deliberately has no server call. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {chromium} = require('playwright');
const source = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let shape = 'native';
const server = http.createServer((_request, response) => {
  const button = shape === 'native'
    ? '<button id="confirm-automatic-run-button">Confirm and start processing</button>'
    : '<div id="confirm-automatic-run-button"><button>Confirm and start processing</button></div>';
  response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
  response.end('<!doctype html><meta charset="utf-8"><style>' + source.APP_CSS + '</style>'
    + '<body>' + button
    + '<div id="automatic-run-timing" data-run-state="ready">Est: preview</div>'
    + '<div class="automatic-run-activity" data-run-state="ready"><div class="automatic-run-progress-label">Ready</div></div>'
    + '<script>const host=document.getElementById("confirm-automatic-run-button");'
    + 'const button=host.querySelector("button")||host;window.targetCalls=[];'
    + 'button.addEventListener("click",()=>window.targetCalls.push({disabled:button.disabled,text:button.textContent}));</script>');
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true,
    ...(process.platform === 'win32' ? {channel: process.env.PDF_UI_BROWSER_CHANNEL || 'msedge'} : {})});
  try {
    for (shape of ['native', 'wrapped']) {
      const page = await browser.newPage();
      await page.goto('http://127.0.0.1:' + server.address().port);
      await page.evaluate(js => (0, eval)('(' + js + ')()'), source.APP_JS);
      await page.waitForFunction(() => document.documentElement.dataset.ragConfirmPresentationBound === 'true');
      const button = page.locator('button#confirm-automatic-run-button, #confirm-automatic-run-button button');
      assert.equal(await button.evaluate(element => element.classList.contains('rag-run-processing')), false,
        shape + ': an idle Confirm label is not an accepted processing state');
      await button.click();
      assert.deepEqual(await page.evaluate(() => window.targetCalls), [{disabled: true, text: 'Processing…'}],
        shape + ': immediate capture feedback must precede, not suppress, the target handler');
      assert.equal(await button.getAttribute('aria-busy'), 'true');
      assert.equal(await page.locator('.automatic-run-activity').getAttribute('data-run-state'), 'preparing');
      assert.equal(await button.evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(37, 99, 235)');
      await button.evaluate(element => { element.textContent = 'Processing successful ✓'; });
      await page.waitForFunction(() => {
        const host = document.getElementById('confirm-automatic-run-button');
        const button = host.querySelector('button') || host;
        return button.classList.contains('rag-run-success') && !button.classList.contains('rag-run-processing')
          && !button.hasAttribute('aria-busy');
      });
      assert.equal(await button.evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(22, 163, 74)');
      await button.evaluate(element => { element.textContent = 'Confirm and start processing'; });
      await page.waitForFunction(() => {
        const host = document.getElementById('confirm-automatic-run-button');
        return !(host.querySelector('button') || host).classList.contains('rag-run-success');
      });
      assert.equal(await button.evaluate(element => element.classList.contains('rag-run-processing')), false);
      console.log(shape + ': immediate feedback, target delivery, accepted colour, terminal cleanup and idle reset passed');
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; })
  .finally(() => server.close());
