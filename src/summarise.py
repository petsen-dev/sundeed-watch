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
bottom half is filler and stops reading the
