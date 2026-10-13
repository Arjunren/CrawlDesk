"""CrawlDesk: a local pywebview window around the public-site crawler."""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import tempfile
import time
import uuid

from crawler import CrawlCancelled, CrawlError, crawl, normalize_url

APP_VERSION = "2.0.0"
ACTIVE = {"running", "paused", "stopping"}


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise CrawlError("Enter valid crawl settings")
    url = normalize_url(str(settings.get("url", "")).strip())
    values = {"url": url}
    for name, default, minimum, maximum, integer in (
        ("max_pages", 50, 1, 500, True), ("max_depth", 5, 0, 20, True),
        ("delay", 1.0, 0.2, 60, False), ("timeout", 10, 1, 60, False),
        ("max_bytes", 2_000_000, 1024, 5_000_000, True),
    ):
        raw = settings.get(name, default)
        try:
            number = float(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CrawlError(f"{name.replace('_', ' ').capitalize()} must be a number") from exc
        if isinstance(raw, bool) or not math.isfinite(number) or not minimum <= number <= maximum:
            raise CrawlError(f"{name.replace('_', ' ').capitalize()} must be between {minimum} and {maximum}")
        if integer and not number.is_integer():
            raise CrawlError(f"{name.replace('_', ' ').capitalize()} must be a whole number")
        values[name] = int(number) if integer else number
    patterns = settings.get("exclude", "")
    if not isinstance(patterns, str) or len(patterns) > 2000:
        raise CrawlError("Excluded paths must contain at most 2000 characters")
    values["exclude"] = [p.strip() for p in patterns.splitlines() if p.strip()]
    if any(not p.startswith("/") for p in values["exclude"]):
        raise CrawlError("Excluded paths must start with /; enter one pattern per line")
    return values


def csv_cell(value) -> str:
    """Protect spreadsheet readers from formulas in untrusted page metadata."""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def serialize_export(pages: list[dict], format: str) -> str:
    if format == "json":
        return json.dumps(pages, ensure_ascii=False, indent=2)
    if format == "jsonl":
        return "".join(json.dumps(page, ensure_ascii=False) + "\n" for page in pages)
    if format != "csv":
        raise CrawlError("Choose CSV, JSON or JSONL")
    stream = io.StringIO(newline="")
    fields = ["url", "status", "title", "description", "h1", "depth", "elapsed_ms",
              "bytes", "links_found", "canonical", "images_missing_alt", "issues"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for page in pages:
        row = {field: csv_cell(page.get(field, "")) for field in fields}
        row["issues"] = csv_cell("; ".join(page.get("issues", [])))
        writer.writerow(row)
    return "\ufeff" + stream.getvalue()


class DesktopAPI:
    def __init__(self, storage: Path | None = None) -> None:
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
        self._storage = storage or root / "CrawlDesk"
        self._storage.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._cancel = False
        self._paused = False
        self._thread = None
        self._window = None
        self._started = None
        self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> dict:
        return {"status": "idle", "pages": [], "logs": [], "attempted": 0, "saved": 0,
                "queued": 0, "elapsed": 0, "current_url": "", "settings": {},
                "error": "", "stop_reason": "", "id": "", "started_at": ""}

    def get_state(self) -> dict:
        with self._lock:
            state = copy.deepcopy(self._state)
            if self._started is not None and state["status"] in ACTIVE:
                state["elapsed"] = round(time.monotonic() - self._started, 1)
            state["version"] = APP_VERSION
            return state

    def get_preferences(self) -> dict:
        with self._lock:
            try:
                value = json.loads((self._storage / "preferences.json").read_text(encoding="utf-8"))
                return {"theme": "dark" if value.get("theme") == "dark" else "light"}
            except (OSError, ValueError, AttributeError):
                return {"theme": "light"}

    def save_preferences(self, preferences: dict) -> dict:
        if not isinstance(preferences, dict) or preferences.get("theme") not in {"light", "dark"}:
            return {"ok": False, "error": "Invalid appearance preference"}
        with self._lock:
            try:
                temporary = self._storage / "preferences.tmp"
                temporary.write_text(json.dumps({"theme": preferences["theme"]}), encoding="utf-8")
                temporary.replace(self._storage / "preferences.json")
                return {"ok": True}
            except OSError as exc:
                return {"ok": False, "error": f"Could not save appearance: {exc}"}

    def start_crawl(self, settings: dict) -> dict:
        try:
            values = validate_settings(settings)
        except CrawlError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._state["status"] in ACTIVE:
                return {"ok": False, "error": "Stop the current crawl before starting another"}
            self._state = self._empty_state()
            self._state.update(status="running", settings=values, id=uuid.uuid4().hex,
                               started_at=datetime.now(timezone.utc).isoformat())
            self._cancel = self._paused = False
            self._started = time.monotonic()
            self._thread = threading.Thread(target=self._run, daemon=True, name="crawl-worker")
            self._thread.start()
        return {"ok": True}

    def pause_crawl(self) -> dict:
        with self._condition:
            if self._state["status"] not in {"running", "paused"}:
                return {"ok": False, "error": "No active crawl to pause"}
            self._paused = not self._paused
            self._state["status"] = "paused" if self._paused else "running"
            self._condition.notify_all()
            return {"ok": True}

    def stop_crawl(self) -> dict:
        with self._condition:
            if self._state["status"] not in ACTIVE:
                return {"ok": False, "error": "No active crawl to stop"}
            self._cancel = True
            self._paused = False
            self._state["status"] = "stopping"
            self._condition.notify_all()
        return {"ok": True}

    def _checkpoint(self) -> None:
        with self._condition:
            while self._paused and not self._cancel:
                self._condition.wait()
            if self._cancel:
                raise CrawlCancelled("Stopped by you")

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            self._checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with self._condition:
                self._condition.wait(min(remaining, 0.1))

    def _event(self, event: dict) -> None:
        with self._lock:
            kind = event["kind"]
            if kind == "page":
                self._state["pages"].append(event["page"])
                message = f"HTTP {event['page']['status'] or 'error'} · {event['page']['url']}"
            elif kind == "progress":
                self._state.update({key: event[key] for key in ("attempted", "saved", "queued")})
                self._state["current_url"] = event["url"]
                return
            else:
                message = event.get("message", "")
                if kind == "stopped":
                    self._state["stop_reason"] = message
            self._state["logs"].append({"time": datetime.now().strftime("%H:%M:%S"),
                                        "message": message, "kind": kind})
            self._state["logs"] = self._state["logs"][-300:]

    def _run(self) -> None:
        with self._lock:
            settings = copy.deepcopy(self._state["settings"])
            session_id = self._state["id"]
        terminal_status = "completed"
        try:
            attempted, saved = crawl(
                settings["url"], max_pages=settings["max_pages"], delay=settings["delay"],
                timeout=settings["timeout"], max_bytes=settings["max_bytes"],
                max_depth=settings["max_depth"], exclude=tuple(settings["exclude"]),
                output=self._storage / f"{session_id}.jsonl", on_event=self._event,
                checkpoint=self._checkpoint, wait=self._wait,
            )
            with self._lock:
                self._state.update(attempted=attempted, saved=saved)
        except CrawlCancelled:
            terminal_status = "stopped"
            with self._lock:
                self._state["stop_reason"] = "Stopped by you; partial results retained"
        except Exception as exc:
            terminal_status = "failed"
            with self._lock:
                self._state["error"] = str(exc)
        finally:
            # Hold the lock through persistence so a new crawl cannot replace this report.
            with self._lock:
                if terminal_status == "completed" and (self._cancel or self._state["stop_reason"]):
                    terminal_status = "stopped"
                self._state["status"] = terminal_status
                self._state["elapsed"] = round(time.monotonic() - self._started, 1)
                self._started = None
                self._state["queued"] = 0
                try:
                    path = self._storage / f"{session_id}.json"
                    path.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
                except OSError as exc:
                    self._state["error"] = f"Could not save crawl history: {exc}"

    def get_history(self) -> list[dict]:
        reports = []
        for path in self._storage.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                reports.append({key: data[key] for key in
                                ("id", "started_at", "status", "settings", "saved", "elapsed")})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(reports, key=lambda item: item["started_at"], reverse=True)

    def load_history(self, session_id: str) -> dict:
        with self._lock:
            if self._state["status"] in ACTIVE:
                return {"ok": False, "error": "Stop the active crawl before opening history"}
            if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
                return {"ok": False, "error": "Invalid crawl identifier"}
            try:
                report = json.loads((self._storage / f"{session_id}.json").read_text(encoding="utf-8"))
                if not isinstance(report.get("pages"), list) or report.get("id") != session_id:
                    raise ValueError("Invalid report")
                self._state = report
                self._started = None
                return {"ok": True}
            except (OSError, ValueError, AttributeError) as exc:
                return {"ok": False, "error": f"Could not open this crawl: {exc}"}

    def load_demo(self) -> dict:
        with self._lock:
            if self._state["status"] in ACTIVE:
                return {"ok": False, "error": "Stop the active crawl first"}
            self._state = self._empty_state()
            self._state.update(status="demo", settings={"url": "https://example.com/", "max_pages": 50},
                               attempted=6, saved=5, elapsed=8.4)
            rows = [
                ("/", "Example — build something great", "A place for your next idea.", "Welcome", 200, 0, 182, []),
                ("/about", "About our team", "Meet the people behind the product.", "Our story", 200, 1, 234, []),
                ("/docs", "Documentation", "", "Documentation", 200, 1, 148, ["Missing meta description"]),
                ("/blog", "Stories & updates", "News from the team.", "", 200, 1, 302, ["Missing H1"]),
                ("/docs/quickstart", "Quickstart guide", "Get started in five minutes.", "Quickstart", 200, 2, 198, []),
                ("/old-page", "", "", "", 404, 1, 127, ["HTTP 404"]),
            ]
            for path, title, description, h1, status, depth, ms, issues in rows:
                self._state["pages"].append({"url": "https://example.com" + path, "title": title,
                    "description": description, "h1": h1, "status": status, "depth": depth,
                    "elapsed_ms": ms, "bytes": 2450 if status == 200 else 120,
                    "issues": issues, "links_found": 4 if status == 200 else 0,
                    "internal_links": ["https://example.com/about", "https://example.com/docs"] if status == 200 else [],
                    "canonical": "", "images": 0, "images_missing_alt": 0})
            self._state["logs"] = [{"time": "—", "message": "Sample crawl loaded. These are fictional pages, not a live scan.", "kind": "log"}]
            return {"ok": True}

    def export_results(self, format: str) -> dict:
        try:
            with self._lock:
                pages = copy.deepcopy(self._state["pages"])
            if not pages:
                raise CrawlError("Crawl a website or load the sample before exporting")
            content = serialize_export(pages, format)
            if self._window is None:
                raise CrawlError("Export is available in the desktop app")
            import webview
            choice = self._window.create_file_dialog(webview.FileDialog.SAVE,
                save_filename=f"crawl-{datetime.now():%Y%m%d-%H%M%S}.{format}",
                file_types=(f"{format.upper()} files (*.{format})",))
            if not choice:
                return {"ok": True, "cancelled": True}
            path = Path(choice if isinstance(choice, str) else choice[0])
            if not path.suffix:
                path = path.with_suffix("." + format)
            with path.open("x", encoding="utf-8", newline="") as file:
                file.write(content)
            return {"ok": True, "path": str(path), "count": len(pages)}
        except FileExistsError:
            return {"ok": False, "error": "That file already exists. Choose a new filename."}
        except (CrawlError, OSError) as exc:
            return {"ok": False, "error": str(exc)}


def asset_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def smoke_test(report_path: Path) -> int:
    """Check the actual renderer and bridge in source and frozen builds."""
    import webview
    result = {"ok": False, "version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False))}
    ready = threading.Event()
    with tempfile.TemporaryDirectory() as directory:
        api = DesktopAPI(Path(directory))
        window = webview.create_window("CrawlDesk release verification", str(asset_root() / "ui" / "index.html"),
                                       js_api=api, width=1280, height=850, hidden=True)
        api._window = window
        window.events.loaded += ready.set
        def checks():
            try:
                if not ready.wait(20):
                    raise RuntimeError("Desktop renderer did not load")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    connected = window.evaluate_js("document.getElementById('connection').textContent")
                    if connected == "Desktop connected":
                        break
                    time.sleep(.2)
                if connected != "Desktop connected":
                    raise RuntimeError("Python-JavaScript bridge did not connect")
                window.evaluate_js("document.getElementById('demo-button').click(); true")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    rows = window.evaluate_js("document.querySelectorAll('#results-body tr').length")
                    if rows == 6:
                        break
                    time.sleep(.2)
                if rows != 6 or api.get_state()["status"] != "demo":
                    raise RuntimeError("Bundled interface did not render sample results")
                # Verify exports and local storage within the bundled Python runtime.
                pages = api.get_state()["pages"]
                if len(json.loads(serialize_export(pages, "json"))) != 6:
                    raise RuntimeError("Bundled export failed")
                if not api.save_preferences({"theme": "dark"})["ok"]:
                    raise RuntimeError("Local preference storage failed")
                result.update(ok=True, renderer=webview.guilib.renderer, rows=rows)
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                window.destroy()
        webview.start(checks)
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit("Install desktop dependencies first: python -m pip install -r requirements.txt")
    if "--smoke-test" in sys.argv:
        try:
            report_path = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
        except IndexError:
            raise SystemExit("Usage: CrawlDesk.exe --smoke-test REPORT.json")
        raise SystemExit(smoke_test(report_path))
    api = DesktopAPI()
    api._window = webview.create_window("CrawlDesk", str(asset_root() / "ui" / "index.html"),
        js_api=api, width=1380, height=900, min_size=(980, 680),
        background_color="#f6f7f9", text_select=True, confirm_close=True)
    def closing():
        if api.get_state()["status"] in ACTIVE:
            api.stop_crawl()
            # Network requests finish at their timeout; interruptible delays stop immediately.
            if api._thread:
                api._thread.join(timeout=2)
            if api._thread and api._thread.is_alive():
                with api._lock:
                    report = api.get_state()
                    report.update(status="stopped", stop_reason="Window closed; partial results retained", queued=0)
                    try:
                        (api._storage / f"{report['id']}.json").write_text(
                            json.dumps(report, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
    api._window.events.closed += closing
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
