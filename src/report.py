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

from prompts import CATEGORY_TAG

log = logging.getLogger("report")

TG_LIMIT = 4096
SAFE_LIMIT = 3900          # headroom for the "1/4" part marker
API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _tags(row) -> tuple[str, str]:
    """(emoji, hashtag line) for one entry."""
    emoji, cat_tag = CATEGORY_TAG.get(row["item"].category or "OTHER",
                                      ("", "other"))
    tags = [cat_tag] + list(row.get("geo") or [])
    return emoji, " ".join(f"#{t}" for t in tags)


def render_entry(rank: int, row) -> str:
    """One top item as its own message — buttons attach to messages, not lines."""
    item = row["item"]
    title = _esc(item.title_en or item.title)
    link = getattr(item, "resolved_url", "") or item.url
    emoji, tagline = _tags(row)
    head = f"{emoji} " if emoji else ""
    lines = [f'{head}{rank}. <b><a href="{_esc(link)}">{title}</a></b>']
    if row.get("sourced") is False:
        lines.append("<i>headline only — article not retrieved</i>")
    for para in (row.get("why") or "").split("\n\n"):
        para = para.strip()
        if para:
            lines.append(_esc(para))
    if tagline:
        # Tags last: tapping one searches the chat, which is the only
        # searchable archive of this monitor a human will actually use.
        lines.append("")
        lines.append(tagline)
    return "\n".join(lines)


def vote_keyboard(doc_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "\U0001F44D", "callback_data": f"up:{doc_id}"},
        {"text": "\U0001F44E", "callback_data": f"down:{doc_id}"},
    ]]}


def render(items, status_line: str, ingested: int, digest: dict | None = None) -> str:
    today = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")
    lines = [f"<b>{today} · Sundeed Watch</b>", ""]

    if digest:
        if digest.get("summary"):
            lines.append(_esc(digest["summary"]))
            lines.append("")
        if digest.get("watch"):
            lines.append(f"<i>Watch: {_esc(digest['watch'])}</i>")
            lines.append("")
        seen_tags: list[str] = []
        for row in digest.get("top") or []:
            for tag in _tags(row)[1].split():
                if tag not in seen_tags:
                    seen_tags.append(tag)
        if seen_tags:
            lines.append(" ".join(seen_tags))
            lines.append("")

    if not items:
        lines.append("<i>No new items.</i>")
        lines.append("")
    else:
        rest = len(items) - len(digest.get("top") or []) if digest else len(items)
        if rest > 0:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            lines.append(
                f"<i>{rest} more item(s) screened — full list in "
                f"state/archive/{stamp}.json</i>"
            )
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


def _post(payload: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = API.format(token=token)
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code == 429:
        wait = resp.json().get("parameters", {}).get("retry_after", 3)
        time.sleep(wait + 1)
        resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


def send_digest(header: str, top: list) -> None:
    """Header first, then one message per top item carrying its vote buttons."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    base = {"chat_id": chat_id, "parse_mode": "HTML",
            "disable_web_page_preview": True}

    for rank, row in enumerate(top, start=1):
        body = render_entry(rank, row)
        for part in _split(body):
            payload = dict(base, text=part)
            # Keyboard goes on the last part only, so a long entry does not
            # sprout two sets of buttons for the same item.
            if part is _split(body)[-1]:
                payload["reply_markup"] = vote_keyboard(row["item"].doc_id)
            _post(payload)
        time.sleep(1.2)

    for part in _split(header):
        _post(dict(base, text=part))
        time.sleep(1.2)
    log.info("sent %d entr(ies) + header", len(top))


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
