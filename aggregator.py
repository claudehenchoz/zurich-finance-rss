"""Filter Swiss newspaper RSS feeds down to Zurich financial-services coverage.

The module is deliberately a single file: the whole program is configuration,
feed fetching, regex filtering, deduplication and deterministic RSS rendering.
"""

from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import feedparser

USER_AGENT = "zurich-finance-rss/0.1 (+https://github.com/)"
FETCH_TIMEOUT = 30
MEDIA_NS = "http://search.yahoo.com/mrss/"

ET.register_namespace("media", MEDIA_NS)


@dataclass(frozen=True)
class Source:
    name: str
    url: str

    def __str__(self) -> str:  # pragma: no cover - completeness only
        return f"{self.name} <{self.url}>"


@dataclass(frozen=True)
class Config:
    title: str
    description: str
    link: str
    language: str
    output: Path
    max_items: int
    sources: tuple[Source, ...]
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class Image:
    url: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Article:
    title: str
    link: str
    summary: str
    published: datetime | None
    source: str
    source_url: str
    image: Image | None = None


def _require_str(raw: dict, key: str, where: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: setting {key!r} must be a non-empty string")
    return value.strip()


def load_config(path: Path) -> Config:
    """Read and validate ``path`` before anything is fetched or written."""

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    title = _require_str(raw, "title", path)
    description = _require_str(raw, "description", path)
    link = _require_str(raw, "link", path)
    language = _require_str(raw, "language", path)
    output_value = _require_str(raw, "output", path)

    max_items = raw.get("max_items")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        raise ValueError(f"{path}: setting 'max_items' must be a positive integer")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"{path}: setting 'sources' must be a non-empty array of tables")

    sources: list[Source] = []
    for index, entry in enumerate(raw_sources):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: setting 'sources[{index}]' must be a table")
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}: setting 'sources[{index}].name' must be a non-empty string")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"{path}: setting 'sources[{index}].url' must be a non-empty string")
        sources.append(Source(name=name.strip(), url=url.strip()))

    raw_patterns = raw.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise ValueError(f"{path}: setting 'patterns' must be a non-empty array of strings")

    patterns: list[str] = []
    for index, pattern in enumerate(raw_patterns):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{path}: setting 'patterns[{index}]' must be a non-empty string")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"{path}: setting 'patterns[{index}]' is not a valid regular expression "
                f"({pattern!r}): {exc}"
            ) from exc
        patterns.append(pattern)

    return Config(
        title=title,
        description=description,
        link=link,
        language=language,
        output=(path.parent / output_value).resolve(),
        max_items=max_items,
        sources=tuple(sources),
        patterns=tuple(patterns),
    )


def fetch_url(url: str) -> bytes:
    """Fetch one feed. Failures propagate; a run never retries."""

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1",
        },
    )
    with urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def canonicalize_link(link: str) -> str:
    """Lowercase scheme/host, drop fragment and tracking params, trim trailing slash."""

    parts = urlsplit(link)
    scheme = parts.scheme.lower()
    netloc = parts.hostname.lower() if parts.hostname else ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"

    query = ""
    if parts.query:
        kept = [
            segment
            for segment in parts.query.split("&")
            if segment
            and not unquote_plus(segment.split("=", 1)[0]).lower().startswith("utm_")
        ]
        query = "&".join(kept)

    path = parts.path
    if path not in ("", "/") and path.endswith("/"):
        path = path[:-1]

    return urlunsplit((scheme, netloc, path, query, ""))


def _entry_timestamp(entry: dict) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (OverflowError, OSError, ValueError, TypeError):
                continue
    return None


