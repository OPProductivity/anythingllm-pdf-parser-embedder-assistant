const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {chromium} = require('playwright');
const source = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const server = http.createServer((_request, response) => {
  response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
  response.end('<!doctype html><meta charset="utf-8"><style>' + source.APP_CSS + '</style>'
    + source.APP_BROWSER_THEME_HEAD + '<body>'
    + '<div id="automatic-pdf-upload"><div class="file-preview"><table><tbody></tbody></table></div></div>'
    + '<div id="batch-folder-file-selector"></div>'
    + '<div id="document-metadata-accordion"><button class="label-wrap"><span>Document metadata</span>'
    + '<span class="icon">▼</span></button></div>'
    + '<div class="metadata-status-transport" id="document-metadata-scan-status"><div class="metadata-scan-inline" data-state="idle" role="status" aria-live="polite">'
    + '<span class="metadata-scan-spinner"></span><span class="metadata-scan-copy"></span></div></div>');
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true,
    ...(process.platform === 'win32' ? {channel: process.env.PDF_UI_BROWSER_CHANNEL || 'chrome'} : {})});
  try {
    const page = await browser.newPage();
    await page.goto('http://127.0.0.1:' + server.address().port);
    const status = page.locator('#document-metadata-accordion .metadata-header-status');
    await status.waitFor({state: 'attached'});
    assert.equal(await status.getAttribute('data-state'), 'idle');
    await page.evaluate(() => {
      const row = document.createElement('td');
      row.setAttribute('aria-label', 'ED329380.pdf');
      row.textContent = 'ED329380.pdf';
      document.querySelector('#automatic-pdf-upload tbody').appendChild(row);
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-status')?.dataset.state === 'scanning');
    assert.match(await status.textContent(), /Preparing metadata preview for 1 PDF/);
    assert.equal(await page.locator('#document-metadata-scan-status').isVisible(), false);
    assert.equal(await status.isVisible(), true);
    assert.equal(await page.locator('#document-metadata-accordion > button.label-wrap').evaluate(node =>
      node.children[node.children.length - 2].classList.contains('metadata-header-status')), true);
    await page.locator('#document-metadata-scan-status').evaluate(node => {
      node.innerHTML = '<div class="metadata-scan-inline" data-state="scanning" role="status"><span class="metadata-scan-spinner"></span><span class="metadata-scan-copy">Inspecting metadata for preview 1 of 1…</span></div>';
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-copy')?.textContent?.includes('Inspecting metadata'));
    assert.equal((await status.textContent()).includes('OCR'), false);
    await page.locator('#document-metadata-scan-status').evaluate(node => {
      node.innerHTML = '<div class="metadata-scan-inline" data-state="ready" role="status"><span class="metadata-scan-copy">Metadata preview complete.</span></div>';
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-status')?.dataset.state === 'ready');
    await page.evaluate(() => document.querySelector('#automatic-pdf-upload tbody').replaceChildren());
    await page.waitForFunction(() => document.querySelector('.metadata-header-status')?.dataset.state === 'idle');

    await page.evaluate(() => {
      const host = document.querySelector('#batch-folder-file-selector');
      for (const name of ['first.pdf', 'second.pdf']) {
        const token = document.createElement('span');
        token.className = 'token'; token.textContent = name + ' ×'; host.appendChild(token);
      }
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-copy')?.textContent?.includes('2 PDFs'));
    await page.locator('#document-metadata-scan-status').evaluate(node => {
      node.innerHTML = '<div class="metadata-scan-inline" data-state="scanning" role="status"><span class="metadata-scan-copy">Inspecting metadata for preview 1 of 2…</span></div>';
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-copy')?.textContent?.includes('preview 1 of 2'));
    await page.locator('#document-metadata-scan-status').evaluate(node => {
      node.innerHTML = '<div class="metadata-scan-inline" data-state="ready" role="status"><span class="metadata-scan-copy">Preview ready for 2 of 2 selected PDFs.</span></div>';
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-copy')?.textContent?.includes('2 of 2 selected PDFs'));

    await page.evaluate(() => {
      const token = document.createElement('span'); token.className = 'token';
      token.textContent = 'third.pdf ×'; document.querySelector('#batch-folder-file-selector').appendChild(token);
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-status')?.dataset.state === 'scanning');
    assert.match(await status.textContent(), /Preparing metadata preview for 3 PDFs/);
    await page.locator('#document-metadata-scan-status').evaluate(node => {
      node.innerHTML = '<div class="metadata-scan-inline" data-state="warning" role="status"><span class="metadata-scan-copy">Metadata preview finished with an error.</span></div>';
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-status')?.dataset.state === 'warning');
    console.log('Immediate single, batch, progress, completion, reselection and warning states passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
