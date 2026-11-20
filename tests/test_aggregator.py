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
        if item.get("extra"):
            parts.append(item["extra"])
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
                    "extra": (
                        '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
                        ' width="200" height="200"'
                        ' url="https://img-msp-prod.nzz.ch/story/1.jpg?width=200&amp;height=200'
                        '&amp;auto=webp&amp;quality=80&amp;fit=crop"/>'
                    ),
                },
                {
                    "title": "UBS cuts jobs",
                    "link": "https://news.example.com/story/1?id=42&utm_source=other",
                    "description": "Duplicate of the same story via another channel.",
                    "pubDate": "Tue, 03 Jun 2025 09:00:00 GMT",
                    "extra": (
                        '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
                        ' width="200" height="200"'
                        ' url="https://img-msp-prod.nzz.ch/duplicate/dropped.jpg?width=200"/>'
                    ),
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

    media = "{http://search.yahoo.com/mrss/}thumbnail"
    thumbnail = ubs.find(media)
    assert thumbnail is not None
    assert thumbnail.get("url") == (
        "https://img-msp-prod.nzz.ch/story/1.jpg?width=200&height=200"
        "&auto=webp&quality=80&fit=crop"
    )
    assert thumbnail.get("width") == "200"
    assert thumbnail.get("height") == "200"
    assert other.find(media) is None
    assert [len(item.findall(media)) for item in items] == [1, 0]

    first_bytes = output.read_bytes()
    assert b"dropped.jpg" not in first_bytes
    assert b'xmlns:media="http://search.yahoo.com/mrss/"' in first_bytes
    assert b"&amp;auto=webp" in first_bytes
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


def test_thumbnail_source_shapes_and_precedence(tmp_path, monkeypatch):
    feed = "https://publisher.example/rss"
    media = "{http://search.yahoo.com/mrss/}thumbnail"
    content_image = (
        '<media:content xmlns:media="http://search.yahoo.com/mrss/"'
        ' type="image/jpeg" medium="image" url="https://cdn.unitycms.io/images/content.jpg"/>'
    )
    image_enclosure = (
        '<enclosure type="image/jpeg" url="https://partner-feeds.20min.ch/image.jpg"'
        ' length="12345"/>'
    )
    audio_enclosure = (
        '<enclosure type="audio/mpeg" url="https://cdn.example/audio.mp3" length="999"/>'
    )
    thumbnail = (
        '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
        ' width="300" height="200" url="https://img.example/winner.jpg"/>'
    )

    def item(title: str, slug: str, extra: str = "") -> dict[str, str]:
        return {
            "title": title,
            "link": f"https://publisher.example/{slug}",
            "description": "match",
            "extra": extra,
        }

    install_feeds(
        monkeypatch,
        {
            feed: rss(
                [
                    item("Content match", "content", content_image),
                    item("Enclosure match", "enclosure", image_enclosure),
                    item("Audio match", "audio", audio_enclosure),
                    item("Plain match", "plain"),
                    item("All match", "all", content_image + image_enclosure + thumbnail),
                ]
            )
        },
    )
    output, root = generate(
        tmp_path,
        monkeypatch,
        sources=[("Publisher", feed)],
        patterns=[r"\bmatch\b"],
    )

    items = root.findall("./channel/item")
    assert len(items) == 5
    by_title = {item.findtext("title"): item for item in items}

    content = by_title["Content match"].find(media)
    assert content is not None
    assert content.get("url") == "https://cdn.unitycms.io/images/content.jpg"
    assert content.get("width") is None and content.get("height") is None

    enclosure = by_title["Enclosure match"].find(media)
    assert enclosure is not None
    assert enclosure.get("url") == "https://partner-feeds.20min.ch/image.jpg"
    assert enclosure.get("width") is None and enclosure.get("height") is None

    winner = by_title["All match"].find(media)
    assert winner is not None
    assert winner.get("url") == "https://img.example/winner.jpg"
    assert winner.get("width") == "300"
    assert winner.get("height") == "200"

    assert by_title["Audio match"].find(media) is None
    assert by_title["Plain match"].find(media) is None
    assert all(item.find("enclosure") is None for item in items)
    assert b"cdn.example/audio.mp3" not in output.read_bytes()


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
    assert b"xmlns:media" not in output.read_bytes()
    assert b"thumbnail" not in output.read_bytes()


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
