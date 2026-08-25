"""Render the English report and push it to Telegram.

Only the digest ships: the ranked top, the synthesis, and a count of what else
was screened. The full corpus stays in state/archive/ and is never discarded —
this file decides what is shown, not what is kept.
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


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def render(items, status_line: str, ingested: int, digest: dict | None = None) -> str:
    today = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")
    lines = [f"<b>{today} · Sundeed Watch</b>", ""]

    if digest:
        top = digest.get("top") or []
        if top:
            lines.append(f"<b>TOP {len(top)}</b>")
            # Every entry rendered identically — the tenth gets the same
            # treatment as the first, or the ranking implies a depth of
            # attention the content does not have.
            for rank, row in enumerate(top, start=1):
                item = row["item"]
                title = _esc(item.title_en or item.title)
                lines.append(
                    f'{rank}. <b><a href="{_esc(item.url)}">{title}</a></b>'
                )
                if row["why"]:
                    lines.append(_esc(row["why"]))
                lines.append("")
        if digest.get("summary"):
            lines.append(_esc(digest["summary"]))
            lines.append("")
        if digest.get("watch"):
            lines.append(f"<i>Watch: {_esc(digest['watch'])}</i>")
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
                f"state/archive/{stamp}.json</
