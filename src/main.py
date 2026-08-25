"""Entry point. Runs the four stages in order and persists state.

    fetch → dedupe(within-lang) → classify(translate+score) → dedupe(cross-lang)
          → rank → send → archive

Archive holds everything ever ingested, in full, searchable. Nothing is thrown
away at any stage — items are collapsed when identical and ordered by score,
and that is the extent of it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import article            # noqa: E402
import classify           # noqa: E402
import dedupe             # noqa: E402
import feedback           # noqa: E402
import fetch              # noqa: E402
import report             # noqa: E402
import stories            # noqa: E402
import summarise          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
SEEN_PATH = STATE / "seen.json"
FEEDBACK_PATH = STATE / "feedback.json"
STATS_PATH = STATE / "query_stats.json"
STORIES_PATH = STATE / "stories.json"
ARCHIVE_DIR = STATE / "archive"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-9s %(levelname)-7s %(message)s",
)
log = logging.getLogger("main")


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_seen() -> dict:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("seen.json corrupt — starting fresh")
    return {}


def save_seen(seen: dict, new_items) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    for item in new_items:
        seen[item.doc_id] = today.isoformat()

    horizon = today - dt.timedelta(days=dedupe.SEEN_TTL_DAYS)
    pruned = {
        k: v for k, v in seen.items()
        if dt.date.fromisoformat(v) >= horizon
    }
    STATE.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(pruned, indent=0), encoding="utf-8")


def update_query_stats(items) -> None:
    """Accumulate per-query yield and mean score across every run.

    This is the instrument for tuning keywords.yml on evidence rather than
    guesswork. After a couple of weeks, a query with high yield and a low mean
    score is pulling noise; a query with zero yield is dead weight. Nothing
    here affects what gets delivered — it only records.
    """
    path = STATE / "query_stats.json"
    stats = {}
    if path.exists():
        try:
            stats = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("query_stats.json corrupt — starting fresh")

    for item in items:
        key = item.query or f"[feed] {item.source_id}"
        row = stats.setdefault(key, {"runs": 0, "items": 0, "score_sum": 0})
        row["items"] += 1
        row["score_sum"] += item.score

    seen_keys = {i.query or f"[feed] {i.source_id}" for i in items}
    for key in seen_keys:
        stats[key]["runs"] += 1

    for row in stats.values():
        row["mean_score"] = round(row["score_sum"] / row["items"], 1) if row["items"] else 0

    STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")


def archive(items, ingested: int, status: str) -> pathlib.Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = ARCHIVE_DIR / f"{stamp}.json"
    payload = {
        "date": stamp,
        "ingested": ingested,
        "status": status,
        "items": [i.as_dict() for i in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report instead of sending it")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip classification (day-2 mode: raw ingest only)")
    ap.add_argument("--no-summary", action="store_true",
                    help="classify and rank, but skip the synthesis pass")
    args = ap.parse_args()

    config = load_yaml(ROOT / "config" / "sources.yml")
    keywords = load_yaml(ROOT / "config" / "keywords.yml")

    # 0 — drain any votes queued since the last run. Telegram holds
    #     callback_query updates for 24h, which is exactly one cycle.
    fb = feedback.load(FEEDBACK_PATH)
    if not args.dry_run:
        feedback.drain(fb)
    pref = feedback.profile(fb)

    # 1 — ingest
    result = fetch.fetch_all(config, keywords)
    ingested = len(result.items)
    log.info("ingested %d raw items · %s", ingested, result.status_line)

    # 2 — collapse duplicates inside each language, then drop anything already
    #     delivered on a previous day
    # Tier is needed at selection time, so it is stamped before anything
    # else touches the items.
    for item in result.items:
        item.tier = fetch.authority(item.publisher, config)

    items = dedupe.within_language(result.items)
    seen = load_seen()
    items = dedupe.drop_seen(items, seen)
    log.info("%d after within-language dedupe and seen-check", len(items))

    # 3 — translate, categorise, score
    if not args.no_llm:
        items = classify.enrich(items)
        # 4 — collapse the same story across languages
        items = dedupe.across_languages(items)
        log.info("%d after cross-language dedupe", len(items))
    else:
        for item in items:
            item.title_en = item.title
            item.category = "OTHER"

    items.sort(key=lambda i: i.score, reverse=True)

    # 5 — synthesis over the whole day. Purely additive: it writes the block at
    #     the top and cannot remove anything from the list below.
    digest = None
    notes: list[str] = []
    if not args.no_llm and not args.no_summary:
        story_reg = stories.load(STORIES_PATH)
        digest = summarise.summarise(items, pref,
                                     stories.prompt_block(story_reg))
        if digest is None:
            log.warning("no summary this run — report renders without it")
            notes.append(f"synthesis failed — {summarise.last_error}")
        elif digest.get("top"):
            # One slot per story. The model is told this, but a rule that
            # protects one-off items from a busy week should not depend on
            # the model remembering it.
            claimed: set[str] = set()
            kept = []
            for row in digest["top"]:
                resolved = stories.resolve(row.get("story_raw", ""), story_reg)
                if resolved is None:
                    kept.append(row)
                    continue
                key, label = resolved
                if key in claimed:
                    log.info("dropping second item for story %s", key)
                    continue
                claimed.add(key)
                meta = stories.record(story_reg, key, label,
                                      row["item"].title_en or row["item"].title)
                row["story_key"] = key
                row["story_note"] = stories.continuation_note(meta)
                kept.append(row)
            digest["top"] = kept
            stories.save(STORIES_PATH, story_reg)
            # Only the selected handful get resolved and fetched. Doing this
            # for the whole day would trip Google's rate limiting and take
            # ten minutes for text nobody reads.
            texts = article.fetch_for(digest["top"])
            for row in digest["top"]:
                row["sourced"] = row["item"].doc_id in texts
            for row in digest["top"]:
                row.setdefault("written", False)
            if not summarise.write_up(digest["top"], texts):
                notes.append(f"write-up failed — {summarise.last_error}")

    detail = result.failure_detail
    if notes:
        detail = "\n".join(notes + ([detail] if detail else []))
    text = report.render(items, result.status_line, ingested, digest, detail)
    path = archive(items, ingested, result.status_line)
    update_query_stats(items)
    log.info("archived → %s", path)

    if args.dry_run:
        if digest and digest.get("top"):
            for rank, row in enumerate(digest["top"], start=1):
                print(report.render_entry(rank, row))
                print()
        print(text)
    else:
        if digest and digest.get("top"):
            report.send_digest(text, digest["top"])
            feedback.register_sent(fb, digest["top"])
        else:
            report.send(text)
        save_seen(seen, items)
        feedback.save(FEEDBACK_PATH, fb)
        feedback.attach_to_stats(fb, STATS_PATH)

    # Fail the workflow run if every source died — that is a broken monitor,
    # not a quiet day, and it should page you.
    return 1 if result.ok == [] else 0


if __name__ == "__main__":
    sys.exit(main())
