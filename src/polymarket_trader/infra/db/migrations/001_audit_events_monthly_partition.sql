-- 001_audit_events_monthly_partition.sql
--
-- 把 audit_events 改成 PostgreSQL RANGE partition by month。
-- docs/新架构方案.md §13.4 + CLAUDE.md §19。
--
-- 为什么：retention purge 删 30 天前的事件——`DROP PARTITION` 1-2 秒删整个分区
-- 比 `DELETE WHERE created_at < cutoff LIMIT 10000` 循环跑数百次快 100×。
-- 对 >1 亿行的 audit_events 表是数量级提升。
--
-- ============================================================================
-- 使用前提
-- ============================================================================
--
-- 1. 表必须已存在（应用启动会通过 `Base.metadata.create_all` 建表）
-- 2. 库压力低的运维窗口执行（迁移会创建新表 + 拷贝数据 + 交换）
-- 3. 跑前先 `pg_dump --schema-only --table=audit_events $DB > backup.sql`
--
-- ============================================================================
-- 步骤总览
-- ============================================================================
--
-- 1. CREATE TABLE audit_events_partitioned (..) PARTITION BY RANGE (created_at)
-- 2. 创建过去 3 月 + 当前月 + 未来 3 月的分区
-- 3. INSERT 旧 audit_events 数据到 audit_events_partitioned
-- 4. 在事务内 ALTER TABLE … RENAME（原子交换）
-- 5. DROP 旧表
--
-- 配套：跑完后，需要定期通过 `app/audit_partition_manager.py:ensure_future_partitions()`
-- 提前 30 天预创建下个月的分区；和 `drop_expired_partitions()` 滚动删除最老分区。
-- 这两个 job 由应用层 supervisor 跑（已在 src/polymarket_trader/app/audit_partition_manager.py
-- 实现）；本 migration 只做一次性的表结构改造。
--
-- ============================================================================

BEGIN;

-- 步骤 1: 创建分区表（不带主键自增——分区表需用 created_at 作为分区键的一部分）
-- 注意：原表有 id SERIAL PRIMARY KEY 是单列；分区表的 PRIMARY KEY 必须包含分区键
-- 所以新表 PRIMARY KEY 改为 (id, created_at)。
CREATE TABLE IF NOT EXISTS audit_events_partitioned (
    id BIGSERIAL,
    event_id VARCHAR(128) NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    event_title VARCHAR(128) NOT NULL,
    market_slug VARCHAR(255),
    event_slug VARCHAR(255),
    condition_id VARCHAR(128),
    token_id VARCHAR(128),
    outcome VARCHAR(64),
    side VARCHAR(16),
    order_type VARCHAR(16),
    price NUMERIC(38, 18),
    size NUMERIC(38, 18),
    notional_usdc NUMERIC(38, 18),
    order_id VARCHAR(128),
    trade_id VARCHAR(128),
    tx_hash VARCHAR(128),
    status VARCHAR(64),
    reason TEXT,
    raw_response TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at),
    UNIQUE (event_id, created_at)
) PARTITION BY RANGE (created_at);

-- 步骤 2: 创建索引（必须在分区表上创建，不在子分区上单独建）
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_trace_id
    ON audit_events_partitioned (trace_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_event_title
    ON audit_events_partitioned (event_title);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_market_slug
    ON audit_events_partitioned (market_slug);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_event_slug
    ON audit_events_partitioned (event_slug);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_condition_id
    ON audit_events_partitioned (condition_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_token_id
    ON audit_events_partitioned (token_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_order_id
    ON audit_events_partitioned (order_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_trade_id
    ON audit_events_partitioned (trade_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_created_at
    ON audit_events_partitioned (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_events_partitioned_trace_event
    ON audit_events_partitioned (trace_id, event_title);

-- 步骤 3: 创建分区（动态：当前月 ± 3 月）
-- 这块需要在应用层用 audit_partition_manager.ensure_future_partitions() 持续维护。
-- 本 migration 内手工创建过去 3 月 + 当前月 + 未来 3 月共 7 个分区。
-- 模板：audit_events_y2026m05 → range [2026-05-01, 2026-06-01)
--
-- 操作员请在跑 migration 前根据当前 UTC 日期手工填写下列日期：
--
-- 假设当前 UTC 是 2026-05-26，下方分区覆盖 2026-02 ~ 2026-08
CREATE TABLE IF NOT EXISTS audit_events_y2026m02 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m03 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m04 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m05 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m06 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m07 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE IF NOT EXISTS audit_events_y2026m08 PARTITION OF audit_events_partitioned
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

-- 步骤 4: 拷贝数据
-- 注意：如果旧表数据量 > 1000 万行，建议用 pg_dump | pg_restore 而不是直接 INSERT
-- 这里写成 INSERT 是为了一站式迁移；操作员可按表规模选择。
INSERT INTO audit_events_partitioned (
    id, event_id, trace_id, event_title, market_slug, event_slug,
    condition_id, token_id, outcome, side, order_type,
    price, size, notional_usdc,
    order_id, trade_id, tx_hash, status, reason, raw_response,
    payload, created_at, updated_at
)
SELECT
    id, event_id, trace_id, event_title, market_slug, event_slug,
    condition_id, token_id, outcome, side, order_type,
    price, size, notional_usdc,
    order_id, trade_id, tx_hash, status, reason, raw_response,
    payload, created_at, updated_at
FROM audit_events
-- 仅迁移上述分区范围内的数据；范围外的（古老/未来）数据请操作员另行处理
WHERE created_at >= '2026-02-01' AND created_at < '2026-09-01';

-- 步骤 5: 原子交换
ALTER TABLE audit_events RENAME TO audit_events_legacy;
ALTER TABLE audit_events_partitioned RENAME TO audit_events;
ALTER SEQUENCE audit_events_partitioned_id_seq RENAME TO audit_events_id_seq;

-- 步骤 6: 重置 sequence 到当前最大值
SELECT setval('audit_events_id_seq', COALESCE((SELECT MAX(id) FROM audit_events), 1));

-- 步骤 7: 保留旧表 7 天做兜底（确认无误后由 ops 手动 DROP TABLE audit_events_legacy）

COMMIT;

-- ============================================================================
-- 回滚（如需）
-- ============================================================================
-- BEGIN;
-- ALTER TABLE audit_events RENAME TO audit_events_partitioned;
-- ALTER TABLE audit_events_legacy RENAME TO audit_events;
-- ALTER SEQUENCE audit_events_id_seq RENAME TO audit_events_partitioned_id_seq;
-- COMMIT;
-- DROP TABLE audit_events_partitioned CASCADE;
-- ============================================================================
