# Sundeed Watch

Daily market monitor. Four languages in, one English report out, delivered to
Telegram.

```
fetch → dedupe (within language) → translate + categorise + score
      → dedupe (across languages) → rank → Telegram → archive
```

Nothing is filtered out. Items are collapsed when they are the same story and
ordered by score; everything else ships. `state/archive/` keeps the full record
of every run.

## Setup

**1. Telegram bot.** Message `@BotFather`, `/newbot`, keep the token. Send your
bot any message, then read your chat id:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

**2. Repository secrets** — Settings → Secrets and variables → Actions:

| Secret | |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_CHAT_ID` | from `getUpdates` |

**3. First run.** Push to the default branch (schedules only fire from there),
then Actions → `daily-report` → *Run workflow*. Do not wait for the cron on day
one — trigger it manually and confirm the message lands.

## Local

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python src/main.py --dry-run          # prints instead of sending
python src/main.py --no-llm --dry-run # raw ingest, no API cost
```

`--no-llm` is the day-2 mode: run it for a day or two to see the real volume
before spending anything on classification.

## Cost

Haiku 4.5 at $1/$5 per million tokens, 25 items per call. A 300-item day is
roughly 12 calls and lands in single-digit cents. If it ever matters, the
Batch API halves it — this workload is asynchronous by nature.

## Tuning

Everything you will actually adjust lives in `config/keywords.yml`.

Score does not gate delivery, so a wrong score costs you position in the
report, not a missed item. Adjust the scoring guidance in `classify.py`
(`SYSTEM`) rather than adding filters.

Read `state/archive/*.json` after a couple of weeks — that is the real corpus
for deciding whether a query earns its place.

## Known gaps

These are open, not oversights:

- **BOE API path.** Verify against the spec at
  `boe.es/datosabiertos/api/api.php` before trusting it. The fetcher logs a
  failure rather than crashing if the path moved.
- **DOGV and Diário da República** have no documented public feed. Until one is
  wired in, subscribe manually — DOGV free alerts at
  `dogv.gva.es/es/alertas-del-diari-oficial` (pick *legislación* and *planes de
  urbanismo*), DRE keyword alerts by registering at `diariodarepublica.pt`.
  DOGV publishes Valencian and Castilian as separate editions, so keywords are
  needed in both.
- **Arabic feed URLs** in `config/sources.yml` are placeholders. Al Arabiya
  documents its list at `english.alarabiya.net/tools/mrss`; Argaam at
  `argaam.com/en/rss`. Pin the exact URLs.
- **Google News is not a wire.** Roughly 100 items per query with no
  pagination, links are `news.google.com` redirects rather than publisher URLs
  (hence title-based dedup), and the index runs stale — a July 2026 sampling of
  48 queries found a median item age near 6.6 days. The gazette feeds are what
  give you same-day.

## Operational notes

- Cron is UTC and runs late under load, sometimes 15 minutes or more. 06:00 UTC
  is set to land before 08:00 CET, not on the hour.
- The daily `state/` commit doubles as the keepalive. GitHub disables scheduled
  workflows after 60 days without repository activity, and only commits
  reliably reset that clock.
- The status line (`13/14 sources ok · failed: argaam`) is not decoration. An
  empty report and a broken parser look identical without it.
- Telegram caps a message at 4096 characters. Long reports split into numbered
  parts; nothing is dropped.
