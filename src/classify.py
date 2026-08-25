"""One LLM pass: translate title to English, assign a category, score.

Score orders the report. It does not gate it — everything fetched is delivered,
highest score first. That is a deliberate design decision, not an oversight.

Model: claude-haiku-4-5-20251001 ($1/$5 per Mtok). Anthropic positions the
Haiku tier for extraction, classification and routing, which is exactly this
job. Items are batched so a 300-item day is roughly a dozen calls.
"""

from __future__ import annotations

import json
import logging
import os
import time

from anthropic import Anthropic

log = logging.getLogger("classify")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 25
MAX_RETRIES = 3

CATEGORIES = [
    "REGULATORY",
    "OWNERSHIP-MODEL",
    "UPFUNNEL",
    "DEMAND-FLOW",
    "COMMISSION-MODEL",
    "CHANNEL",
    "HEADLINE-GAP",
    "OTHER",
]

SYSTEM = """You process headlines for a market monitor covering cross-border \
buyers of second homes and vacation property in Europe (Spain, Portugal, Italy, \
France, Greece), and the businesses serving them.

For each numbered input you return exactly one object with:
  id          the input's id, unchanged
  title_en    the headline in English. If already English, copy it verbatim.
              Translate faithfully; do not summarise, editorialise or expand.
  category    one of: REGULATORY, OWNERSHIP-MODEL, UPFUNNEL, DEMAND-FLOW,
              COMMISSION-MODEL, CHANNEL, HEADLINE-GAP, OTHER
  score       0-100, how much this matters to that monitor
  rationale   at most 12 words, English, why it scores that way

Category meanings:
  REGULATORY        taxes, licences, rental rules, foreign-ownership restrictions
  OWNERSHIP-MODEL   fractional, co-ownership, swaps, rent-to-own, timeshare
  UPFUNNEL          anyone moving into rules-and-costs advice before the listing
  DEMAND-FLOW       cross-border buyer volumes, corridors, origin-market data
  COMMISSION-MODEL  commission splits, buyer agency, referral fees
  CHANNEL           AI assistants, search, distribution to buyers
  HEADLINE-GAP      a measure announced but not enacted, stalled or reversed
  OTHER             anything that fits none of the above

Scoring guidance. Score on consequence for a cross-border second-home buyer or
for a business serving them, not on how dramatic the headline sounds. Anchor
against these:

  95  Andalusia's agent register opens for applications
      (she cannot take commission in her launch corridor without it)
  90  Valencia cuts transfer tax from 10% to 9%
      (changes what every buyer in the corridor pays)
  85  Malaga freezes new tourist-rental licences
      (kills the rental-income case for a whole micro-market)
  70  idealista launches a buyer-side cost calculator
      (a named competitor entering her exact territory)
  60  Pacaso adds week-swapping between co-owners
      (competing answer to the same buyer problem, no immediate impact)
  45  Portugal's INE reports Q2 foreign-buyer medians
      (useful input, changes no decision today)
  25  General "Spanish coastal property remains popular" coverage
  10  Dubai off-plan tower launch
      (property, Arabic, and entirely outside the corridor)
   0  Unrelated to property or to cross-border buying

Two distinctions that move a score more than anything else:

  ANNOUNCED vs ENACTED. A proposed 100% tax on non-EU buyers that was never
  debated in parliament scores far below a 1-point transfer-tax cut that took
  effect. Tag the former HEADLINE-GAP and score it on the noise it creates,
  not on the policy it describes.

  CORRIDOR vs ELSEWHERE. Spain, Portugal, Italy, France and Greece are the
  corridor. Gulf, Egyptian, US-domestic and UK-domestic property markets are
  not — unless the story is about buyers moving between them and the corridor.

Assign OTHER and a low score freely — that is how ranking works. It does not
remove the item.

Return a JSON array and nothing else. No prose, no markdown fences."""


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


def _payload(batch) -> str:
    lines = []
    for idx, item in enumerate(batch):
        lines.append(
            json.dumps(
                {"id": idx, "lang": item.lang, "title": item.title[:300]},
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in response")
    return json.loads(text[start : end + 1])


def _run_batch(client, batch) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user", "content": _payload(batch)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            for row in _extract_json(text):
                i = int(row["id"])
                if not 0 <= i < len(batch):
                    continue
                item = batch[i]
                item.title_en = (row.get("title_en") or item.title).strip()
                cat = (row.get("category") or "OTHER").strip().upper()
                item.category = cat if cat in CATEGORIES else "OTHER"
                item.score = max(0, min(100, int(row.get("score", 0))))
                item.rationale = (row.get("rationale") or "").strip()
            return
        except Exception as exc:
            log.warning("batch attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                # Degrade, never drop. An unclassified item still ships — it
                # goes to the bottom of the report flagged as unprocessed.
                for item in batch:
                    if not item.title_en:
                        item.title_en = item.title
                        item.category = "OTHER"
                        item.score = 0
                        item.rationale = "not classified (API error)"
                return
            time.sleep(2 ** attempt)


def enrich(items) -> list:
    if not items:
        return items
    client = _client()
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        _run_batch(client, batch)
        log.info("classified %d/%d", min(start + BATCH_SIZE, len(items)), len(items))
    return items