def _is_web_link(link: str) -> bool:
    try:
        parts = urlsplit(link)
        parts.port  # noqa: B018 - raises ValueError for malformed ports
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _dimension(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _media_url(media: dict, *keys: str) -> str | None:
    for key in keys:
        value = media.get(key)
        if isinstance(value, str) and _is_web_link(value.strip()):
            return value.strip()
    return None


def _entry_image(entry: dict) -> Image | None:
    """First usable publisher image: media:thumbnail, image media:content, image enclosure."""

    for media in entry.get("media_thumbnail") or ():
        if isinstance(media, dict) and (url := _media_url(media, "url")):
            return Image(
                url=url,
                width=_dimension(media.get("width")),
                height=_dimension(media.get("height")),
            )

    for media in entry.get("media_content") or ():
        if not isinstance(media, dict):
            continue
        medium = str(media.get("medium") or "").lower()
        mime = str(media.get("type") or "").lower()
        if medium == "image" or mime.startswith("image/"):
            if url := _media_url(media, "url"):
                return Image(url=url)

    for media in entry.get("enclosures") or ():
        if not isinstance(media, dict):
            continue
        if str(media.get("type") or "").lower().startswith("image/"):
            if url := _media_url(media, "href", "url"):
                return Image(url=url)

    return None


def aggregate(config: Config, fetcher: Callable[[str], bytes] = fetch_url) -> list[Article]:
    """Fetch every source, keep matching entries, dedupe, sort and cap."""

    patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in config.patterns)
    articles: list[Article] = []
    seen_links: set[str] = set()

    for source in config.sources:
        try:
            payload = fetcher(source.url)
            parsed = feedparser.parse(payload)
            if not parsed.get("version"):
                raise ValueError("response is not a recognised RSS or Atom feed")
        except Exception as exc:  # noqa: BLE001 - re-raised with source context
            raise RuntimeError(
                f"Source {source.name!r} ({source.url}) failed: {exc}"
            ) from exc

        entries = parsed.get("entries", [])
        matched = 0
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            summary = str(entry.get("summary") or "").strip()

            if not title or not link or not _is_web_link(link):
                print(
                    f"warning: skipping entry without title or HTTP(S) link in {source.name!r}",
                    file=sys.stderr,
                )
                continue

            haystack = f"{title}\n{summary}"
            if not any(pattern.search(haystack) for pattern in patterns):
                continue

            matched += 1
            canonical = canonicalize_link(link)
            if canonical in seen_links:
                continue
            seen_links.add(canonical)

            articles.append(
                Article(
                    title=title,
                    link=canonical,
                    summary=summary,
                    published=_entry_timestamp(entry),
                    source=source.name,
                    source_url=source.url,
                    image=_entry_image(entry),
                )
            )

        print(f"{source.name}: {len(entries)} entries, {matched} matched")

    articles.sort(
        key=lambda article: (
            article.published is None,
            -article.published.timestamp() if article.published else 0.0,
            article.title.casefold(),
            article.link,
        )
    )
    return articles[: config.max_items]


def render_feed(articles: Iterable[Article], config: Config) -> bytes:
    """Render a deterministic RSS 2.0 document for ``config``."""

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = config.title
    ET.SubElement(channel, "link").text = config.link
    ET.SubElement(channel, "description").text = config.description
    ET.SubElement(channel, "language").text = config.language
    ET.SubElement(channel, "generator").text = "zurich-finance-rss/0.1"
    ET.SubElement(channel, "ttl").text = "60"

    for article in articles:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = article.title
        ET.SubElement(item, "link").text = article.link
        ET.SubElement(item, "description").text = article.summary
        if article.published is not None:
            ET.SubElement(item, "pubDate").text = format_datetime(
                article.published, usegmt=True
            )
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true"})
        guid.text = article.link
        source = ET.SubElement(item, "source", {"url": article.source_url})
        source.text = article.source
        if article.image is not None:
            attributes = {"url": article.image.url}
            if article.image.width is not None:
                attributes["width"] = str(article.image.width)
            if article.image.height is not None:
                attributes["height"] = str(article.image.height)
            ET.SubElement(item, f"{{{MEDIA_NS}}}thumbnail", attributes)

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="utf-8")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + body + b"\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="feeds.toml", type=Path, help="TOML configuration file"
    )
    parser.add_argument(
        "--feed-link",
        default=None,
        help="public URL of this feed (overrides FEED_LINK and feeds.toml)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    feed_link = args.feed_link or os.environ.get("FEED_LINK") or config.link
    if feed_link != config.link:
        config = replace(config, link=feed_link)

    articles = aggregate(config)
    payload = render_feed(articles, config)
    config.output.write_bytes(payload)
    print(f"Wrote {len(articles)} articles to {config.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
