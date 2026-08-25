"""Feedback: collect votes from Telegram, turn them into a preference profile.

There is no server here. Telegram queues callback_query updates for up to 24
hours, so the next scheduled run drains them with getUpdates. A tap is
recorded reliably; the confirmation just arrives late.

What the profile does and does not do:

  It nudges SELECTION — which of the day's items reach the top, and in what
  order. It never touches retrieval. Queries keep running, everything keeps
  landing in the archive, and nothing disappears because it was once
  disliked.

  That restraint is the point. A monitor exists to catch what you did not
  know to look for. Tune the intake on a handful of votes and it converges on
  what you already believe within a fortnight, which is the one failure mode
  that cannot be detected from inside the reports.

Votes are attached to the query that retrieved the item, so query_stats.json
gains a signal you can act on by hand: a query with eight items and five
dislikes is a query to rewrite.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib

import requests

log = logging.getLogger("feedback")

API = "https://api.telegram.org/bot{token}/{method}"
SENT_TTL_DAYS = 30
PROFILE_MIN_VOTES = 1     # two known readers: every vote is deliberate signal


def _call(method: str, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    resp = requests.post(API.format(token=token, method=method),
                         json=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("feedback.json corrupt — starting fresh")
    return {"offset": 0, "sent": {}, "votes": {}}


def save(path: pathlib.Path, data: dict) -> None:
    today = dt.date.today()
    horizon = today - dt.timedelta(days=SENT_TTL_DAYS)
    data["sent"] = {
        k: v for k, v in data.get("sent", {}).items()
        if v.get("date", "1970-01-01") >= horizon.isoformat()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def register_sent(data: dict, top: list) -> None:
    """Record what each delivered item was, so a vote arriving tomorrow can
    still be attributed to a category and a query."""
    today = dt.date.today().isoformat()
    for row in top:
        item = row["item"]
        data.setdefault("sent", {})[item.doc_id] = {
            "title": (item.title_en or item.title)[:180],
            "cat": item.category or "OTHER",
            "query": item.query or f"[feed] {item.source_id}",
            "lang": item.lang,
            "date": today,
        }


def drain(data: dict) -> int:
    """Pull queued votes. Returns how many new ones were recorded."""
    try:
        resp = _call("getUpdates",
                     offset=data.get("offset", 0),
                     timeout=0,
                     allowed_updates=["callback_query"])
    except Exception as exc:
        log.error("getUpdates failed: %s", exc)
        return 0

    if not resp.get("ok"):
        log.error("getUpdates not ok: %s", resp.get("description"))
        return 0

    new = 0
    for update in resp.get("result", []):
        data["offset"] = update["update_id"] + 1
        cq = update.get("callback_query")
        if not cq:
            continue
        payload = cq.get("data", "")
        if ":" not in payload:
            continue
        verdict, doc_id = payload.split(":", 1)
        if verdict not in ("up", "down"):
            continue

        voter = str(cq.get("from", {}).get("id", "?"))
        name = cq.get("from", {}).get("first_name", "")
        data.setdefault("voters", {})[voter] = name
        # Keyed by voter, not just by item: with two readers, the second tap
        # would otherwise overwrite the first and their disagreement would
        # vanish into a single sign.
        data.setdefault("votes", {}).setdefault(doc_id, {})[voter] = (
            1 if verdict == "up" else -1
        )
        new += 1

        # Late, but it clears the spinner on the user's client.
        try:
            _call("answerCallbackQuery", callback_query_id=cq["id"],
                  text="Recorded" if verdict == "up" else "Noted")
        except Exception:
            pass

    if new:
        log.info("recorded %d vote(s)", new)
    return new


def _tally(data: dict) -> dict:
    """{doc_id: (net, n_voters)}. Tolerates the old flat {doc_id: int} shape."""
    out = {}
    for doc_id, val in data.get("votes", {}).items():
        if isinstance(val, dict):
            vals = list(val.values())
        else:
            vals = [val]
        out[doc_id] = (sum(vals), len(vals))
    return out


def attach_to_stats(data: dict, stats_path: pathlib.Path) -> None:
    """Fold votes into query_stats.json so a query's record shows its votes."""
    if not stats_path.exists():
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    # Recount from scratch each run — votes can change, and incrementing
    # would double-count an item voted on twice.
    for row in stats.values():
        row["up"] = row["down"] = row["split"] = 0
    for doc_id, (net, _n) in _tally(data).items():
        meta = data.get("sent", {}).get(doc_id)
        if not meta or meta["query"] not in stats:
            continue
        key = "up" if net > 0 else "down" if net < 0 else "split"
        stats[meta["query"]][key] += 1

    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def profile(data: dict) -> str:
    """A compact preference note for the selection prompt.

    Written for a readership of two. Every vote is a deliberate judgement from
    someone who knows the business, so there is no minimum sample to wait for.
    What does need care is disagreement: when the two readers split, that is
    not a weak signal to average away, it is a strong signal that the item was
    worth arguing about. Contested items are shown to the model as contested.
    """
    tally = _tally(data)
    sent = data.get("sent", {})
    if len(tally) < PROFILE_MIN_VOTES:
        return ""

    liked, disliked, split = [], [], []
    cat_score: dict[str, int] = {}
    for doc_id, (net, _n) in tally.items():
        meta = sent.get(doc_id)
        if not meta:
            continue
        if net > 0:
            liked.append(meta["title"])
            cat_score[meta["cat"]] = cat_score.get(meta["cat"], 0) + 1
        elif net < 0:
            disliked.append(meta["title"])
            cat_score[meta["cat"]] = cat_score.get(meta["cat"], 0) - 1
        else:
            split.append(meta["title"])

    if not (liked or disliked or split):
        return ""

    up_cats = sorted([c for c, v in cat_score.items() if v > 0],
                     key=lambda c: -cat_score[c])
    down_cats = sorted([c for c, v in cat_score.items() if v < 0],
                       key=lambda c: cat_score[c])

    parts = ["", "READER FEEDBACK", "",
             "This monitor has two readers. Both know the business, so each "
             "vote is a considered judgement rather than a crowd signal — "
             "weight a single vote accordingly.", ""]
    if up_cats:
        parts.append(f"Categories marked useful: {', '.join(up_cats)}")
    if down_cats:
        parts.append(f"Categories marked not useful: {', '.join(down_cats)}")
    if liked:
        parts += ["", "Marked useful:"] + [f"  + {t}" for t in liked[-10:]]
    if disliked:
        parts += ["", "Marked not useful:"] + [f"  - {t}" for t in disliked[-10:]]
    if split:
        parts += ["", "The two readers disagreed on these:"]
        parts += [f"  ? {t}" for t in split[-6:]]
        parts.append("A split is not a weak signal. It means the item was "
                     "arguable, which usually means it was worth running. "
                     "Treat these as closer to useful than to not.")
    parts += [
        "",
        "Weight this, but do not obey it. Two things override it entirely: "
        "anything that changes what a buyer may do or pay, and anything a "
        "competitor just did in her territory — surface those even from a "
        "downvoted category. A monitor that only returns what its readers "
        "already like stops being a monitor, and neither of them can detect "
        "that from inside the reports.",
    ]
    log.info("profile: %d item(s) voted (%d up, %d down, %d split)",
             len(tally), len(liked), len(disliked), len(split))
    return "\n".join(parts)
