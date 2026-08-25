"""Ingestion. Pulls every source, returns a flat list of raw items.

No filtering happens here. A source that fails is recorded as a failure and
reported — a silent parser looks exactly like a quiet day, and that is the one
failure mode worth engineering against.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import logging
import time
import urllib.parse
from dataclasses import dataclass, field, asdict

import feedparser
import requests

log = logging.getLogger("fetch")

GNEWS_BASE = "https://news.google.com/rss/search"

# Google News appends " - Publisher" to every title. That suffix is the only
# place the publisher is available before the redirect is resolved — and
# resolution happens after selection, far too late to inform ranking.
GNEWS_TAIL = re.compile(r"\s+[-–—]\s+([^-–—]{2,40})$")

# A polite custom User-Agent gets refused by Cloudflare and by several
# government sites — the request never reaches the feed. article.py already
# used a browser string and fetched fine from the same hosts, which is what
# gave this away.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FEED_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en,es;q=0.9,pt;q=0.8,ar;q=0.7",
}

# Keys in a language block that are configuration, not query groups. Anything
# ending in _match is a term list used to narrow a feed that has no query of
# its own — feeding those to Google News turns a narrowing list into the
# broadest possible search, which is exactly backwards.
NON_QUERY_KEYS = {"locale", "extra_locales", "suffix"}


@dataclass
class Item:
    source_id: str
    lang: str
    stream: str
    title: str
    url: str
    published_at: str
    query: str = ""
    publisher: str = ""
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
    reasons: dict = field(default_factory=dict)

    @property
    def status_line(self) -> str:
        total = len(self.ok) + len(self.failed)
        base = f"{len(self.ok)}/{total} sources ok"
        if self.failed:
            base += f" · failed: {', '.join(self.failed)}"
        return base

    @property
    def failure_detail(self) -> str:
        """One line per dead source, short enough for a Telegram message.

        A source that has been dead for a week and a source that died this
        morning look identical in a count. The reason is the only thing that
        tells you which — and it should not live somewhere you have to go
        and dig for it.
        """
        return "\n".join(f"{sid}: {why}" for sid, why in self.reasons.items())


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Publishers file paid placements under a path segment. It is the only
# machine-readable signal that a piece is advertising, and it disappears the
# moment a Google News redirect wraps the link — which is why this is checked
# again after resolution rather than only at ingest.
SPONSORED_MARKERS = (
    "sponsored", "advertorial", "partner-content", "partnercontent",
    "paid-post", "paid-content", "branded-content", "brandedcontent",
    "native-ad", "promoted", "press-release", "advertisement",
)


def is_sponsored(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url or "").path.lower()
    except ValueError:
        return False
    return any(m in path for m in SPONSORED_MARKERS)


def authority(publisher: str, config) -> str:
    """primary | specialist | general.

    primary     official gazettes, statistics offices, regulators
    specialist  sector trade press — the reason this distinction exists
    general     everything else

    Matched loosely: the publisher is sometimes a domain and sometimes the
    display name Google chose, and the map should catch both.
    """
    table = config.get("authority", {})
    needle = (publisher or "").lower()
    if not needle:
        return "general"
    for tier in ("primary", "specialist"):
        for entry in table.get(tier, []):
            e = entry.lower()
            if e in needle or needle in e:
                return tier
    return "general"


def _cutoff(hours: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


def _parse_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            return dt.datetime(*val[:6], tzinfo=dt.timezone.utc)
    return None


# 429 and 503 mean "slow down", not "broken". Retrying immediately makes it
# worse — the delay has to grow.
BACKOFF = (3, 8, 20)
THROTTLE_CODES = {429, 503}


def _get(url: str, settings, headers=None):
    """GET with backoff. Raises with the status code attached."""
    last = None
    for attempt, wait in enumerate(BACKOFF, start=1):
        try:
            resp = requests.get(
                url,
                timeout=settings.get("http_timeout", 25),
                headers=headers or FEED_HEADERS,
            )
            if resp.status_code in THROTTLE_CODES:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} from {url[:70]}")
            if resp.status_code >= 400:
                # A 404 or 403 will not improve by waiting.
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} from {url[:70]}")
            return resp
        except requests.HTTPError as exc:
            last = exc
            if "HTTP 429" not in str(exc) and "HTTP 503" not in str(exc):
                raise
            if attempt < len(BACKOFF):
                time.sleep(wait)
        except Exception as exc:
            last = exc
            if attempt < len(BACKOFF):
                time.sleep(2)
    raise last


def _from_feed(url: str, source, settings, query: str = "") -> list[Item]:
    """Parse one RSS/Atom feed into Items inside the lookback window."""
    resp = _get(url, settings)
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception}")

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

        publisher = source.get("publisher", "")
        if source["kind"] == "gnews":
            m = GNEWS_TAIL.search(title)
            if m:
                publisher = m.group(1).strip()
                title = title[: m.start()].strip()
        if not publisher:
            publisher = _domain(link)

        out.append(
            Item(
                source_id=source["id"],
                lang=source["lang"],
                stream=source.get("stream", "news"),
                title=title,
                url=link,
                published_at=ts.isoformat() if ts else "",
                query=query,
                publisher=publisher,
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
            if key in NON_QUERY_KEYS or key.endswith("_match"):
                continue
            if not isinstance(val, list):
                continue
            queries.extend(val)

    locales = [kset["locale"]] + list(kset.get("extra_locales", []))
    cap = settings.get("max_queries_per_source", 60)
    if len(queries) > cap:
        log.error("%s expanded to %d queries, capping at %d — check keywords.yml",
                  source["id"], len(queries), cap)
        queries = queries[:cap]
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


def _match_terms(source, keywords) -> list[str]:
    """Terms that stand in for a query the source cannot be given.

    Google News narrows at the source: the keyword IS the query. A gazette or
    a whole-section RSS feed has no such endpoint — it hands over the entire
    daily edition or the entire business desk. These terms are the missing
    query, not a filter layered on top of results.

    A source opts in with `match_key`, naming a list in its language block.
    No match_key means no narrowing.
    """
    key = source.get("match_key")
    if not key:
        return []
    kset = keywords.get(source["lang"], {})
    return [t.lower() for t in kset.get(key, [])]


def _matches(title: str, terms: list[str]) -> bool:
    if not terms:
        return True
    low = title.lower()
    return any(t in low for t in terms)


def _from_boe(source, settings, terms) -> list[Item]:
    """BOE open-data summary for today, JSON."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    url = source["url"].format(yyyymmdd=stamp)
    try:
        resp = _get(url, settings,
                    headers=dict(FEED_HEADERS, Accept="application/json"))
    except requests.HTTPError as exc:
        if "HTTP 404" in str(exc):
            return []  # no edition today (Sundays, holidays) — not a failure
        raise

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
                terms = _match_terms(source, keywords)
                if terms:
                    before = len(items)
                    items = [i for i in items if _matches(i.title, terms)]
                    log.info("%s match: %d of %d kept", sid, len(items), before)

            elif source["kind"] == "boe_api":
                items = _from_boe(source, settings, _match_terms(source, keywords))

            elif source["kind"] == "gnews":
                items = []
                pairs = _gnews_urls(source, keywords, settings)
                gap = settings.get("gnews_delay", 1.2)
                sub_fail = 0
                streak = 0
                for idx, (query, url) in enumerate(pairs):
                    try:
                        items.extend(_from_feed(url, source, settings, query=query))
                        streak = 0
                    except Exception as exc:  # one bad query must not kill the set
                        sub_fail += 1
                        streak += 1
                        log.warning("gnews query failed (%s): %s", query[:50], exc)
                        # Google throttles the whole client, not one query.
                        # Once it starts refusing, continuing for another
                        # hundred requests deepens the block and wastes the
                        # run — stop and say so.
                        if streak >= 8:
                            raise RuntimeError(
                                f"throttled after {idx + 1}/{len(pairs)} queries "
                                f"({sub_fail} failed) — back off and retry later")
                    if idx < len(pairs) - 1:
                        time.sleep(gap)
                if sub_fail and sub_fail == len(pairs):
                    raise RuntimeError("all gnews queries failed")
                if sub_fail:
                    log.warning("%s: %d of %d queries failed",
                                sid, sub_fail, len(pairs))

            else:
                raise ValueError(f"unknown source kind: {source['kind']}")

            result.items.extend(items)
            result.ok.append(sid)
            log.info("%s → %d items", sid, len(items))

        except Exception as exc:
            result.failed.append(sid)
            reason = f"{type(exc).__name__}: {exc}"
            result.reasons[sid] = reason[:160]
            log.error("%s FAILED: %s", sid, reason)

    return result
