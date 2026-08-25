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
MAX_TOKENS = 32000       # thinking shares this budget
SELECT_ITEMS = 150       # sent to the selection pass, highest-scored first
MAX_ITEMS = 220          # hard ceiling on any payload

# Set when a pass fails, so the caller can put the reason in the report
# instead of the reader learning only that "synthesis failed".
last_error = ""


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


def _complete(system: str, content: str) -> tuple[str, str]:
    """One completion, streamed. Returns (text, stop_reason).

    Streaming is not an optimisation here — the SDK refuses a non-streaming
    request whose max_tokens is large enough that it could run past ten
    minutes, and 32k crosses that line. The response is accumulated and
    handed back whole, so callers see no difference.
    """
    with _client().messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        final = stream.get_final_message()
    text = "".join(b.text for b in final.content if b.type == "text")
    return text, final.stop_reason


def _payload(items) -> str:
    rows = []
    for item in items[:SELECT_ITEMS]:
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


def _parse_blocks(text: str, allowed_geo) -> dict:
    """Parse the delimited write-up format into {id: (body, [geo])}.

    Deliberately not JSON. The body is a paragraph of prose containing
    quotation marks, apostrophes, percentages and currency symbols; inside a
    JSON string every one of those is a chance for the whole array to fail to
    parse, taking all ten entries with it. A line-delimited format cannot fail
    that way — a malformed block costs one entry, not the response.
    """
    out = {}
    for chunk in text.split("###"):
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        doc_id = lines[0].strip()
        if not doc_id:
            continue
        geo, body_lines = [], []
        for line in lines[1:]:
            if not body_lines and line.strip().upper().startswith("GEO:"):
                raw = line.split(":", 1)[1]
                geo = [g.strip().lower() for g in raw.split(",") if g.strip()]
                geo = [g for g in geo if g in allowed_geo][:2]
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if body:
            out[doc_id] = (body, geo)
    return out


def write_up(top: list, texts: dict) -> bool:
    global last_error
    """Fill each top entry's body from the article text. Returns success.

    Mutates `top` in place. On failure the entry keeps the one-line `why`
    from the selection pass — which is a selection rationale, not a write-up,
    and reads as a terse conditional. That fallback is nearly indistinguishable
    from a working report, so the caller marks it and the reader is told.
    """
    if not top:
        return False

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

    text = stop = ""
    try:
        text, stop = _complete(WRITEUP_SYSTEM, "\n\n".join(rows))
        if stop == "max_tokens":
            log.warning("write-up hit max_tokens — raise MAX_TOKENS")
        from prompts import GEO_TAGS
        data = _parse_blocks(text, set(GEO_TAGS))
        if not data:
            raise ValueError(f"no blocks parsed from {len(text)} chars")
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc} [stop={stop}, {len(text)} chars]"
        log.error("write-up failed: %s | first 400: %s",
                  last_error, text[:400].replace("\n", " "))
        return False

    by_id = {r["item"].doc_id: r for r in top}
    for doc_id, (body, geo) in data.items():
        row = by_id.get(doc_id)
        if not row:
            log.warning("write-up returned unknown id %s — dropped", doc_id)
            continue
        row["why"] = body
        row["geo"] = geo
        row["written"] = True

    written = sum(1 for r in top if r.get("written"))
    log.info("write-up: %d of %d entries filled", written, len(top))
    return written > 0


def summarise(items, pref: str = "") -> dict | None:
    global last_error
    """Return {lead, lead_why, summary, watch} or None if unavailable.

    None means the report renders without a top block. It never means an item
    is dropped.
    """
    if not items:
        return None

    text = stop = ""
    try:
        text, stop = _complete(SYSTEM + (pref or ""), _payload(items))
        if stop == "max_tokens":
            log.warning("synthesis hit max_tokens — raise MAX_TOKENS")
        data = _extract_json(text)
    except Exception as exc:
        # Log enough to diagnose without another run: why the model stopped
        # and what it actually said.
        last_error = f"{type(exc).__name__}: {exc} [stop={stop}, {len(text)} chars]"
        log.error("synthesis failed: %s | first 400: %s",
                  last_error, text[:400].replace("\n", " "))
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
