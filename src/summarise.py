"""Synthesis pass. One call, sees the whole day at once.

Runs after classification, before rendering. Produces a lead item and a short
synthesis that sit at the top of the report. Adds nothing to the filtering
logic — everything classified still ships below, in full.

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
MAX_TOKENS = 16000       # thinking shares this budget; 10 entries x 2 paras
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
                why  one line, under 25 words: why this earns a place. The
                     full write-up happens in a later step that has the
                     article text; here you are only selecting and ranking.
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

Use the why line as the test. If the only honest thing you can write is a
paraphrase of the headline, the item has not earned a place — drop it. Every
entry must survive the question "what does this change?"

Do not manufacture significance. Most days are quiet. If nothing is
consequential, return an empty top list and let summary say so in one sentence
— "Nothing consequential today; routine market coverage only." is a correct
and useful answer. A monitor that finds a headline every single day teaches
its reader to stop believing it.

You have the headline and nothing else. This is the hard constraint on every
why line you write.

Never invent a figure, date, place or detail that is not in the title. Do not
describe what the article "reports" or "says" — you have not read it. Where
you are reasoning past the headline, the sentence must read as your inference:
"if this confirms X, then Y" rather than "X has happened". A confident
sentence built on a headline you half-understood is worse than a short one,
because the reader cannot tell the difference without opening the link.

If a title is too thin to support two paragraphs of honest analysis, that is
information: the item does not belong in the top. Drop it.

Distinguish announced from enacted. A proposed tax and a passed tax are not
the same event, and the difference is often the whole story.

Return only the JSON object. No prose around it, no markdown fences."""


WRITEUP_SYSTEM = """You write the entries of a daily market monitor.

The reader runs a demand-side platform for cross-border buyers of second homes
and vacation property in Europe — Spain, Portugal, Italy, France, Greece. Her
revenue depends on: buyers being legally able to purchase, her being legally
able to take a share of a listing agent's commission, and no one else owning
the neutral advice layer before the listing.

For each item you receive an id, a headline, and — sometimes — the article
text. Return a JSON array of objects:

  id     unchanged
  body   two short paragraphs, 60-110 words total, separated by a blank line.

         First paragraph: the concrete substance. Figures, dates, regions,
         named parties, what takes effect when. This is the paragraph that
         justifies the whole pipeline, so use the article text hard.

         Second paragraph: what it means for her. Name the part of the
         business it touches — the True-Cost Engine, the Costa del Sol or
         Costa Blanca corridor, the commission split, an archetype in the
         segmentation, the neutrality position — and what she might do.

The rule that governs everything else:

Where article text is provided, every figure and date in your first paragraph
must come from it. Do not round, do not restate from memory, do not fill a
gap with what is usually true.

Where article text is NOT provided, you have the headline alone. Say less.
Write what the headline supports and no more, and let the sentence read as
inference — "if this is a rate change rather than a proposal, then" — instead
of asserting a fact you cannot see. Never write that the article "reports" or
"states" anything when you were not given it.

Distinguish announced from enacted. A proposed tax and a passed tax are not
the same event, and the difference is often the whole story.

Return only the JSON array. No prose around it, no markdown fences."""


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

    by_id = {r["item"].doc_id: r for r in top}
    for entry in data:
        row = by_id.get(entry.get("id"))
        if row and entry.get("body"):
            row["why"] = entry["body"].strip()


def summarise(items) -> dict | None:
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
        top.append({"item": item, "why": (row.get("why") or "").strip(),
                    "sourced": False})

    return {
        "top": top,
        "summary": (data.get("summary") or "").strip(),
        "watch": (data.get("watch") or "").strip(),
    }
