"""Story registry — continuity across days, not just within one.

Deduplication collapses the same article. This collapses the same *story*:
Greece publishes a housing strategy on the 17th, an analysis lands on the
24th, thresholds follow in September. Different articles, different headlines,
one line of development — and for the announced-versus-enacted question the
line is the whole point. A bill that generated eighteen months of headlines
and was never debated only looks like that over time.

Two rules keep this from quietly becoming a ranking system:

  One slot per story per day. A running story cannot take three places in a
  ten-item digest; the rest become a "more on this" pointer. Without the cap
  a hot week crowds out exactly the one-off items a monitor exists to catch.

  A story history is context, never a score. An item with no past is not
  thereby less important, and a fifth instalment gets no free pass.

Only stories touched in the last 30 days go into the prompt, so the block
stays a fixed couple of thousand tokens no matter how large the archive gets.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re

log = logging.getLogger("stories")

OPEN_DAYS = 30        # a story untouched this long drops out of the prompt
MAX_IN_PROMPT = 60    # newest first; the tail is rarely what today matches
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


def load(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("stories.json corrupt — starting fresh")
    return {}


def save(path: pathlib.Path, stories: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stories, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def _open_stories(stories: dict) -> list[tuple[str, dict]]:
    horizon = (dt.date.today() - dt.timedelta(days=OPEN_DAYS)).isoformat()
    live = [(k, v) for k, v in stories.items()
            if v.get("last_seen", "1970-01-01") >= horizon]
    live.sort(key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
    return live[:MAX_IN_PROMPT]


def prompt_block(stories: dict) -> str:
    """The open-stories list, appended to the selection prompt."""
    live = _open_stories(stories)
    if not live:
        return ""
    lines = ["", "OPEN STORIES", "",
             "Storylines already running. Assign an item to one when it "
             "continues that line; otherwise open a new one.", ""]
    for key, meta in live:
        lines.append(
            f"  {key} — {meta.get('label', '')} "
            f"(since {meta.get('first_seen', '?')}, "
            f"{meta.get('count', 0)} appearances)"
        )
    lines += [
        "",
        "Two things this list does NOT mean.",
        "",
        "It is not a ranking input. An item belonging to a long-running story "
        "is not thereby more important, and an item with no history is not "
        "thereby less. Judge every item on what it changes, exactly as if this "
        "list were absent.",
        "",
        "You may place at most ONE item per story in the top. Where several of "
        "today's items continue the same story, pick the one that carries the "
        "development and leave the others out — they are recorded and pointed "
        "to. The slots this frees are for one-off news, which is what a monitor "
        "is for.",
    ]
    return "\n".join(lines)


def resolve(raw: str, stories: dict) -> tuple[str, str] | None:
    """Turn the model's story field into (key, label). None if unusable.

    Accepts an existing key, or `NEW:slug | Label` for a new line.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    if raw.upper().startswith("NEW:"):
        body = raw[4:].strip()
        slug, _, label = body.partition("|")
        slug = slug.strip().lower()
        label = label.strip() or slug.replace("-", " ").title()
        if not SLUG.match(slug):
            log.warning("bad story slug %r — ignored", slug)
            return None
        return slug, label

    if raw in stories:
        return raw, stories[raw].get("label", raw)

    log.warning("unknown story id %r — ignored", raw)
    return None


def record(stories: dict, key: str, label: str, headline: str) -> dict:
    """Register one appearance. Returns the story's state after updating."""
    today = dt.date.today().isoformat()
    meta = stories.setdefault(key, {
        "label": label,
        "first_seen": today,
        "count": 0,
    })
    meta["label"] = meta.get("label") or label
    meta["last_seen"] = today
    meta["count"] = meta.get("count", 0) + 1
    meta["last_headline"] = headline[:180]
    return meta


def continuation_note(meta: dict) -> str:
    """The line under a headline, or "" for the first appearance.

    Counts digest appearances, not articles seen — that is what the registry
    actually knows, and claiming otherwise would be a small lie printed daily.
    """
    count = meta.get("count", 1)
    if count <= 1:
        return ""
    first = meta.get("first_seen", "")
    try:
        pretty = dt.date.fromisoformat(first).strftime("%-d %b")
    except ValueError:
        pretty = first
    return f"{count}th appearance in this digest since {pretty}".replace(
        "2th", "2nd").replace("3th", "3rd").replace("1th", "1st")
