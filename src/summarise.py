"""Synthesis, in two stages.

Stage 1 (`summarise`) reads every headline of the day and picks the top ten.
Stage 2 (`write_up`) receives those ten with their article text — fetched by
article.py in between — and writes the entry for each. Splitting them is what
makes the article fetch affordable: only the selected handful get downloaded.

Wording for both lives in prompts.py.
"""

from __future__ import annotations

import json
import logging
import os

from anthropic import Anthropic

from prompts import SYSTEM, WRITEUP_SYSTEM

log = logging.getLogger("summarise")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000       # thinking shares this budget
MAX_ITEMS = 220          # titles sent for selection; ordering already ranks them


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


def _payload(items) -> str:
    rows = []
    for item in items[:MAX_ITEMS]:
        rows.append(
            json.dumps(
                {
                    "id": item.doc_id,
                    "cat": item.category,
                    "score": item.score,
                    "title": (item.title_en or item.title)[:220],
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(rows)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in response (got {len(text)} chars)")
    end = text.rfind("}")
    if end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    # Truncated mid-object: salvage whatever complete entries exist rather
    # than losing the whole block. Close the open structures and retry.
    fragment = text[start:]
    for closer in ("}]}", "\"}]}", "]}", "}"):
        try:
            return json.loads(fragment.rsplit(",", 1)[0] + closer)
        except json.JSONDecodeError:
            continue
    raise ValueError("response was not parseable JSON")


def _extract_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start == -1:
        raise ValueError(f"no JSON array in response (got {len(text)} chars)")
    if end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    fragment = text[start:]
    for closer in ("}]", "\"}]", "]"):
        try:
            return json.loads(fragment.rsplit(",", 1)[0] + closer)
        except json.JSONDecodeError:
            continue
    raise ValueError("write-up response was not parseable JSON")


def write_up(top: list, texts: dict) -> None:
    """Fill each top entry's `body` from the article text where available.

    Mutates `top` in place. On failure the entry keeps the one-line `why`
    from the selection pass, so the report degrades to the previous format
    rather than losing the item.
    """
    if not top:
        return

    rows = []
    for row in top:
        item = row["item"]
        payload = {
            "id": item.doc_id,
            "headline": item.title_en or item.title,
        }
        body = texts.get(item.doc_id)
        if body:
            payload["article_text"] = body
        rows.append(json.dumps(payload, ensure_ascii=False))

    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=WRITEUP_SYSTEM,
            messages=[{"role": "user", "content": "\n\n".join(rows)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            log.warning("write-up hit max_tokens — raise MAX_TOKENS")
        data = _extract_array(text)
    except Exception as exc:
        log.error("write-up failed: %s", exc)
        return

    from prompts import GEO_TAGS

    by_id = {r["item"].doc_id: r for r in top}
    for entry in data:
        row = by_id.get(entry.get("id"))
        if not row:
            continue
        if entry.get("body"):
            row["why"] = entry["body"].strip()
        # Anything outside the fixed vocabulary is discarded rather than
        # rendered — one invented spelling is enough to fragment a tag.
        geo = [g for g in (entry.get("geo") or [])
               if isinstance(g, str) and g.lower() in GEO_TAGS]
        row["geo"] = [g.lower() for g in geo][:2]


def summarise(items, pref: str = "") -> dict | None:
    """Return {lead, lead_why, summary, watch} or None if unavailable.

    None means the report renders without a top block. It never means an item
    is dropped.
    """
    if not items:
        return None

    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM + (pref or ""),
            messages=[{"role": "user", "content": _payload(items)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            log.warning("synthesis hit max_tokens — raise MAX_TOKENS")
        data = _extract_json(text)
    except Exception as exc:
        # Log enough to diagnose without another run: the reason the model
        # stopped and what it actually said.
        log.error("synthesis failed: %s", exc)
        try:
            log.error("stop_reason=%s | first 300 chars: %s",
                      resp.stop_reason, text[:300].replace("\n", " "))
        except NameError:
            pass
        return None

    # Resolve ids back to real items. An id the model invented is dropped
    # rather than rendered — every line in this block must be clickable
    # through to its source, or the block cannot be checked.
    by_id = {i.doc_id: i for i in items}
    top = []
    for row in (data.get("top") or [])[:10]:
        item = by_id.get(row.get("id"))
        if item is None:
            log.warning("summary referenced unknown id %s — dropped", row.get("id"))
            continue
        top.append({"item": item, "why": (row.get("why") or "").strip(),
                    "sourced": False})

    return {
        "top": top,
        "summary": (data.get("summary") or "").strip(),
        "watch": (data.get("watch") or "").strip(),
    }
