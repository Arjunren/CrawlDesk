"""Deterministic browser-to-Python acceptance checks. No external crawl requests."""
from email.message import Message
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import DesktopAPI
from crawler import Fetcher, FetchResult
from playwright.sync_api import sync_playwright


def fixture_fetch(self, url, max_bytes, is_allowed=None):
    self.limiter.wait()
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    if url.endswith("robots.txt"):
        return FetchResult(url, 200, headers, b"User-agent: *\nDisallow: /private\n")
    path = url.removeprefix("https://fixture.example")
    pages = {
        "/": '<title>Fixture home</title><meta name="description" content="Home"><h1>Home</h1>'
             '<a href="/about">About</a><a href="/docs">Docs</a><a href="/missing">Missing</a>',
        "/about": '<title>&lt;img src=x onerror=window.__xss=true&gt;</title><h1>About</h1>',
        "/docs": '<title>Documentation</title><a href="/docs/quickstart">Quickstart</a>',
        "/docs/quickstart": '<title>Quickstart</title><h1>Start</h1>',
    }
    time.sleep(.1)
    return FetchResult(url, 200 if path in pages else 404, headers, pages.get(path, "Missing").encode())


def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, patch.object(Fetcher, "fetch", fixture_fetch), sync_playwright() as p:
        api = DesktopAPI(Path(directory)/"history")
        target = Path(directory)/"results.csv"
        class SaveWindow:
            def create_file_dialog(self, *args, **kwargs):
                return [str(target)]
        api._window = SaveWindow()
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width":1440,"height":1000}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        methods = ["get_state", "get_history", "start_crawl", "pause_crawl", "stop_crawl", "load_demo", "load_history", "export_results", "get_preferences", "save_preferences"]
        page.expose_function("desktopCall", lambda method, args: getattr(api, method)(*args))
        page.add_init_script("window.pywebview={api:{}};" + "".join(
            f"window.pywebview.api.{method}=(...args)=>window.desktopCall('{method}',args);" for method in methods))
        page.goto((ROOT/"ui"/"index.html").as_uri())
        page.wait_for_function("document.getElementById('connection').textContent === 'Desktop connected'")
        page.screenshot(path=str(output/"overview.png"), full_page=True)
        page.locator("#demo-button").click()
        page.wait_for_function("document.getElementById('pages-count').textContent === '6'")
        assert page.locator("#results-body tr").count() == 6
        page.locator("#toast").evaluate("element => { element.hidden = true; }")
        page.screenshot(path=str(output/"sample.png"), full_page=True)
        page.locator("#search").fill("docs")
        assert page.locator("#results-body tr").count() == 2
        page.locator("#search").fill("")
        page.locator("#tab-issues").click()
        assert page.locator("#results-body tr").count() == 3
        page.locator("#tab-pages").click()
        page.locator("#status-filter").select_option("errors")
        assert page.locator("#results-body tr").count() == 1
        page.locator("#status-filter").select_option("all")
        page.locator("#sort-order").select_option("slow")
        assert "Stories" in page.locator("#results-body tr").first.inner_text()
        page.locator("#theme-toggle").click()
        assert page.locator("body").evaluate("element => element.classList.contains('dark')")
        page.locator("#results-body tr").first.click()
        assert page.locator("#page-drawer").is_visible()
        page.screenshot(path=str(output/"dark-inspector.png"), full_page=True)
        page.keyboard.press("Escape")
        page.locator("#theme-toggle").click()
        assert not page.locator("body").evaluate("element => element.classList.contains('dark')")
        page.locator("#export-button").click()
        page.locator('[data-export="csv"]').click()
        page.wait_for_function("document.getElementById('toast').textContent.includes('Exported 6')")
        assert target.exists() and "Example" in target.read_text(encoding="utf-8")
        # Start through the form, pause/resume through the bridge, then inspect the saved report.
        page.locator("#url").fill("https://fixture.example/")
        page.locator("#advanced-toggle").click()
        page.locator("#delay").fill("0.2")
        page.locator("#start-button").click()
        page.wait_for_function("document.getElementById('pause-button').hidden === false")
        page.locator("#pause-button").click()
        page.wait_for_function("document.getElementById('run-label').textContent === 'Crawl paused'")
        page.locator("#pause-button").click()
        page.wait_for_function("document.getElementById('run-label').textContent === 'Crawl complete'", timeout=15000)
        assert len(api.get_state()["pages"]) == 5
        assert page.evaluate("window.__xss === undefined")
        assert "<img" in page.locator("#results-body").inner_text()
        assert page.locator("#results-body img").count() == 0
        page.locator('[data-view="history"]').click()
        page.wait_for_selector("[data-history]")
        page.locator("[data-history]").click()
        page.locator("#view-workspace").wait_for(state="visible")
        assert page.locator("#url").input_value() == "https://fixture.example/"
        page.locator("#tab-activity").click()
        assert page.locator(".log-row").count() > 0
        page.locator('[data-view="guide"]').click()
        assert page.locator(".guide-card").count() == 4
        page.locator('[data-view="workspace"]').click()
        page.locator("#tab-pages").click()
        for width, height in [(1024,768), (390,844)]:
            page.set_viewport_size({"width":width,"height":height})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
            page.screenshot(path=str(output/f"responsive-{width}.png"), full_page=True)
        assert not errors, errors
        browser.close()
        print("UI acceptance passed: sample, search, filters, sorting, inspector, themes, export, live worker, pause/resume, history, activity, escaping, responsive layout.")


if __name__ == "__main__":
    main()
