"""Web connectors: one link, a whole website, and RSS/Atom/sitemap feeds."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterator
from urllib.parse import urljoin, urlsplit

from ..metadata import to_iso
from ..watch import normalise_url
from .base import COMMON_FIELDS, DOC_EXTS, Connector, ConnectorError, F, Item, check_max_bytes, filename_for

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#")
NON_DOC_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
)


class UrlConnector(Connector):
    kind = "url"
    label = "Web page or file link"
    description = "One URL: a web page, PDF, Word, Excel or any document link. Re-sync detects when it changes."
    icon = "🔗"
    fields = [
        F("url", "URL", "url", required=True, placeholder="https://example.gov.in/circulars/2026-04.pdf"),
        F("respect_robots", "Respect robots.txt", "bool", default=True),
        F("max_file_mb", "Max file size (MB)", "number", default=100),
        *COMMON_FIELDS,
    ]

    def discover(self) -> Iterator[Item]:
        url = self.cfg["url"].strip()
        r = self.polite_get(url, min_interval=0, respect_robots=bool(self.cfg.get("respect_robots")))
        r.raise_for_status()
        data = check_max_bytes(r.content, self.cfg, url)
        yield Item(
            key=normalise_url(url),
            filename=filename_for(str(r.url), r.headers.get("content-type")),
            fetch=lambda: data,
            url=url,
            size=len(data),
        )


class WebsiteConnector(Connector):
    kind = "website"
    label = "Website (multi-page crawl)"
    description = "Crawls a site breadth-first from a start page: follows links within scope, downloads linked documents, honours robots.txt and rate limits."
    icon = "🌐"
    fields = [
        F("start_url", "Start URL", "url", required=True, placeholder="https://intranet.example/wiki/"),
        F(
            "scope",
            "Stay within",
            "select",
            default="path",
            options=["path", "domain", "subdomains"],
            help="path: only URLs under the start page's folder",
            group="Crawl",
        ),
        F("max_pages", "Max pages", "number", default=50, group="Crawl"),
        F("max_depth", "Max link depth", "number", default=3, group="Crawl"),
        F("include", "Include URLs matching (regex)", group="Crawl", placeholder="/circulars/|/manuals/"),
        F("exclude", "Exclude URLs matching (regex)", group="Crawl", placeholder="/login|\\?print="),
        F("follow_documents", "Download linked documents (PDF, Office, ZIP)", "bool", default=True, group="Crawl"),
        F("use_sitemap", "Seed from sitemap.xml", "bool", default=False, group="Crawl"),
        F("min_interval_s", "Seconds between requests per host", "number", default=1.0, group="Crawl"),
        F("respect_robots", "Respect robots.txt", "bool", default=True, group="Crawl"),
        F("max_file_mb", "Max file size (MB)", "number", default=100, group="Crawl"),
        *COMMON_FIELDS,
    ]

    def _in_scope(self, url: str, start: str) -> bool:
        u, s = urlsplit(url), urlsplit(start)
        scope = self.cfg.get("scope") or "path"
        if scope == "subdomains":
            ok = u.hostname == s.hostname or (u.hostname or "").endswith("." + (s.hostname or ""))
        else:
            ok = u.netloc == s.netloc
        if ok and scope == "path":
            base = s.path if s.path.endswith("/") else s.path.rsplit("/", 1)[0] + "/"
            ok = u.path.startswith(base) or u.path == s.path
        if ok and self.cfg.get("include") and not re.search(self.cfg["include"], url):
            ok = url == start
        if ok and self.cfg.get("exclude") and re.search(self.cfg["exclude"], url):
            ok = False
        return ok

    def discover(self) -> Iterator[Item]:
        from bs4 import BeautifulSoup

        start = self.cfg["start_url"].strip()
        max_pages, max_depth = int(self.cfg.get("max_pages") or 50), int(self.cfg.get("max_depth") or 3)
        interval = float(self.cfg.get("min_interval_s") or 0)
        robots = bool(self.cfg.get("respect_robots"))
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        seen: set[str] = {normalise_url(start)}
        docs_seen: set[str] = set()
        if self.cfg.get("use_sitemap"):
            for loc in _sitemap_urls(self, start):
                n = normalise_url(loc)
                if n not in seen and self._in_scope(loc, start):
                    seen.add(n)
                    queue.append((loc, 1))
        pages = 0
        while queue and pages < max_pages and not self.ctx.cancelled():
            url, depth = queue.popleft()
            try:
                r = self.polite_get(url, min_interval=interval, respect_robots=robots)
            except ConnectorError as e:
                self.ctx.log(f"skip {url}: {e}")
                continue
            if r.status_code >= 400:
                self.ctx.log(f"skip {url}: HTTP {r.status_code}")
                continue
            ctype = (r.headers.get("content-type") or "").split(";")[0].lower()
            final = str(r.url)
            if "html" not in ctype:  # the start URL or a redirect landed on a document
                data = check_max_bytes(r.content, self.cfg, final)
                yield Item(
                    key=normalise_url(final),
                    filename=filename_for(final, ctype),
                    fetch=lambda d=data: d,
                    url=final,
                    size=len(data),
                )
                pages += 1
                continue
            html = r.content
            pages += 1
            yield Item(
                key=normalise_url(final),
                filename=filename_for(final, "text/html"),
                fetch=lambda d=html: d,
                url=final,
                size=len(html),
            )
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or href.lower().startswith(SKIP_SCHEMES):
                    continue
                link = urljoin(final, href).split("#", 1)[0]
                if not link.startswith(("http://", "https://")):
                    continue
                n = normalise_url(link)
                path = urlsplit(link).path.lower()
                if path.endswith(NON_DOC_EXTS):
                    continue
                is_doc = path.endswith(DOC_EXTS) and not path.endswith((".html", ".htm"))
                if is_doc:
                    if (
                        self.cfg.get("follow_documents")
                        and n not in docs_seen
                        and _same_site(link, start, self.cfg.get("scope"))
                    ):
                        docs_seen.add(n)
                        yield self._doc_item(link, a.get_text(" ", strip=True), interval, robots)
                    continue
                if n in seen or depth + 1 > max_depth or not self._in_scope(link, start):
                    continue
                seen.add(n)
                queue.append((link, depth + 1))
        if queue and pages >= max_pages:
            self.ctx.log(f"stopped at max_pages={max_pages}; {len(queue)} URLs not visited")

    def _doc_item(self, url: str, anchor: str, interval: float, robots: bool) -> Item:
        def fetch() -> bytes:
            r = self.polite_get(url, min_interval=interval, respect_robots=robots)
            r.raise_for_status()
            return check_max_bytes(r.content, self.cfg, url)

        hints = (
            {"title": anchor}
            if anchor and len(anchor) > 6 and not re.fullmatch(r"(download|pdf|view|click here|link)", anchor, re.I)
            else {}
        )
        return Item(
            key=normalise_url(url),
            filename=filename_for(url, None),
            fetch=fetch,
            url=url,
            hints=hints,
            title=hints.get("title"),
        )


def _same_site(url: str, start: str, scope: str | None) -> bool:
    u, s = urlsplit(url), urlsplit(start)
    if scope == "subdomains":
        return u.hostname == s.hostname or (u.hostname or "").endswith("." + (s.hostname or ""))
    return u.netloc == s.netloc


def _sitemap_urls(c: Connector, start: str) -> list[str]:
    s = urlsplit(start)
    try:
        r = c.polite_get(f"{s.scheme}://{s.netloc}/sitemap.xml", min_interval=0, respect_robots=False)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)  # noqa: S314 - sitemap from the configured site; no DTD processing
    except (ConnectorError, ET.ParseError):
        return []
    return [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text][:5000]


class FeedConnector(Connector):
    kind = "feed"
    label = "RSS, Atom or sitemap"
    description = "Reads a feed or sitemap and ingests each linked page or document, with its published date."
    icon = "📰"
    fields = [
        F("url", "Feed or sitemap URL", "url", required=True, placeholder="https://example.gov.in/rss.xml"),
        F("min_interval_s", "Seconds between requests", "number", default=1.0),
        F("respect_robots", "Respect robots.txt", "bool", default=True),
        F("max_file_mb", "Max file size (MB)", "number", default=100),
        *COMMON_FIELDS,
    ]

    def discover(self) -> Iterator[Item]:
        r = self.polite_get(self.cfg["url"], min_interval=0, respect_robots=bool(self.cfg.get("respect_robots")))
        r.raise_for_status()
        try:
            root = ET.fromstring(r.content)  # noqa: S314 - configured feed; no DTD processing
        except ET.ParseError as e:
            raise ConnectorError(f"not a valid feed or sitemap: {e}") from e
        interval = float(self.cfg.get("min_interval_s") or 0)
        robots = bool(self.cfg.get("respect_robots"))
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag not in ("item", "entry", "url"):
                continue
            kids = {c.tag.rsplit("}", 1)[-1]: c for c in el}
            link = None
            if "link" in kids:
                link = (kids["link"].text or kids["link"].get("href") or "").strip()
            elif "loc" in kids:
                link = (kids["loc"].text or "").strip()
            if not link:
                continue
            title = (kids["title"].text or "").strip() if "title" in kids and kids["title"].text else None
            when = next(
                (kids[k].text for k in ("pubDate", "updated", "published", "lastmod") if k in kids and kids[k].text),
                None,
            )
            hints = {k: v for k, v in (("title", title), ("date", to_iso(when) if when else None)) if v}
            link = urljoin(str(r.url), link)

            def fetch(u: str = link) -> bytes:
                rr = self.polite_get(u, min_interval=interval, respect_robots=robots)
                rr.raise_for_status()
                return check_max_bytes(rr.content, self.cfg, u)

            yield Item(
                key=normalise_url(link),
                filename=filename_for(link, None),
                fetch=fetch,
                url=link,
                title=title,
                hints=hints,
            )
