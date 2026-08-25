"""Synthesis pass, in two stages.

Stage 1 (`summarise`) reads every headline of the day and picks the top ten.
Stage 2 (`write_up`) receives those ten with their article text — fetched by
article.py in between — and writes the entry for each.

Splitting them is what makes the article fetch affordable: only the selected
handful get resolved and downloaded, not the whole day.

Model: claude-sonnet-5 for both.
"""

from __future__ import annotations

import json
import logging
import os

from anthropic import Anthropic

log = logging.getLogger("summarise")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000       # thinking shares this budget
MAX_ITEMS = 220          # titles sent for selection; ordering already ranks them

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
at once. A minor item that confirms a
