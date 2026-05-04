# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this bot does

A Telegram bot that monitors Google Play app availability. It stores app links in MongoDB, checks their status every 20 minutes, detects metadata changes (name, version, developer, screenshots, etc.), and notifies subscribed chats. On status changes, it optionally syncs to Trello (moving cards between lists) and a CRM MongoDB database.

## Running the bot

```bash
# Install dependencies (no virtualenv required)
python3 -m pip install -r requirements.txt

# Copy and fill in env vars
cp .env.example .env

# Run
python3 main.py
```

## Testing checker logic (no bot needed)

```bash
python3 test_checker.py
```

Edit the `test_links` list in `test_checker.py` to test specific packages.

## Required environment variables

- `BOT_TOKEN` — Telegram bot token
- `MONGO_URI` — MongoDB connection string

All other vars have defaults in `.env.example`. Key optional ones:
- `CHECK_INTERVAL_MINUTES` (default 20) — periodic check interval
- `CHECK_VERBOSE_LOGS` — set to `1` to enable per-step timing logs in checker
- `PARSE_APP_METHOD` — controls how the app ID is resolved for CRM/Trello headers: `0`=none, `1`=Trello only, `2`=`trello_app_id` from DB, `3`=Trello preferred, `4`=manual preferred

Trello integration (all optional, Trello sync disabled if any is missing):
- `TRELLO_API_KEY`, `TRELLO_API_SECRET`, `TRELLO_TOKEN`
- `TRELLO_PROCESSING_BOARD_ID`, `TRELLO_BANNED_LIST_ID`, `TRELLO_IN_MARKET_LIST_ID`

CRM integration (optional, disabled if missing):
- `CRM_DB_MONGO_URI`, `CRM_DB_NAME`

## Architecture

### Module overview

| File | Role |
|------|------|
| `main.py` | Telegram bot: command handlers, job scheduler, notification broadcasting |
| `checker.py` | HTTP scraper: fetches Google Play HTML, parses metadata, determines availability |
| `db.py` | All MongoDB operations: app CRUD, change detection, chat subscriptions |
| `crm_sync.py` | Optional: syncs status changes to an external CRM MongoDB database |
| `utils/trello_api.py` | Optional: wraps `py-trello` to find cards by bundle ID and move them between lists |
| `config.py` | Reads all env vars with defaults |
| `logger/__init__.py` | Sets up console + file (`bot.log`) logging |

### Check flow

1. **Periodic job** (`job_check_all_apps`) fires every `CHECK_INTERVAL_MINUTES`. It skips apps banned for more than 30 days unless `force_all=True`.
2. `run_check_all_apps` iterates apps sequentially with 2-second delays between requests.
3. For each app, `fetch_app_metadata` (in `checker.py`) makes an HTTP GET. HTTP 400+ or explicit "not found" text in the body → unavailable. HTTP 200 without not-found indicators → available. It also parses metadata from the HTML.
4. `db.update_after_metadata_check` compares new metadata against stored values and builds a `changes` list. Status transitions: `unknown/unavailable → available` triggers `moderation_passed` (first time) or `renewed`; `available → unavailable` triggers `banned`.
5. On `banned`/`renewed`/`moderation_passed`: Trello card is moved (sync runs in `asyncio.to_thread` since `py-trello` is sync), then CRM is updated via `crm_sync.sync_app_status_to_crm`.
6. Change events are broadcast to all subscribed chats (one message per app).

### App ID resolution (`PARSE_APP_METHOD`)

The "app ID" shown in notification headers and synced to CRM can come from two sources:
- **Trello**: extracted from card name pattern `[id] bundle_id name` on the processing board
- **Manual** (`trello_app_id` field in DB): set via `/add <pkg> <id>` or updated on duplicate `/add`

`_resolve_app_id_for_doc` in `main.py` applies the method from `PARSE_APP_METHOD`.

### Database collections

**`apps`** — one doc per package (unique index on `package`):
- Core: `package`, `link` (may include `hl`/`gl` locale params), `status`, `banned_at`, `renew_at`, `last_checked_at`, `created_at`
- Metadata: `name`, `version`, `developer`, `icon`, `short_desc`, `long_desc`, `updated_on_text`, `screenshots`
- Change timestamps: `last_name_change_at`, `last_version_change_at`, etc.
- History arrays: `name_history`, `version_history`, `developer_history`, `icon_history`, `screenshots_history`, `desc_history`, `updated_on_history`
- `trello_app_id` — manual CRM/header ID
- `first_time_added` — flag for detecting first-time moderation pass

**`chats`** — Telegram chats: `chat_id`, `subscribed`, `created_at`, `updated_at`

### Link normalization

Apps can be added by full Play URL or package name. `db.normalize_play_link` preserves `hl`/`gl` locale params in stored links (so the same locale is used for future fetches, enabling consistent metadata comparison). The unique key is always `package`.

### Checker HTML parsing

`checker.py` reads only the first ~1.6 MB of HTML for metadata (to avoid catastrophic regex backtracking on multi-MB pages). Screenshots require the full HTML because they appear later. `_extract_screenshots` tries four strategies in order: `data-screenshot-index` img tags, old hard-coded patterns, `ULeU3b` container with windowed DOTALL, and a generic fallback.
