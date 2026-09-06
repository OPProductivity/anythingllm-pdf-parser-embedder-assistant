"""Explicit idle-server lifecycle probe; never collected by pytest."""
import json
import time
import sys
from unittest import mock

from playwright.sync_api import sync_playwright

import anythingllm_pdf_assistant_cli as cli


def main():
    record = json.loads(cli._server_marker_path().read_text())
    assert not cli._owned_active_run_roots(cli._marker_root_pid(record))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        requests = []
        page.on('request', lambda r: requests.append((time.monotonic(), r.url)))
        page.on('pageerror', lambda e: print('JS ERROR', e))
        page.on('console', lambda m: print('CONSOLE', m.type, m.text) if m.type == 'error' else None)
        page.goto('http://127.0.0.1:7860/', wait_until='domcontentloaded')
        page.get_by_role('heading', name='PDF to AnythingLLM Text').wait_for()
        page.wait_for_timeout(2000)
        unexpected = '--unexpected' in sys.argv
        if unexpected:
            # Same owned, idle termination, but omit the intentional signal.
            with mock.patch.object(cli, '_notify_browser_stop'):
                print('STOP RESULT', cli._stop())
        else:
            print('STOP RESULT', cli._stop())
        page.wait_for_timeout(5000)
        print('URL', page.url)
        print('TAIL', page.locator('body').inner_text()[-500:])
        before = len(requests)
        page.wait_for_timeout(5000)
        print('LATER REQUESTS', [url for _, url in requests[before:]])
        if unexpected:
            assert 'Connection to the assistant was unexpectedly lost. Attempting reconnection' in page.locator('body').inner_text()
            assert len(requests) > before
        else:
            assert 'Assistant stopped through user intervention. Connection broken.' in page.locator('body').inner_text()
            assert page.locator('body').inner_text().strip() == 'Assistant stopped through user intervention. Connection broken.'
            assert page.get_by_role('link').count() == 0
            assert len(requests) == before
        browser.close()


if __name__ == '__main__':
    main()
