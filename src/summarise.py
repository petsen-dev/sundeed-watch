"""Synthesis pass. One call, sees the whole day at once.

Runs after classification, before rendering. Produces the ranked top and the
summary paragraph that open the report.

Model: claude-sonnet-5. One call per day over a few hundred short titles is
fractions of a cent, and picking out what actually matters is a judgement call
the Haiku tier does noticeably worse.
"""

from __future__ import annotations

import json
import logging
import os

from anthropic import Anthropic

log = logging.getLogger("summarise")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000        # thinking shares this budget — 1000 truncates the JSON
MAX_ITEMS = 220          # titles sent for synthesis; ordering already ranks them

SYSTEM = """You write the opening block of a daily market monitor.

The reader runs a demand-side platform for cross-border buyers of second homes
and vacation property in Europe — Spain, Portugal, Italy, France, Greece. Her
revenue depends on: buyers being legally able to purchase, her being legally
able to take a share of a listing agent's commission, and no one else owning
the neutral advice layer before the listing. Judge everything against that.

You receive the day's items, already categorised and machine-scored. Return
one JSON object:

  top         ordered list of up to 10 objects, most consequential first:
                id   the item's id, unchanged
                why  the reason it earns its place. Under 25 words for the
                     first entry, under 10 words for the rest. Say what
                     changes, not what the headline says — the headline is
                     already shown next to it.
  summary     2-4 sentences synthesising the day. Plain declarative prose.
              Name specifics — countries, companies, numbers. No hedging
              phrases, no "several developments", no throat-clearing.
  watch       optional, under 15 words: one thing worth checking tomorrow.
              Omit unless something is genuinely unresolved.

Rules that matter more than fluency:

Rank by consequence, not by the score you were given. The scores are per-item
and were assigned without sight of the rest of the day; you can see everything
at once. A minor item that confirms a pattern across three other items may
outrank a higher-scored isolated one. Do not simply copy the top ten by score.

Ten is a ceiling, not a quota. Return four entries if only four earn a place,
and an empty list if none do. Padding the list to ten with routine coverage is
the single fastest way to make this block worthless — the reader learns the
bottom half is filler and stops reading the top half with it.

Do not manufacture significance. Most days are quiet. If nothing is
consequential, return an empty top list and let summary say so in one sentence
— "Nothing consequential today; routine market coverage only." is a correct
and useful answer. A monitor that finds a headline every single day teaches
its reader to stop believing it.

Never assert anything not present in the titles you were given. You are
summarising headlines, not the underlying articles — if a title is ambiguous,
describe it as it reads rather than inferring what the article probably says.

Distinguish announced from enacted. A proposed tax and a passed tax are not
the same event, and the difference is often the whole story.

Return only the JSON object. No prose around it, no markdown fences."""


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


def summarise(items) -> dict | None:
    """Return {top, summary, watch} or None if unavailable.

    None means the report renders without a top block. It never means an item
    is dropped.
    """
    if not items:
        return None

    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
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
        top.append({"item": item, "why": (row.get("why") or "").strip()})

    return {
        "top": top,
        "summary": (data.get("summary") or "").strip(),
        "watch": (data.get("watch") or "").strip(),
    }
