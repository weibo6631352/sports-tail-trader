# Sports Opportunity Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ended-not-closed entries and controlled scale-in entries without bypassing the existing trading, risk, and follow-up exit chain.

**Architecture:** Strategy logic remains under `src/strategies/current/`; framework changes are limited to a generic BUY intent flag that lets RiskManager distinguish ordinary re-entry from explicitly controlled scale-in. Worker lifecycle accepts scale-in plans while a follow-up exit order is open, but ordinary re-entry remains blocked by strategy and risk.

**Tech Stack:** Python 3.12, pytest, Polymarket domain/app layering, current strategy extension hooks.

---

### Task 1: Red Tests

**Files:**
- Modify: `tests/strategies/current/test_sports_tail_target_slice.py`
- Modify: `tests/domain/test_risk.py`
- Modify: `tests/workers/test_trading_decision_gate.py`

- [ ] Add tests for ended-not-closed entry on final-score Moneyline and Totals Under.
- [ ] Add tests proving push/tie outcomes remain skipped.
- [ ] Add tests proving scale-in requires exit coverage and strict advantage.
- [ ] Add tests proving ordinary BUY still fails open-exit risk while controlled scale-in passes.
- [ ] Add worker lifecycle test proving a scale-in plan is not dropped while market lifecycle is `FOLLOW_UP_ORDER_OPEN`.

### Task 2: Pure Strategy Opportunity Model

**Files:**
- Modify: `src/strategies/current/sports_tail.py`
- Modify: `src/strategies/current/config.py`

- [ ] Add a stable opportunity type field to `SportsTailEvaluation`.
- [ ] Add `ended_not_closed` final-score evaluators.
- [ ] Add `scale_in_advantage` stricter evaluators.
- [ ] Keep live-tail behavior unchanged for current passing tests.

### Task 3: Strategy Allocation And Entry

**Files:**
- Modify: `src/strategies/current/allocation.py`
- Modify: `src/strategies/current/trading.py`

- [ ] Add strategy-local allocation flags for scale-in eligibility and budget caps.
- [ ] Keep ordinary existing-position/open-exit candidates skipped.
- [ ] Allow eligible scale-in candidates through allocation with capped budget.
- [ ] Emit `strategy_scale_in` BUY decisions with `allow_open_exit_overlap` metadata.

### Task 4: Generic Risk And Worker Support

**Files:**
- Modify: `src/polymarket_trader/domain/order.py`
- Modify: `src/polymarket_trader/app/extension_intent_builder.py`
- Modify: `src/polymarket_trader/domain/risk.py`
- Modify: `src/polymarket_trader/workers/trading_decision_worker.py`
- Modify: `src/polymarket_trader/workers/trading_decision_event_payloads.py`

- [ ] Add generic `allow_open_exit_overlap` to BUY intent.
- [ ] Keep the default open-exit risk rejection unchanged.
- [ ] Allow open-exit overlap only when the BUY intent explicitly marks controlled scale-in.
- [ ] Let entry-signal processing continue for scale-in plans in position/follow-up lifecycle states.
- [ ] Serialize the flag for audit visibility.

### Task 5: Documentation And Verification

**Files:**
- Modify: `docs/体育扫尾策略设计.md`
- Modify: `docs/开发进度.md`

- [ ] Document `ended_not_closed` and `scale_in_advantage`.
- [ ] Run focused pytest files.
- [ ] Run full `PYTHONPATH=src pytest -q`.
- [ ] Run `ruff check .`.
- [ ] Commit and push.
