# Sports Market Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit sports market scope modeling so tennis set totals and period-specific markets cannot be confused with full-game totals.

**Architecture:** Keep strategy-specific market semantics in `src/strategies/current/`. Add a small scope model in `sports_tail.py`, feed it through `SportsMarketSnapshot`, and evaluate tennis totals by scope instead of raw slug strings. Non-modeled period markets should be recognized and rejected with auditable reasons rather than silently treated as full-game markets.

**Tech Stack:** Python dataclasses, Decimal, pytest.

---

### Task 1: Scope Model

**Files:**
- Modify: `src/strategies/current/sports_tail.py`
- Test: `tests/strategies/current/test_sports_tail_target_slice.py`

- [ ] Add `SportsMarketScopeType` and `SportsMarketScope`.
- [ ] Add scope fields to `SportsMarketSnapshot`.
- [ ] Preserve default behavior as `full_game` when no scope is supplied.

### Task 2: Tennis Set Games

**Files:**
- Modify: `src/strategies/current/sports_tail.py`
- Test: `tests/strategies/current/test_sports_tail_target_slice.py`

- [ ] Add failing tests for ended first-set total Over and Under.
- [ ] Implement set-number extraction for `first/second/third/fourth/fifth set total`.
- [ ] Evaluate ended tennis set-games totals from `tennis_state.set_scores[set_number - 1]`.
- [ ] Evaluate live tennis set-games Over only when the current or completed set score is mathematically over the line.

### Task 3: Unsupported Period Scopes

**Files:**
- Modify: `src/strategies/current/sports_tail.py`
- Test: `tests/strategies/current/test_sports_tail_target_slice.py`

- [ ] Add tests proving first-quarter/first-half/first-inning totals do not auto-trade as full-game totals.
- [ ] Recognize period scopes and return `tennis_total_scope_unsupported` or a generic unsupported scope reason.

### Task 4: Verification

**Files:**
- Test: full test suite

- [ ] Run targeted tests for the new scope behavior.
- [ ] Run `pytest -q`.
- [ ] Restart live process with `./start_all.sh`.
