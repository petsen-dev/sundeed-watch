"""Render the English report and push it to Telegram.

Nothing is truncated for length. Telegram caps a single message at 4096
characters, so a long report is split across numbered parts — the only limit
in this pipeline that is not ours.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import os
import time

import requests

log = logging.getLogger("report")

TG_LIMIT = 4096
SAFE_LIMIT = 3900          # headroom for the "1/4" part marker
API = "https://api.telegram.org/bot{token}/sendMessage"

# Report order. Regulatory first regardless of score: a tax change outranks a
# funding round even when the model disagrees.
STREAM_ORDER = ["REGULATORY", "HEADLINE-GAP", "DEMAND-FLOW", "COMMISSION-MODEL",
                "OWNERSHIP-MODEL", "UPFUNNEL", "CHANNEL", "OTHER"]

HEADING = {
    "REGULATORY": "REGULATORY",
    "HEADLINE-GAP": "ANNOUNCED vs ENACTED",
    "DEMAND-FLOW": "DEMAND FLOW",
    "COMMISSION-MODEL": "COMMISSION MODEL",
    "OWNERSHIP-MODEL": "OWNERSHIP MODELS",
    "UPFUNNEL": "UP-FUNNEL COMPETITION",
    "CHANNEL": "CHANNEL",
    "OTHER": "OTHER",
}

FLAG = {"en": "EN", "ar": "AR", "es": "ES", "pt": "PT", "it": "IT", "fr": "FR"}


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def render(items, status_line: str, ingested: int) -> str:
    today = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")
    lines = [f"<b>{today} · Sundeed Watch</b>", ""]

    buckets: dict[str, list] = {}
    for item in items:
        buckets.setdefault(item.category or "OTHER", []).append(item)

    for cat in STREAM_ORDER:
        group = buckets.get(cat)
        if not group:
            continue
        group.sort(key=lambda i: i.score, reverse=True)
        lines.append(f"<b>{HEADING[cat]}</b> — {len(group)}")
        for item in group:
            lang = FLAG.get(item.lang, item.lang.upper())
            title = _esc(item.title_en or item.title)
            lines.append(f'• <a href="{_esc(item.url)}">{title}</a>')
            meta = f"  {lang} · {item.score}"
            if item.rationale:
                meta += f" · {_esc(item.rationale)}"
            lines.append(meta)
            if item.lang != "en" and item.title_en:
                lines.append(f"  <i>orig:</i> {_esc(item.title[:110])}")
            dupes = getattr(item, "duplicates", None)
            if dupes:
                lines.append(f"  <i>also in {len(dupes)} other source(s)</i>")
        lines.append("")

    if not items:
        lines.append("<i>No new items.</i>")
        lines.append("")

    lines.append(f"<i>ingested {ingested} · delivered {len(items)} · {_esc(status_line)}</i>")
    return "\n".join(lines)


def _split(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split on blank lines, then lines, never mid-entry if avoidable."""
    if len(text) <= limit:
        return [text]

    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        if len(block) <= limit:
            current = block
        else:
            # A single oversized block: fall back to line-level splitting.
            current = ""
            for line in block.split("\n"):
                cand = f"{current}\n{line}" if current else line
                if len(cand) <= limit:
                    current = cand
                else:
                    parts.append(current)
                    current = line[:limit]
    if current:
        parts.append(current)
    return parts


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    parts = _split(text)
    total = len(parts)
    url = API.format(token=token)

    for idx, part in enumerate(parts, start=1):
        body = part if total == 1 else f"{part}\n\n<i>{idx}/{total}</i>"
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 3)
            time.sleep(wait + 1)
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
        resp.raise_for_status()
        # Telegram allows roughly one message per second to a single chat.
        if idx < total:
            time.sleep(1.2)
    log.info("sent %d part(s)", total)
