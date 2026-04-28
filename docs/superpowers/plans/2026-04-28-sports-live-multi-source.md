# Sports Live Multi Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-source ESPN live state sync with a multi-source live state provider covering ESPN, NBA, NHL, MLB, SofaScore, and TheSportsDB without changing the trading executor path.

**Architecture:** External sports APIs remain in `infra/sports`, each adapter normalizes payloads into `domain.sports_live.SportsLiveGame`. A new aggregate provider fetches enabled sources concurrently, records per-source status, deduplicates equivalent games, and gives live/recent states priority before `SportsLiveStateWorker` matches markets and writes `EntryMetadataStore`.

**Tech Stack:** Python 3.13, httpx, FastAPI runtime settings, pytest, ruff, existing `SportsLiveStateWorker` and `EntryMetadataStore`.

---

### Task 1: Common Sports Data Infrastructure

**Files:**
- Create: `src/polymarket_trader/infra/sports/common.py`
- Modify: `src/polymarket_trader/infra/sports/espn_client.py`
- Modify: `src/polymarket_trader/infra/sports/__init__.py`
- Test: `tests/infra/test_sports_live_aggregate_client.py`

- [x] Move sports data error classes and common helpers out of `espn_client.py` into `common.py`.
- [x] Keep existing ESPN behavior and imports working.
- [x] Add tests that can import shared errors from `polymarket_trader.infra.sports`.

### Task 2: League-Specific Live Adapters

**Files:**
- Create: `src/polymarket_trader/infra/sports/nba_client.py`
- Create: `src/polymarket_trader/infra/sports/nhl_client.py`
- Create: `src/polymarket_trader/infra/sports/mlb_client.py`
- Create: `src/polymarket_trader/infra/sports/sofascore_client.py`
- Create: `src/polymarket_trader/infra/sports/thesportsdb_client.py`
- Modify: `src/polymarket_trader/infra/sports/__init__.py`
- Test: `tests/infra/test_sports_live_league_clients.py`

- [x] Add parser tests for NBA live scoreboard payloads, including live status and ISO clock to total remaining seconds.
- [x] Add parser tests for NHL score payloads, including `CRIT`/`LIVE` status and clock remaining.
- [x] Add parser tests for MLB schedule payloads, including in-progress/final/scheduled mapping and inning labels.
- [x] Add parser tests for SofaScore scheduled-events payloads, including tournament filtering, live status and country-field alias exclusion.
- [x] Add parser tests for TheSportsDB eventsday payloads, including strict league filtering and period status mapping.
- [x] Implement each adapter with `list_games()` returning `SportsLiveSnapshot`.
- [x] Use current date semantics required by each upstream endpoint.

### Task 3: Aggregate Provider

**Files:**
- Create: `src/polymarket_trader/infra/sports/aggregate_client.py`
- Modify: `src/polymarket_trader/domain/sports_live.py`
- Test: `tests/infra/test_sports_live_aggregate_client.py`

- [x] Add a per-source sync status DTO so runtime can expose source-level counts and errors.
- [x] Fetch providers concurrently and do not fail the whole sync when one source fails.
- [x] Deduplicate by league/team aliases and start-time bucket; prefer game status, then official source priority, then fresher observations.
- [x] Keep same teams on different start dates as separate games.
- [x] Return one aggregate `SportsLiveSnapshot` whose source is a stable combined name.

### Task 4: Runtime Wiring and Config

**Files:**
- Modify: `src/polymarket_trader/config.py`
- Modify: `src/polymarket_trader/main.py`
- Modify: `src/polymarket_trader/app/admin_runtime_view.py`
- Modify: `src/polymarket_trader/workers/sports_live_state_worker.py`
- Test: existing worker/runtime tests plus new aggregate tests

- [x] Replace single `SPORTS_LIVE_STATE_SOURCE=espn` validation with multi-value `SPORTS_LIVE_STATE_SOURCES`.
- [x] Construct ESPN/NBA/NHL/MLB/SofaScore/TheSportsDB clients based on enabled sources.
- [x] Pass aggregate `list_games` into `SportsLiveStateWorker`.
- [x] Close every live state client on shutdown.
- [x] Expose per-source source status in `/runtime`, `/workers`, and `/metrics` through the existing `sports_live_sync` snapshot.

### Task 5: Docs and Verification

**Files:**
- Modify: `docs/config.md`
- Modify: `docs/使用说明书.md`
- Modify: `docs/体育直播状态输入设计.md`
- Modify: `docs/开发进度.md`

- [x] Document source configuration and fallback behavior.
- [x] Run targeted tests for sports live infra and worker.
- [x] Run full pytest, ruff, and frontend build if visible runtime fields changed.
- [x] Query the real local `/runtime` and direct clients to confirm enabled sources return a combined snapshot.

### Task 6: Coverage and Source Quality Hardening

**Files:**
- Modify: `src/polymarket_trader/domain/sports_live.py`
- Modify: `src/polymarket_trader/infra/sports/aggregate_client.py`
- Modify: `src/polymarket_trader/infra/sports/mlb_client.py`
- Modify: `src/polymarket_trader/infra/sports/sofascore_client.py`
- Modify: `src/polymarket_trader/infra/sports/thesportsdb_client.py`
- Modify: `src/strategies/current/config.py`
- Modify: `src/strategies/current/live_state.py`
- Modify: `src/strategies/current/sports_tail.py`
- Test: `tests/infra/test_sports_live_aggregate_client.py`
- Test: `tests/infra/test_sports_live_league_clients.py`
- Test: `tests/infra/test_espn_scoreboard_client.py`
- Test: `tests/strategies/current/test_sports_tail_source_quality.py`
- Test: `tests/test_config.py`

- [x] Define source health states so `success_empty`、`cached`、`rate_limited` and `failed` are observable instead of being collapsed into a generic failure.
- [x] Add local cache and rate-limit behavior for SofaScore and TheSportsDB free sources.
- [x] Preserve official source status when a generic free source conflicts, and expose the conflict to strategy metadata.
- [x] Model MLB baseball state using inning、inning half、outs、offense/defense and occupied bases instead of inventing a remaining-seconds clock.
- [x] Keep NFL candidates manual-confirm only until possession、timeouts、down and distance are part of the target model.
- [x] Keep remote discovery broad but make local universe filtering match the currently covered leagues, so unsupported sports do not enter automatic trading only because they have a `sports` category.
