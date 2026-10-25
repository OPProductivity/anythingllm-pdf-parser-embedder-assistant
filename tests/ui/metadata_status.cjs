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
    + '<span class="icon">▼</span></button><div id="metadata-tables"></div></div>'
    + ['title', 'author', 'short-label', 'publisher'].map(field =>
      '<div hidden id="automatic-' + field + '-override-transport"><textarea></textarea></div>').join('')
    + '<div hidden id="automatic-document-overrides-transport"><textarea>{}</textarea></div>'
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

    // These are the actual Python-rendered tables and application JS. No PDF
    // inference is simulated as correctness evidence: this exercises early
    // editing and HTML replacement before/during/after metadata detection.
    await page.locator('#metadata-tables').evaluate((node, html) => { node.innerHTML = html; }, source.pending);
    const editors = page.locator('.metadata-override-input');
    assert.equal(await editors.count(), 8);
    const titleSuggestions = page.locator('[data-metadata-known="filename"]');
    assert.deepEqual(await titleSuggestions.allTextContents(), ['first', 'second']);
    const firstAuthor = page.locator('[data-metadata-override="document_author"]').first();
    const secondPublisher = page.locator('[data-metadata-override="document_publisher"]').nth(1);
    await firstAuthor.fill('First independent author');
    await secondPublisher.fill('Second independent press');
    const transport = page.locator('#automatic-document-overrides-transport textarea');
    const saved = JSON.parse(await transport.inputValue());
    const documents = Object.values(saved);
    assert.equal(documents.filter(item => item.document_author === 'First independent author').length, 1);
    assert.equal(documents.filter(item => item.document_publisher === 'Second independent press').length, 1);

    // A new file-list mutation invokes the immediate loading pass. Known
    // filename suggestions survive it; unknown author/publisher cells load.
    await page.evaluate(() => {
      const token = document.createElement('span'); token.className = 'token'; token.textContent = 'fourth.pdf ×';
      document.querySelector('#batch-folder-file-selector').appendChild(token);
    });
    await page.waitForFunction(() => document.querySelector('.metadata-header-copy')?.textContent?.includes('4 PDFs'));
    assert.deepEqual(await titleSuggestions.allTextContents(), ['first', 'second']);
    assert.equal(await editors.evaluateAll(nodes => nodes.every(node => !node.disabled)), true);
    await page.locator('#metadata-tables').evaluate((node, html) => { node.innerHTML = html; }, source.pending);
    await page.waitForFunction(() => document.querySelectorAll('[data-metadata-override="document_author"]')[0]?.value === 'First independent author');
    assert.equal(await secondPublisher.inputValue(), 'Second independent press');
    assert.equal(await page.locator('[data-metadata-override="document_author"]').nth(1).inputValue(), '');
    assert.equal(await page.locator('[data-metadata-override="document_publisher"]').first().inputValue(), '');
    await secondPublisher.fill('Second press after replacement');
    assert.equal(Object.values(JSON.parse(await transport.inputValue())).filter(item =>
      item.document_publisher === 'Second press after replacement').length, 1);
    console.log('Immediate single, batch, progress, completion, reselection and warning states passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
