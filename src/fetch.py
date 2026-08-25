"""Ingestion. Pulls every source, returns a flat list of raw items.

No filtering happens here. A source that fails is recorded as a failure and
reported — a silent parser looks exactly like a quiet day, and that is the one
failure mode worth engineering against.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import urllib.parse
from dataclasses import dataclass, field, asdict

import feedparser
import requests

log = logging.getLogger("fetch")

GNEWS_BASE = "https://news.google.com/rss/search"


@dataclass
class Item:
    source_id: str
    lang: str
    stream: str
    title: str
    url: str
    published_at: str
    query: str = ""
    doc_id: str = ""
    # filled downstream
    title_en: str = ""
    category: str = ""
    score: int = 0
    rationale: str = ""

    def __post_init__(self):
        if not self.doc_id:
            seed = f"{self.source_id}|{self.url}|{self.title}"
            self.doc_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def as_dict(self):
        return asdict(self)


@dataclass
class FetchResult:
    items: list = field(default_factory=list)
    ok: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    @property
    def status_line(self) -> str:
        total = len(self.ok) + len(self.failed)
        base = f"{len(self.ok)}/{total} sources ok"
        if self.failed:
            base += f" · failed: {', '.join(self.failed)}"
        return base


def _cutoff(hours: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


def _parse_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            return dt.datetime(*val[:6], tzinfo=dt.timezone.utc)
    return None


def _from_feed(url: str, source, settings, query: str = "") -> list[Item]:
    """Parse one RSS/Atom feed into Items inside the lookback window."""
    resp = requests.get(
        url,
        timeout=settings.get("http_timeout", 20),
        headers={"User-Agent": settings.get("user_agent", "sundeed-watch/1.0")},
    )
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    cutoff = _cutoff(settings.get("lookback_hours", 30))
    out = []
    for entry in parsed.entries[: settings.get("max_items_per_query", 100)]:
        ts = _parse_time(entry)
        # Undated entries are kept: dropping them would be a filter, and a
        # feed with broken dates should not silently vanish from the report.
        if ts and ts < cutoff:
            continue
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue
        out.append(
            Item(
                source_id=source["id"],
                lang=source["lang"],
                stream=source.get("stream", "news"),
                title=title,
                url=link,
                published_at=ts.isoformat() if ts else "",
                query=query,
            )
        )
    return out


def _gnews_urls(source, keywords, settings) -> list[tuple[str, str]]:
    """Expand a keyword set into (query, feed_url) pairs.

    One feed per query on purpose. A single broad query hits the ~100 item cap
    and silently truncates; narrow queries fan out and each returns its own
    hundred.
    """
    kset = keywords[source["keyword_set"]]
    group = source.get("keyword_group")

    queries: list[str] = []
    if group:
        queries = list(kset.get(group, []))
    else:
        for key, val in kset.items():
            if key in ("locale", "extra_locales") or not isinstance(val, list):
                continue
            queries.extend(val)

    locales = [kset["locale"]] + list(kset.get("extra_locales", []))
    # Recency operator, appended to every query. Google News returns a stale
    # index by default; when: aligns the retrieval window with the run cadence.
    suffix = kset.get("suffix", "") or settings.get("gnews_recency", "")
    pairs = []
    for q in queries:
        q = f"{q} {suffix}".strip() if suffix else q
        for loc in locales:
            params = urllib.parse.urlencode(
                {"q": q, "hl": loc["hl"], "gl": loc["gl"], "ceid": loc["ceid"]}
            )
            pairs.append((q, f"{GNEWS_BASE}?{params}"))
    return pairs


def _gazette_terms(source, keywords) -> list[str]:
    """Terms that make a gazette entry relevant.

    A gazette has no search endpoint — it publishes the whole daily edition,
    most of which is naval procurement and university appointments. These
    terms stand in for the query that news sources get for free; without
    them the feed is the entire Boletin.
    """
    kset = keywords.get(source["lang"], {})
    return [t.lower() for t in kset.get("gazette_match", [])]


def _matches(title: str, terms: list[str]) -> bool:
    if not terms:
        return True
    low = title.lower()
    return any(t in low for t in terms)


def _from_boe(source, settings, terms) -> list[Item]:
    """BOE open-data summary for today, JSON."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    url = source["url"].format(yyyymmdd=stamp)
    resp = requests.get(
        url,
        timeout=settings.get("http_timeout", 20),
        headers={
            "Accept": "application/json",
            "User-Agent": settings.get("user_agent", "sundeed-watch/1.0"),
        },
    )
    if resp.status_code == 404:
        return []  # no edition today (Sundays, holidays) — not a failure
    resp.raise_for_status()

    payload = resp.json()
    out = []

    def walk(node):
        """The summary nests diario > seccion > departamento > epigrafe > item
        with inconsistent list-vs-dict shapes. Walking generically is more
        robust than mirroring the schema."""
        if isinstance(node, dict):
            title = node.get("titulo")
            link = node.get("url_pdf") or node.get("url_html")
            if isinstance(link, dict):
                link = link.get("texto") or link.get("url")
            if (isinstance(title, str) and isinstance(link, str)
                    and title.strip() and _matches(title, terms)):
                if link.startswith("/"):
                    link = "https://www.boe.es" + link
                out.append(
                    Item(
                        source_id=source["id"],
                        lang=source["lang"],
                        stream=source.get("stream", "regulatory"),
                        title=title.strip(),
                        url=link,
                        published_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    )
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return out


def fetch_all(config, keywords) -> FetchResult:
    settings = config.get("settings", {})
    result = FetchResult()

    for source in config["sources"]:
        sid = source["id"]
        try:
            if source["kind"] == "rss":
                items = _from_feed(source["url"], source, settings)
                if source.get("stream") == "regulatory":
                    terms = _gazette_terms(source, keywords)
                    items = [i for i in items if _matches(i.title, terms)]

            elif source["kind"] == "boe_api":
                items = _from_boe(source, settings, _gazette_terms(source, keywords))

            elif source["kind"] == "gnews":
                items = []
                pairs = _gnews_urls(source, keywords, settings)
                sub_fail = 0
                for query, url in pairs:
                    try:
                        items.extend(_from_feed(url, source, settings, query=query))
                    except Exception as exc:  # one bad query must not kill the set
                        sub_fail += 1
                        log.warning("gnews query failed (%s): %s", query, exc)
                if sub_fail and sub_fail == len(pairs):
                    raise RuntimeError("all gnews queries failed")

            else:
                raise ValueError(f"unknown source kind: {source['kind']}")

            result.items.extend(items)
            result.ok.append(sid)
            log.info("%s → %d items", sid, len(items))

        except Exception as exc:
            result.failed.append(sid)
            log.error("%s FAILED: %s", sid, exc)

    return result
