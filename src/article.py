"""Resolve article URLs and pull their text — for the top items only.

Two obstacles sit between an RSS entry and the words in the article:

  1. Google News does not link to the publisher. It links to an encrypted
     redirect (news.google.com/rss/articles/CBMi...). Resolving it means
     scraping decode parameters off the article page and calling Google's
     internal batchexecute endpoint. That endpoint is undocumented, rate
     limited and will break without notice.

  2. Plenty of publishers are paywalled. Inman, the FT and most trade press
     return a stub or a consent wall rather than the article.

Both fail often enough that failure is the normal case, not the exception.
So: fetch only the handful of items that made the top, never the full day,
and record for each one whether the text was actually retrieved. The report
marks headline-only entries so a paragraph built on inference is visibly
different from one built on the article.
"""

from __future__ import annotations

import logging
import time

import requests
import trafilatura

log = logging.getLogger("article")

DECODE_INTERVAL = 2.0     # seconds between Google decodes; below this, 429s
FETCH_TIMEOUT = 20
MAX_CHARS = 6000          # per article, sent to the model
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _resolve(url: str) -> str | None:
    """Google News redirect → publisher URL. Other URLs pass through."""
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
        log.warning("decode returned no url: %s", result.get("message"))
    except Exception as exc:
        log.warning("decode failed: %s", exc)
    return None


def _extract(url: str) -> str | None:
    """Fetch and strip to readable text. None when there is nothing usable."""
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": UA, "Accept-Language": "en,es,pt,ar"},
        )
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception as exc:
        log.warning("fetch failed (%s): %s", url[:60], exc)
        return None

    if not text:
        return None
    text = text.strip()
    # A paywall stub or consent wall extracts to a couple of sentences. Below
    # this length the "article" is not worth sending to the model and would
    # only invite it to pad.
    if len(text) < 400:
        log.info("too short, likely paywalled (%d chars): %s", len(text), url[:60])
        return None
    return text[:MAX_CHARS]


def fetch_for(top_rows: list) -> dict:
    """Attach article text to top entries. Returns {doc_id: text}.

    Never raises. An item with no text keeps its place in the report — it is
    marked headline-only rather than dropped.
    """
    texts: dict[str, str] = {}
    for idx, row in enumerate(top_rows):
        item = row["item"]
        resolved = _resolve(item.url)
        if resolved:
            # Keep the publisher URL the moment it is known. Extraction fails
            # constantly — paywalls, consent walls — and losing the link along
            # with the text leaves the reader holding an unusable
            # news.google.com redirect for an article they can plainly see.
            item.resolved_url = resolved
            text = _extract(resolved)
            if text:
                texts[item.doc_id] = text
                log.info("got %d chars for %s", len(text), item.doc_id)
        if idx < len(top_rows) - 1:
            time.sleep(DECODE_INTERVAL)

    resolved_n = sum(1 for r in top_rows
                     if getattr(r["item"], "resolved_url", ""))
    log.info("resolved %d/%d urls, extracted text for %d",
             resolved_n, len(top_rows), len(texts))
    return texts
