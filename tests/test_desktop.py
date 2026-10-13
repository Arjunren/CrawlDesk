from email.message import Message
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import crawler
from desktop import DesktopAPI, serialize_export, validate_settings


def response(url, body, status=200, mime="text/html"):
    headers = Message()
    headers["Content-Type"] = mime + "; charset=utf-8"
    return crawler.FetchResult(url, status, headers, body.encode())


class MetadataTests(unittest.TestCase):
    def test_metadata_alt_and_multiple_h1(self):
        page = crawler.PageParser()
        page.feed('<meta name="DESCRIPTION" content="Hello &amp; there">'
                  '<link rel="alternate canonical" href="/main"><h1>One</h1><h1>Two</h1>'
                  '<img src="a"><img src="b" alt=""><img src="c" alt="Photo">')
        self.assertEqual(page.description, "Hello & there")
        self.assertEqual(page.canonical, "/main")
        self.assertEqual((page.h1_count, page.images, page.images_missing_alt), (2, 3, 1))


class SettingsTests(unittest.TestCase):
    def test_rejects_non_finite_fractional_and_invalid_values(self):
        for changes in [{"delay":float("nan")}, {"timeout":float("inf")}, {"max_pages":1.5},
                        {"max_depth":-1}, {"max_bytes":0}, {"max_pages":True},
                        {"exclude":"admin/*"}, {"url":"file:///test"}]:
            with self.subTest(changes=changes), self.assertRaises(crawler.CrawlError):
                validate_settings({"url":"https://example.com/", **changes})

    def test_normalizes_settings_and_exclusions(self):
        values = validate_settings({"url":"https://EXAMPLE.com/#test", "exclude":"/admin/*\n\n /account/* "})
        self.assertEqual(values["url"], "https://example.com/")
        self.assertEqual(values["exclude"], ["/admin/*", "/account/*"])


class ExtendedCrawlTests(unittest.TestCase):
    def test_depth_exclusions_errors_and_issue_events(self):
        requested = []
        events = []
        pages = {
            "https://example.com/robots.txt": response("https://example.com/robots.txt", "", mime="text/plain"),
            "https://example.com/": response("https://example.com/", '<title>Home</title><h1>Home</h1>'
                '<a href="/admin/private">Admin</a><a href="/next">Next</a><a href="/broken">Broken</a>'),
            "https://example.com/next": response("https://example.com/next", '<a href="/deep">Deep</a>'),
            "https://example.com/broken": response("https://example.com/broken", "Missing", status=404),
        }
        def fetch(_self, url, *args, **kwargs):
            requested.append(url)
            return pages[url]
        with tempfile.TemporaryDirectory() as directory, patch.object(crawler.Fetcher, "fetch", fetch):
            counts = crawler.crawl("https://example.com/", max_pages=10, delay=.2, timeout=1,
                max_bytes=10000, max_depth=1, exclude=("/admin/*",),
                output=Path(directory)/"output.jsonl", on_event=events.append)
        self.assertEqual(counts, (3, 2))
        self.assertNotIn("https://example.com/deep", requested)
        self.assertNotIn("https://example.com/admin/private", requested)
        results = [event["page"] for event in events if event["kind"] == "page"]
        self.assertEqual(results[-1]["status"], 404)
        self.assertIn("Missing meta description", results[0]["issues"])
        self.assertEqual(results[1]["depth"], 1)

    def test_cancel_is_not_swallowed_as_fetch_failure(self):
        def cancelled(_self, url, *args, **kwargs):
            if url.endswith("robots.txt"):
                return response(url, "", mime="text/plain")
            raise crawler.CrawlCancelled("Stopped")
        with tempfile.TemporaryDirectory() as directory, patch.object(crawler.Fetcher, "fetch", cancelled):
            with self.assertRaises(crawler.CrawlCancelled):
                crawler.crawl("https://example.com/", max_pages=1, delay=.2, timeout=1,
                    max_bytes=1024, output=Path(directory)/"output.jsonl")

    def test_server_backoff_stops_with_reason(self):
        events = []
        def fetch(_self, url, *args, **kwargs):
            return response(url, "", 200 if url.endswith("robots.txt") else 429, "text/plain")
        with tempfile.TemporaryDirectory() as directory, patch.object(crawler.Fetcher, "fetch", fetch):
            counts = crawler.crawl("https://example.com/", max_pages=5, delay=.2, timeout=1,
                max_bytes=1024, output=Path(directory)/"output.jsonl", on_event=events.append)
        self.assertEqual(counts, (1, 0))
        self.assertTrue(any(event["kind"] == "stopped" for event in events))


