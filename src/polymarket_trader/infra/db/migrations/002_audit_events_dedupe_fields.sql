-- 002_audit_events_dedupe_fields.sql
--
-- 给 audit_events 表加 3 个 dedupe 字段：payload_hash / occurrence_count / last_seen_at。
-- 原架构方案 §13.4 + 新架构 §13.3 写入侧 dedupe（worker 侧 AuditDeduper 已 ready，
-- 之前只能 DROP 重复事件丢统计；本 migration 让 deduper 能 UPDATE 累计 occurrence_count）。
--
-- 字段语义：
-- * `payload_hash CHAR(16)`：blake2b-8 hex 短摘要（PAYLOAD 不变即 hash 不变）；
--   配合 (event_title, condition_id, payload_hash) 作为短窗口去重键。
-- * `occurrence_count INT NOT NULL DEFAULT 1`：本条事件在 dedupe 窗口内累计触发次数；
--   首次写入 1，UPDATE 路径 +1。
-- * `last_seen_at TIMESTAMPTZ NULL`：最近一次触发的实际时间；首次写入 = created_at，
--   后续触发更新此字段（created_at 永远是首次时间不变）。
--
-- 索引：暂不强制 unique on (event_title, condition_id, payload_hash) ——
-- 跨分区 unique 在 partitioned table 上要求包含分区键，复杂度高且 deduper
-- 进程内 LRU 已经在内存里收口去重；DB 端 UPDATE 走 event_id（已 unique）即可。
--
-- 安全性：所有新字段允许 NULL 或带 DEFAULT，旧行不需要 backfill（按 CLAUDE.md §8
-- 不写迁移代码原则——历史 audit_events 保留原 occurrence_count = 默认 1）。

BEGIN;

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS payload_hash VARCHAR(16),
    ADD COLUMN IF NOT EXISTS occurrence_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

-- 高频 dedupe 复合索引（不 unique；纯查询加速）。配合 deduper 内存 LRU 兜底。
CREATE INDEX IF NOT EXISTS ix_audit_events_dedupe_lookup
    ON audit_events (event_title, condition_id, payload_hash);

COMMIT;
