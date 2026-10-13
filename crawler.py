#!/usr/bin/env python3
"""A bounded, robots-aware, same-origin website crawler (Python standard library)."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import fnmatch
import math
from pathlib import Path
import socket
import ssl
import sys
import time
from typing import Callable
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "SiteCrawler/1.0"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_LINKS_PER_PAGE = 500
MAX_QUEUE_MULTIPLIER = 20
ROBOTS_MAX_BYTES = 512_000


class CrawlError(Exception):
    """A safe, expected crawling error."""


class CrawlCancelled(CrawlError):
    """Cooperative cancellation, including while waiting between requests."""


def normalize_url(raw: str, base: str | None = None) -> str:
    """Return an absolute, fragment-free HTTP(S) URL on its default port."""
    if not raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise CrawlError("URL is empty or contains control characters")
    value, _ = urldefrag(urljoin(base, raw) if base is not None else raw)
    if len(value) > 2048:
        raise CrawlError("URL exceeds the 2048-character limit")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CrawlError("Malformed URL") from exc
    if scheme not in ("http", "https") or not host:
        raise CrawlError("Only absolute HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise CrawlError("URLs containing credentials are not supported")
    expected_port = 443 if scheme == "https" else 80
    if port not in (None, expected_port):
        raise CrawlError("Only the default HTTP(S) ports are allowed")

    try:
        parsed_ip = ipaddress.ip_address(host)
        host = parsed_ip.compressed
    except ValueError:
        try:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise CrawlError("Invalid DNS hostname") from exc
        if not host or len(host) > 253:
            raise CrawlError("Invalid DNS hostname")

    netloc = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def public_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Connect to a verified public IP, not to a second DNS resolution."""
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CrawlError("DNS lookup failed") from exc

    last_error: OSError | None = None
    has_public_address = False
    for family, socktype, proto, _, sockaddr in candidates:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            continue
        has_public_address = True
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)  # Address checked above is the address used.
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if not has_public_address:
        raise CrawlError("Blocked: host resolves only to non-public IP addresses")
    raise CrawlError("Could not connect to the public host") from last_error


class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = public_socket(self.host, self.port, self.timeout)


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        raw_sock = public_socket(self.host, self.port, self.timeout)
        try:
            # Validate certificate and hostname using the original DNS name.
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
        except Exception:
            raw_sock.close()
            raise


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: Message
    body: bytes


