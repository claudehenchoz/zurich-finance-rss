"""Offline behavior tests: no test may touch the network."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

import aggregator

UBS_PATTERN = r"\bUBS\b"


def write_config(
    tmp_path: Path,
    *,
    sources: list[tuple[str, str]],
    patterns: list[str],
    max_items: int = 100,
    link: str = "http://localhost/feed.xml",
    output: str = "feed.xml",
) -> Path:
    lines = [
        'title = "Test Feed"',
        'description = "Test description"',
        f'link = "{link}"',
        'language = "de-ch"',
        f'output = "{output}"',
        f"max_items = {max_items}",
        "",
        "patterns = [",
    ]
    lines += [f"  '{pattern}'," for pattern in patterns]
    lines += ["]", ""]
    for name, url in sources:
        lines += ["[[sources]]", f'name = "{name}"', f'url = "{url}"', ""]
    path = tmp_path / "feeds.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rss(items: list[dict[str, str]]) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0"><channel>',
        "<title>Publisher</title>",
        "<link>https://publisher.example/</link>",
        "<description>Publisher feed</description>",
    ]
    for item in items:
        parts.append("<item>")
        parts.append(f"<title>{escape(item['title'])}</title>")
        parts.append(f"<link>{escape(item['link'])}</link>")
        if item.get("description") is not None:
            parts.append(f"<description>{escape(item['description'])}</description>")
        if item.get("pubDate"):
            parts.append(f"<pubDate>{item['pubDate']}</pubDate>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def install_feeds(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bytes | Exception]) -> list[str]:
    """Patch ``aggregator.urlopen`` to serve in-memory feeds; return requested URLs."""

    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        url = getattr(request, "full_url", request)
        calls.append(url)
        value = mapping[url]
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)

    monkeypatch.setattr(aggregator, "urlopen", fake_urlopen)
    return calls


def generate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[Path, ET.Element]:
    config_path = write_config(tmp_path, **kwargs)
    assert aggregator.main(["--config", str(config_path), "--feed-link", "https://example.test/feed.xml"]) == 0
    output = tmp_path / "feed.xml"
    return output, ET.parse(output).getroot()


def test_end_to_end_generation_and_determinism(tmp_path, monkeypatch):
    feed_one = "https://publisher-one.example/rss"
    feed_two = "https://publisher-two.example/rss"
    mapping = {
        feed_one: rss(
            [
                {
                    "title": "UBS raises outlook",
                    "link": "https://News.Example.com/story/1/?utm_source=feed&utm_medium=rss&id=42#top",
                    "description": "The bank lifted guidance.",
                    "pubDate": "Tue, 03 Jun 2025 10:00:00 GMT",
                },
                {
                    "title": "UBS cuts jobs",
                    "link": "https://news.example.com/story/1?id=42&utm_source=other",
                    "description": "Duplicate of the same story via another channel.",
                    "pubDate": "Tue, 03 Jun 2025 09:00:00 GMT",
                },
                {
                    "title": "Local weather",
                    "link": "https://news.example.com/weather",
                    "description": "Sunny all week.",
                },
            ]
        ),
        feed_two: rss(
            [
                {
                    "title": "Julius Bär names new chief executive",
                    "link": "https://publisher-two.example/bank/ceo",
                    "description": "Zurich private bank announcement.",
                    "pubDate": "Mon, 02 Jun 2025 08:30:00 GMT",
                }
            ]
        ),
    }
    install_feeds(monkeypatch, mapping)

    output, root = generate(
        tmp_path,
        monkeypatch,
        sources=[("News One", feed_one), ("News Two", feed_two)],
        patterns=[UBS_PATTERN, r"\bJulius[ -]B(?:ä|ae)r\b"],
    )

    assert root.tag == "rss" and root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("link") == "https://example.test/feed.xml"

    items = channel.findall("item")
    assert len(items) == 2
    titles = [item.findtext("title") for item in items]
    assert sum("UBS" in title for title in titles) == 1
    assert "Local weather" not in titles

    ubs = next(item for item in items if "UBS" in item.findtext("title", ""))
    assert ubs.findtext("link") == "https://news.example.com/story/1?id=42"
    assert ubs.findtext("guid") == ubs.findtext("link")
    assert ubs.find("guid").get("isPermaLink") == "true"
    assert ubs.findtext("description") == "The bank lifted guidance."
    assert ubs.findtext("source") == "News One"
    assert ubs.find("source").get("url") == feed_one
    stamp = parsedate_to_datetime(ubs.findtext("pubDate"))
    assert stamp == datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc)

    for item in items:
        link = item.findtext("link", "")
        assert "utm_" not in link
        assert "#" not in link

    other = next(item for item in items if item is not ubs)
    assert other.findtext("source") == "News Two"
    assert other.find("source").get("url") == feed_two

    first_bytes = output.read_bytes()
    assert channel.find("lastBuildDate") is None
    assert b"lastBuildDate" not in first_bytes

    assert aggregator.main(
        ["--config", str(tmp_path / "feeds.toml"), "--feed-link", "https://example.test/feed.xml"]
    ) == 0
    assert output.read_bytes() == first_bytes


def test_or_semantics_over_title_and_summary(tmp_path, monkeypatch):
    feed = "https://publisher.example/rss"
    install_feeds(
        monkeypatch,
        {
            feed: rss(
                [
                    {
                        "title": "UBS outlook improves",
                        "link": "https://publisher.example/title-match",
                        "description": "Unrelated body text.",
                    },
                    {
                        "title": "Markets",
                        "link": "https://publisher.example/summary-match",
                        "description": "Analysts say ubs shares are cheap.",
                    },
                    {
                        "title": "Weather",
                        "link": "https://publisher.example/no-match",
                        "description": "Sunny all week.",
                    },
                ]
            )
        },
    )
    config = aggregator.load_config(
        write_config(
            tmp_path,
            sources=[("Publisher", feed)],
            patterns=[UBS_PATTERN],
        )
    )

    articles = aggregator.aggregate(config)

    assert sorted(article.title for article in articles) == ["Markets", "UBS outlook improves"]


def test_ordering_undated_and_cap(tmp_path, monkeypatch):
    feed = "https://publisher.example/rss"

    def dated(title: str, link: str, when: str | None) -> dict[str, str]:
        return {
            "title": title,
            "link": link,
            "description": "match",
            "pubDate": when,
        }

    install_feeds(
        monkeypatch,
        {
            feed: rss(
                [
                    dated("Zebra", "https://publisher.example/zebra", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Alpha", "https://publisher.example/alpha", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Beta", "https://publisher.example/beta", "Mon, 02 Jun 2025 12:00:00 GMT"),
                    dated(
                        "Dup",
                        "https://publisher.example/beta?utm_source=again",
                        "Mon, 02 Jun 2025 12:00:00 GMT",
                    ),
                    dated("Undated", "https://publisher.example/undated", None),
                    dated("Same", "https://publisher.example/same-b", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Same", "https://publisher.example/same-a", "Sun, 01 Jun 2025 12:00:00 GMT"),
                ]
            )
        },
    )
    config = aggregator.load_config(
        write_config(
            tmp_path,
            sources=[("Publisher", feed)],
            patterns=[r"\bmatch\b"],
        )
    )

    articles = aggregator.aggregate(config)
    assert [article.title for article in articles] == [
        "Beta",
        "Alpha",
        "Same",
        "Same",
        "Zebra",
        "Undated",
    ]
    assert [article.link for article in articles[2:4]] == [
        "https://publisher.example/same-a",
        "https://publisher.example/same-b",
    ]
    assert articles[-1].published is None

    root = ET.fromstring(aggregator.render_feed(articles, config))
    items = root.findall("./channel/item")
    assert items[-1].findtext("title") == "Undated"
    assert items[-1].find("pubDate") is None
    assert all(item.find("pubDate") is not None for item in items[:-1])

    capped = aggregator.aggregate(replace(config, max_items=3))
    assert [article.title for article in capped] == ["Beta", "Alpha", "Same"]
    assert capped[1].link == "https://publisher.example/alpha"


def test_valid_empty_result(tmp_path, monkeypatch):
    feed = "https://publisher.example/rss"
    calls = install_feeds(
        monkeypatch,
        {
            feed: rss(
                [
                    {
                        "title": "Weather",
                        "link": "https://publisher.example/weather",
                        "description": "Sunny.",
                    }
                ]
            )
        },
    )
    output, root = generate(
        tmp_path,
        monkeypatch,
        sources=[("Publisher", feed)],
        patterns=[UBS_PATTERN],
    )

    assert calls == [feed]
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "Test Feed"
    assert channel.findall("item") == []
    assert output.read_bytes().startswith(b'<?xml version="1.0" encoding="utf-8"?>')

    empty = ET.fromstring(aggregator.render_feed([], aggregator.load_config(tmp_path / "feeds.toml")))
    assert empty.findall("./channel/item") == []


def test_source_failure_preserves_previous_output(tmp_path, monkeypatch):
    good = "https://publisher.example/good"
    bad = "https://publisher.example/bad"
    install_feeds(
        monkeypatch,
        {
            good: rss(
                [
                    {
                        "title": "UBS story",
                        "link": "https://publisher.example/story",
                        "description": "match",
                    }
                ]
            ),
            bad: OSError("connection refused"),
        },
    )
    output = tmp_path / "feed.xml"
    sentinel = b"previous feed bytes\n"
    output.write_bytes(sentinel)
    config_path = write_config(
        tmp_path,
        sources=[("Good Source", good), ("Failing Source", bad)],
        patterns=[UBS_PATTERN],
    )

    with pytest.raises(RuntimeError, match="Failing Source"):
        aggregator.main(["--config", str(config_path)])

    assert output.read_bytes() == sentinel


def test_configuration_validation_happens_before_fetching(tmp_path, monkeypatch):
    def forbidden_fetch(*args, **kwargs):
        raise AssertionError("fetcher must not be called for invalid configuration")

    monkeypatch.setattr(aggregator, "urlopen", forbidden_fetch)
    source = [("Publisher", "https://publisher.example/rss")]

    broken_regex = write_config(tmp_path, sources=source, patterns=["(unclosed"])
    with pytest.raises(ValueError, match="patterns"):
        aggregator.main(["--config", str(broken_regex)])

    no_patterns = write_config(tmp_path, sources=source, patterns=[])
    with pytest.raises(ValueError, match="patterns"):
        aggregator.main(["--config", str(no_patterns)])

    no_sources = write_config(tmp_path, sources=[], patterns=[UBS_PATTERN])
    with pytest.raises(ValueError, match="sources"):
        aggregator.load_config(no_sources)

    no_output = write_config(tmp_path, sources=source, patterns=[UBS_PATTERN])
    no_output.write_text(
        re.sub(r'^output = .*$', 'output = ""', no_output.read_text(encoding="utf-8"), flags=re.M),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="output"):
        aggregator.load_config(no_output)