class ExportTests(unittest.TestCase):
    def test_csv_protects_formula_cells_and_quotes_newlines(self):
        import csv
        import io
        text = serialize_export([{"url":"https://example.com/", "title":"  =1+1",
                                "description":"hello,\nworld", "issues":["Missing H1"]}], "csv")
        row = next(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(row["title"], "'  =1+1")
        self.assertEqual(row["description"], "hello,\nworld")
        self.assertEqual(row["issues"], "Missing H1")

    def test_json_preserves_page_metadata(self):
        pages = [{"title":"安全", "internal_links":["https://example.com/"], "issues":[]}]
        self.assertEqual(json.loads(serialize_export(pages, "json")), pages)
        self.assertEqual(json.loads(serialize_export(pages, "jsonl")), pages[0])

    def test_export_refuses_overwrite(self):
        class FakeWindow:
            def create_file_dialog(self, *args, **kwargs):
                return [str(target)]
        with tempfile.TemporaryDirectory() as directory:
            api = DesktopAPI(Path(directory))
            api.load_demo()
            api._window = FakeWindow()
            target = Path(directory)/"existing.csv"
            target.write_text("original", encoding="utf-8")
            with patch.dict("sys.modules", {"webview": type("Webview", (), {"FileDialog": type("Dialog", (), {"SAVE":30})})}):
                self.assertFalse(api.export_results("csv")["ok"])
            self.assertEqual(target.read_text(), "original")


class WorkerTests(unittest.TestCase):
    def test_theme_persists_across_app_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            api = DesktopAPI(Path(directory))
            self.assertEqual(api.get_preferences()["theme"], "light")
            self.assertTrue(api.save_preferences({"theme":"dark"})["ok"])
            restored = DesktopAPI(Path(directory))
            self.assertEqual(restored.get_preferences()["theme"], "dark")
            self.assertEqual(restored.get_history(), [])
            self.assertFalse(restored.save_preferences({"theme":"other"})["ok"])

    def test_pause_resume_stop_and_preserve_history(self):
        def fake_crawl(*args, **kwargs):
            kwargs["on_event"]({"kind":"page", "page":{"url":"https://example.com/", "status":200}})
            kwargs["on_event"]({"kind":"progress", "attempted":1, "saved":1, "queued":0, "url":"https://example.com/"})
            kwargs["wait"](60)
            return 1, 1
        with tempfile.TemporaryDirectory() as directory, patch("desktop.crawl", fake_crawl):
            api = DesktopAPI(Path(directory))
            self.assertTrue(api.start_crawl({"url":"https://example.com/"})["ok"])
            self.assertFalse(api.start_crawl({"url":"https://example.com/"})["ok"])
            self.assertTrue(api.pause_crawl()["ok"])
            self.assertEqual(api.get_state()["status"], "paused")
            self.assertTrue(api.pause_crawl()["ok"])
            self.assertEqual(api.get_state()["status"], "running")
            self.assertFalse(api.load_history("../test")["ok"])
            self.assertTrue(api.stop_crawl()["ok"])
            api._thread.join(timeout=2)
            self.assertFalse(api._thread.is_alive())
            self.assertEqual(api.get_state()["status"], "stopped")
            self.assertEqual(len(api.get_state()["pages"]), 1)
            report = api.get_history()[0]
            self.assertEqual(report["saved"], 1)
            self.assertTrue(api.load_demo()["ok"])
            self.assertTrue(api.load_history(report["id"])["ok"])
            self.assertEqual(api.get_state()["status"], "stopped")
            self.assertFalse(api.load_history("../test")["ok"])

    def test_pause_blocks_checkpoint_until_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            api = DesktopAPI(Path(directory))
            api._paused = True
            arrived = threading.Event()
            thread = threading.Thread(target=lambda: (api._checkpoint(), arrived.set()), daemon=True)
            thread.start()
            self.assertFalse(arrived.wait(.05))
            with api._condition:
                api._paused = False
                api._condition.notify_all()
            self.assertTrue(arrived.wait(1))
            thread.join(1)

    def test_worker_error_is_saved_and_new_crawl_can_start(self):
        with tempfile.TemporaryDirectory() as directory, patch("desktop.crawl", side_effect=crawler.CrawlError("robots.txt denies access")):
            api = DesktopAPI(Path(directory))
            api.start_crawl({"url":"https://example.com/"})
            api._thread.join(2)
            self.assertEqual(api.get_state()["status"], "failed")
            self.assertIn("robots.txt", api.get_state()["error"])
            self.assertEqual(len(api.get_history()), 1)
            api.start_crawl({"url":"https://example.com/"})
            api._thread.join(2)
            self.assertEqual(len(api.get_history()), 2)


if __name__ == "__main__":
    unittest.main()
