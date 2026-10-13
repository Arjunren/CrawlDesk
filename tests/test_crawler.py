from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import crawler


def response(url: str, body: str, status: int = 200, mime: str = "text/html") -> crawler.FetchResult:
    headers = Message()
    headers["Content-Type"] = mime + "; charset=utf-8"
    return crawler.FetchResult(url, status, headers, body.encode())


class UrlTests(unittest.TestCase):
    def test_normalizes_links_and_removes_fragments(self):
        self.assertEqual(crawler.normalize_url("/about#contact", "https://EXAMPLE.com/"),
                         "https://example.com/about")
        self.assertEqual(crawler.normalize_url("?q=hello world", "https://example.com/a"),
                         "https://example.com/a?q=hello%20world")

    def test_blocks_bad_schemes_credentials_and_ports(self):
        for url in ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com/",
                    "http://example.com:8080/", "https://example.com:8443/", "http://a/\r\nX:1"]:
            with self.subTest(url=url), self.assertRaises(crawler.CrawlError):
                crawler.normalize_url(url)

    def test_origin_policy(self):
        self.assertNotEqual(crawler.origin("https://example.com/"), crawler.origin("http://example.com/"))
        self.assertNotEqual(crawler.origin("https://example.com/"), crawler.origin("https://other.com/"))


class ParsingTests(unittest.TestCase):
    def test_extracts_title_h1_and_links(self):
        parser = crawler.PageParser()
        parser.feed("<html><title>Hello &amp; World</title><h1>Welcome <em>home</em></h1>"
                    "<a href='/one'>One</a><a href='mailto:test@example.com'>Email</a></html>")
        self.assertEqual(crawler.compact(parser.title_parts), "Hello & World")
        self.assertEqual(crawler.compact(parser.h1_parts), "Welcome home")
        self.assertEqual(parser.links, ["/one", "mailto:test@example.com"])

    def test_rate_limit(self):
        now = [0.0]
        delays = []
        limiter = crawler.RateLimiter(1.0, clock=lambda: now[0],
                                     sleep=lambda wait: (delays.append(wait), now.__setitem__(0, now[0] + wait)))
        limiter.wait()
        limiter.wait()
        self.assertEqual(delays, [1.0])


class CrawlTests(unittest.TestCase):
    def test_crawls_allowed_same_origin_pages_and_writes_jsonl(self):
        pages = {
            "https://example.com/robots.txt": response("https://example.com/robots.txt",
                "User-agent: *\nDisallow: /private\nCrawl-delay: 0\n", mime="text/plain"),
            "https://example.com/": response("https://example.com/",
                "<title>Home</title><h1>Start</h1><a href='/next'>Next</a>"
                "<a href='https://evil.test/'>External</a><a href='/private'>Private</a>"),
            "https://example.com/next": response("https://example.com/next", "<title>Next</title>"),
        }

        def fake_fetch(_self, url, max_bytes, is_allowed=None):
            if url not in pages:
                raise AssertionError(f"Unexpected URL fetch: {url}")
            return pages[url]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jsonl"
            with patch.object(crawler.Fetcher, "fetch", fake_fetch):
                visited, saved = crawler.crawl("https://example.com/", max_pages=10,
                    delay=0.2, timeout=1.0, max_bytes=10_000, output=output)
            lines = output.read_text().splitlines()
        self.assertEqual((visited, saved), (2, 2))
        self.assertEqual(len(lines), 2)
        self.assertIn('"title": "Home"', lines[0])
        self.assertIn('"title": "Next"', lines[1])
        self.assertIn('"internal_links": ["https://example.com/next", "https://example.com/private"]', lines[0])

    def test_robots_disallow_start(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jsonl"
            with patch.object(crawler.Fetcher, "fetch", return_value=response(
                    "https://example.com/robots.txt", "User-agent: *\nDisallow: /\n", mime="text/plain")):
                with self.assertRaises(crawler.CrawlError):
                    crawler.crawl("https://example.com/", max_pages=1,
                        delay=0.2, timeout=1.0, max_bytes=1024, output=output)
            self.assertFalse(output.exists())

    def test_redirect_into_robots_disallowed_path_is_blocked(self):
        class FakeHTTPResponse:
            status = 302
            length = 0
            headers = Message()
            headers["Location"] = "/private"

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return FakeHTTPResponse()
            def close(self):
                pass

        fetcher = crawler.Fetcher(crawler.origin("https://example.com/"), 1.0,
                                  crawler.RateLimiter(0.0))
        with patch.object(crawler, "PublicHTTPSConnection", FakeConnection):
            with self.assertRaisesRegex(crawler.CrawlError, "robots.txt"):
                fetcher.fetch("https://example.com/", 1024,
                              is_allowed=lambda url: not url.endswith("/private"))

    def test_non_public_ip_rejected(self):
        from unittest.mock import Mock
        with patch.object(crawler.socket, "getaddrinfo", return_value=[
            (crawler.socket.AF_INET, crawler.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]):
            with self.assertRaisesRegex(crawler.CrawlError, "non-public"):
                crawler.public_socket("example.com", 443, 2)


if __name__ == "__main__":
    unittest.main()
