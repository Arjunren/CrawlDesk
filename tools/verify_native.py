"""Smoke-test the real pywebview renderer and JavaScript bridge on Windows."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import DesktopAPI
import webview


def main():
    result = {"ok":False}
    ready = threading.Event()
    with tempfile.TemporaryDirectory() as directory:
        api = DesktopAPI(Path(directory))
        window = webview.create_window("CrawlDesk renderer test", str(ROOT/"ui"/"index.html"),
            js_api=api, width=1280, height=850, hidden=True)
        api._window = window
        window.events.loaded += ready.set
        def checks():
            try:
                assert ready.wait(20), "Webview did not load"
                deadline = time.monotonic()+20
                while time.monotonic() < deadline:
                    connected = window.evaluate_js("document.getElementById('connection').textContent")
                    if connected == "Desktop connected":
                        break
                    time.sleep(.2)
                assert connected == "Desktop connected", connected
                window.evaluate_js("document.getElementById('demo-button').click(); true")
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    count = window.evaluate_js("document.querySelectorAll('#results-body tr').length")
                    if count == 6:
                        break
                    time.sleep(.2)
                assert count == 6, f"Expected 6 sample rows, got {count}"
                assert api.get_state()["status"] == "demo"
                result.update(ok=True, renderer=webview.guilib.renderer, rows=count)
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                window.destroy()
        webview.start(checks)
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
