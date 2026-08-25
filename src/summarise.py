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
MAX_ITEMS = 220          # titles sent for synthesis; ordering already ranks them

SYSTEM = """You write the opening block of a daily market monitor.

The reader runs a demand-side platform for cross-border buyers of second homes
and vacation property in Europe — Spain, Portugal, Italy, France, Greece. Her
revenue depends on: buyers being legally able to purchase, her being legally
able to take a share of a listing agent's commission, and no one else owning
the neutral advice layer before the listing. Judge everything against that.

You receive the day's items, already categorised and ranked. Return one JSON
object:

  lead_id     the id of the single most consequential item, or null
  lead_why    one sentence, under 25 words: why that item matters to her
  summary     2-4 sentences synthesising the day. Plain declarative prose.
              Name specifics — countries, companies, numbers. No hedging
              phrases, no "several developments", no throat-clearing.
  watch       optional, under 15 words: one thing worth checking tomorrow.
              Omit unless something is genuinely unresolved.

Rules that matter more than fluency:

Do not manufacture significance. Most days are quiet. If nothing is
consequential, set lead_id to null and let summary say so in one sentence —
"Nothing consequential today; routine market coverage only." is a correct and
useful answer. A monitor that finds a headline every single day teaches its
reader to stop believing it.

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
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


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
            max_tokens=1000,
            system=SYSTEM,
            messages=[{"role": "user", "content": _payload(items)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = _extract_json(text)
    except Exception as exc:
        log.error("synthesis failed: %s", exc)
        return None

    lead = None
    lead_id = data.get("lead_id")
    if lead_id:
        lead = next((i for i in items if i.doc_id == lead_id), None)
        if lead is None:
            log.warning("lead_id %s not in item set — dropping lead", lead_id)

    return {
        "lead": lead,
        "lead_why": (data.get("lead_why") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
        "watch": (data.get("watch") or "").strip(),
    }