class RateLimiter:
    def __init__(self, delay: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.delay = delay
        self._clock = clock
        self._sleep = sleep
        self._previous: float | None = None

    def wait(self) -> None:
        if self._previous is not None:
            remaining = self.delay - (self._clock() - self._previous)
            if remaining > 0:
                self._sleep(remaining)
        self._previous = self._clock()


class Fetcher:
    def __init__(self, site_origin: tuple[str, str, int], timeout: float,
                 limiter: RateLimiter) -> None:
        self.site_origin = site_origin
        self.timeout = timeout
        self.limiter = limiter

    def fetch(self, url: str, max_bytes: int,
              is_allowed: Callable[[str], bool] | None = None) -> FetchResult:
        current = normalize_url(url)
        redirects_seen: set[str] = set()
        for _ in range(MAX_REDIRECTS + 1):
            if origin(current) != self.site_origin:
                raise CrawlError("Redirect to another origin was blocked")
            if is_allowed is not None and not is_allowed(current):
                raise CrawlError("Redirect disallowed by robots.txt or crawl exclusions")
            if current in redirects_seen:
                raise CrawlError("Redirect loop detected")
            redirects_seen.add(current)
            parts = urlsplit(current)
            request_target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            connection_cls = PublicHTTPSConnection if parts.scheme == "https" else PublicHTTPConnection
            connection = (connection_cls(parts.hostname or "", parts.port, timeout=self.timeout,
                                         context=ssl.create_default_context())
                          if parts.scheme == "https"
                          else connection_cls(parts.hostname or "", parts.port, timeout=self.timeout))
            self.limiter.wait()
            try:
                connection.request("GET", request_target, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.5",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                status, headers = response.status, response.headers
                if status in REDIRECT_STATUSES:
                    location = headers.get("Location")
                    if not location:
                        raise CrawlError("Redirect missing Location header")
                    target = normalize_url(location, current)
                    if origin(target) != self.site_origin:
                        raise CrawlError("Redirect to another origin was blocked")
                    current = target
                    continue
                if response.length is not None and response.length > max_bytes:
                    raise CrawlError("Response exceeds configured byte limit")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise CrawlError("Response exceeds configured byte limit")
                return FetchResult(current, status, headers, body)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise CrawlError("HTTP request failed") from exc
            finally:
                connection.close()
        raise CrawlError("Too many redirects")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.links: list[str] = []
        self._title_depth = 0
        self._h1_depth = 0
        self.description = ""
        self.canonical = ""
        self.h1_count = 0
        self.images = 0
        self.images_missing_alt = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._title_depth += 1
        elif tag == "h1":
            self._h1_depth += 1
            self.h1_count += 1
        elif tag == "meta":
            values = dict(attrs)
            if (values.get("name") or "").lower() == "description":
                self.description = (values.get("content") or "")[:1000]
        elif tag == "link":
            values = dict(attrs)
            if "canonical" in (values.get("rel") or "").lower().split():
                self.canonical = (values.get("href") or "")[:2048]
        elif tag == "img":
            self.images += 1
            if "alt" not in dict(attrs):
                self.images_missing_alt += 1
        elif tag == "a" and len(self.links) < MAX_LINKS_PER_PAGE:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        elif tag == "h1":
            self._h1_depth = max(0, self._h1_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._title_depth and sum(map(len, self.title_parts)) < 500:
            self.title_parts.append(data[:500])
        if self._h1_depth and sum(map(len, self.h1_parts)) < 500:
            self.h1_parts.append(data[:500])


def compact(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())[:300]


def load_robots(fetcher: Fetcher, start_url: str) -> RobotFileParser:
    parts = urlsplit(start_url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    result = fetcher.fetch(robots_url, ROBOTS_MAX_BYTES)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    if result.status in (404, 410):
        parser.parse([])  # A missing robots.txt means no robots-specific restrictions.
        return parser
    if result.status in (401, 403):
        raise CrawlError("robots.txt denies crawler access")
    if result.status != 200:
        raise CrawlError(f"robots.txt unavailable (HTTP {result.status}); stopping")
    parser.parse(result.body.decode("utf-8-sig", errors="replace").splitlines())
    return parser


def crawl(start_url: str, *, max_pages: int, delay: float, timeout: float,
          max_bytes: int, output: Path, max_depth: int = 10,
          exclude: tuple[str, ...] = (),
          on_event: Callable[[dict], None] | None = None,
          checkpoint: Callable[[], None] | None = None,
          wait: Callable[[float], None] | None = None) -> tuple[int, int]:
    # Validate direct API calls as well as command-line arguments.
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 500:
        raise CrawlError("Page limit must be between 1 and 500")
    if not math.isfinite(delay) or not 0.2 <= delay <= 60:
        raise CrawlError("Delay must be between 0.2 and 60 seconds")
    if not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise CrawlError("Timeout must be between 1 and 60 seconds")
    if not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 5_000_000:
        raise CrawlError("Byte limit must be between 1024 and 5000000")
    if not isinstance(max_depth, int) or not 0 <= max_depth <= 20:
        raise CrawlError("Depth must be between 0 and 20")
    def check() -> None:
        if checkpoint:
            checkpoint()

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event({"kind": kind, **data})

    def excluded(url: str) -> bool:
        return any(fnmatch.fnmatchcase(urlsplit(url).path, pattern) for pattern in exclude)

    start = normalize_url(start_url)
    if excluded(start):
        raise CrawlError("Start URL matches an excluded path")
    site_origin = origin(start)
    limiter = RateLimiter(delay, sleep=wait or time.sleep)
    fetcher = Fetcher(site_origin, timeout, limiter)
    check()
    emit("log", message="Checking robots.txt", url=start)
    robots = load_robots(fetcher, start)
    if not robots.can_fetch(USER_AGENT, start):
        raise CrawlError("Start URL disallowed by robots.txt")
    robots_delay = robots.crawl_delay(USER_AGENT)
    if robots_delay is not None:
        limiter.delay = max(delay, float(robots_delay))

    queue = deque([(start, 0)])
    scheduled = {start}
    visited: set[str] = set()
    saved = 0
    attempted = 0
    with output.open("x", encoding="utf-8") as file:
        while queue and attempted < max_pages:
            check()
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            attempted += 1
            emit("progress", attempted=attempted, saved=saved, queued=len(queue), url=url)
            if not robots.can_fetch(USER_AGENT, url):
                print(f"[SKIP robots] {url}", file=sys.stderr)
                emit("log", message="Skipped by robots.txt", url=url)
                continue
            try:
                started = time.monotonic()
                response = fetcher.fetch(url, max_bytes, is_allowed=lambda target: robots.can_fetch(USER_AGENT, target) and not excluded(target))
                elapsed_ms = round((time.monotonic() - started) * 1000)
                visited.add(response.url)  # Avoid fetching redirected targets twice.
                if response.status in (429, 503):
                    print(f"[STOP HTTP {response.status}] Server is busy or rate limiting; ending crawl",
                          file=sys.stderr)
                    emit("stopped", message=f"Server returned HTTP {response.status}; crawl stopped")
                    break
                if response.status != 200:
                    print(f"[SKIP HTTP {response.status}] {url}", file=sys.stderr)
                    emit("page", page={"url": response.url, "status": response.status,
                         "title": "", "h1": "", "description": "", "internal_links": [],
                         "links_found": 0, "depth": depth, "elapsed_ms": elapsed_ms,
                         "bytes": len(response.body), "issues": [f"HTTP {response.status}"]})
                    continue
                if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                    print(f"[SKIP non-HTML] {url}", file=sys.stderr)
                    emit("log", message="Skipped non-HTML response", url=url)
                    continue
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    html = response.body.decode(charset, errors="replace")
                except LookupError:
                    html = response.body.decode("utf-8", errors="replace")
                page = PageParser()
                page.feed(html)
                internal_links: list[str] = []
                seen_on_page: set[str] = set()
                for link in page.links:
                    try:
                        target = normalize_url(link, response.url)
                    except CrawlError:
                        continue
                    if origin(target) == site_origin and target not in seen_on_page:
                        internal_links.append(target)
                        seen_on_page.add(target)
                result = {
                    "url": response.url,
                    "status": response.status,
                    "title": compact(page.title_parts),
                    "h1": compact(page.h1_parts),
                    "links_found": len(page.links),
                    "internal_links": internal_links,
                    "description": page.description,
                    "canonical": page.canonical,
                    "h1_count": page.h1_count,
                    "images": page.images,
                    "images_missing_alt": page.images_missing_alt,
                    "depth": depth,
                    "elapsed_ms": elapsed_ms,
                    "bytes": len(response.body),
                    "issues": [],
                }
                if not result["title"]:
                    result["issues"].append("Missing title")
                elif len(result["title"]) > 60:
                    result["issues"].append("Long title")
                if not page.description:
                    result["issues"].append("Missing meta description")
                if page.h1_count == 0:
                    result["issues"].append("Missing H1")
                elif page.h1_count > 1:
                    result["issues"].append("Multiple H1 headings")
                if page.images_missing_alt:
                    result["issues"].append("Images missing alt attributes")
                file.write(json.dumps(result, ensure_ascii=False) + "\n")
                file.flush()
                saved += 1
                emit("page", page=result)
                emit("progress", attempted=attempted, saved=saved, queued=len(queue), url=url)
                print(f"[OK] {response.url}", file=sys.stderr)
                for target in internal_links:
                    if depth >= max_depth:
                        break
                    if len(scheduled) >= max_pages * MAX_QUEUE_MULTIPLIER:
                        break
                    if target in scheduled:
                        continue
                    if excluded(target):
                        continue
                    if not robots.can_fetch(USER_AGENT, target):
                        continue
                    scheduled.add(target)
                    queue.append((target, depth + 1))
            except CrawlCancelled:
                raise
            except CrawlError as exc:
                print(f"[SKIP] {url}: {exc}", file=sys.stderr)
                emit("page", page={"url": url, "status": 0, "title": "", "h1": "",
                     "description": "", "internal_links": [], "links_found": 0,
                     "depth": depth, "elapsed_ms": 0, "bytes": 0, "issues": [str(exc)]})
    return attempted, saved


def bounded_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= value <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return value


def bounded_delay(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0.2 <= value <= 60:
        raise argparse.ArgumentTypeError("must be between 0.2 and 60 seconds")
    return value


def bounded_timeout(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 1 <= value <= 60:
        raise argparse.ArgumentTypeError("must be between 1 and 60 seconds")
    return value


def bounded_bytes(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1024 <= value <= 5_000_000:
        raise argparse.ArgumentTypeError("must be between 1024 and 5000000")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely crawl public pages on one website.")
    parser.add_argument("url", help="Starting URL, e.g. https://example.com/")
    parser.add_argument("--max-pages", type=bounded_int, default=25)
    parser.add_argument("--delay", type=bounded_delay, default=1.0, help="Minimum seconds between requests")
    parser.add_argument("--timeout", type=bounded_timeout, default=10.0)
    parser.add_argument("--max-bytes", type=bounded_bytes, default=2_000_000, help="Per-page response limit")
    parser.add_argument("--max-depth", type=int, default=10, help="Link depth from start (0-20)")
    parser.add_argument("--exclude", action="append", default=[], help="Excluded path glob, e.g. /admin/* (repeatable)")
    parser.add_argument("--output", type=Path, default=Path("pages.jsonl"), help="Output JSON Lines file (must not exist)")
    args = parser.parse_args(argv)
    try:
        attempted, saved = crawl(args.url, max_pages=args.max_pages, delay=args.delay,
                               timeout=args.timeout, max_bytes=args.max_bytes, output=args.output,
                               max_depth=args.max_depth, exclude=tuple(args.exclude))
    except (CrawlError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Finished: {attempted} URLs attempted, {saved} HTML pages saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
