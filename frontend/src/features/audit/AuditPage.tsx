import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { adminApi } from '../../core/api/resources'
import type { AuditEventRecord, OutboxEventRecord } from '../../core/api/types'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { formatDateTime } from '../../shared/utils/format'

export const AuditPage = () => {
  const pageSize = 100
  const [traceId, setTraceId] = useState('')
  const [eventTitle, setEventTitle] = useState('')
  const [outboxTraceId, setOutboxTraceId] = useState('')
  const [auditOffset, setAuditOffset] = useState(0)
  const [outboxOffset, setOutboxOffset] = useState(0)

  const auditQuery = useQuery({
    queryKey: ['audit-events', { traceId, eventTitle, auditOffset }],
    queryFn: () =>
      adminApi.listAuditEvents({
        limit: pageSize,
        offset: auditOffset,
        trace_id: traceId || undefined,
        event_title: eventTitle || undefined,
      }),
  })

  const outboxQuery = useQuery({
    queryKey: ['outbox-pending', { outboxTraceId, outboxOffset }],
    queryFn: () =>
      adminApi.listOutboxPending({
        limit: pageSize,
        offset: outboxOffset,
        trace_id: outboxTraceId || undefined,
      }),
  })

  const auditColumns: Array<DataColumn<AuditEventRecord>> = [
    {
      key: 'title',
      header: '事件',
      cell: (row) => (
        <div className="table-primary">
          <strong>{row.event_title}</strong>
          {row.market_slug ? (
            <MarketExternalLink className="link-subtle" eventSlug={row.event_slug}>
              {row.market_slug}
            </MarketExternalLink>
          ) : (
            <span>{row.condition_id ?? '—'}</span>
          )}
        </div>
      ),
    },
    { key: 'status', header: '状态', cell: (row) => row.status ?? '—' },
    { key: 'trace', header: '追踪 ID', cell: (row) => row.trace_id ?? '—' },
    { key: 'reason', header: '原因', cell: (row) => row.reason ?? '—' },
    { key: 'updated', header: '更新时间', cell: (row) => formatDateTime(row.updated_at ?? row.created_at) },
  ]

  const outboxColumns: Array<DataColumn<OutboxEventRecord>> = [
    {
      key: 'event',
      header: '事件',
      cell: (row) => (
        <div className="table-primary">
          <strong>{row.event_type}</strong>
          {row.market_slug ? (
            <MarketExternalLink className="link-subtle" eventSlug={row.event_slug}>
              {row.market_slug}
            </MarketExternalLink>
          ) : (
            <span>{row.condition_id ?? '—'}</span>
          )}
        </div>
      ),
    },
    { key: 'trace', header: '追踪 ID', cell: (row) => row.trace_id ?? '—' },
    { key: 'priority', header: '优先级', cell: (row) => row.priority ?? '—' },
    { key: 'retry', header: '重试次数', cell: (row) => row.retry_count ?? '—' },
    { key: 'created', header: '创建时间', cell: (row) => formatDateTime(row.created_at) },
  ]

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">审计</p>
          <h1>审计事件与待处理外发队列</h1>
          <p>这里查看审计事件和积压队列，用于排查状态流转是否正常。</p>
        </div>
      </header>

      <SectionCard title="过滤条件" subtitle="审计事件和待处理外发队列分别查询。">
        <div className="form-grid form-grid--filters">
          <label>
            <span>审计追踪 ID</span>
            <input
              value={traceId}
              onChange={(event) => {
                setTraceId(event.target.value)
                setAuditOffset(0)
              }}
            />
          </label>
          <label>
            <span>事件名称</span>
            <input
              value={eventTitle}
              onChange={(event) => {
                setEventTitle(event.target.value)
                setAuditOffset(0)
              }}
            />
          </label>
          <label>
            <span>外发队列追踪 ID</span>
            <input
              value={outboxTraceId}
              onChange={(event) => {
                setOutboxTraceId(event.target.value)
                setOutboxOffset(0)
              }}
            />
          </label>
        </div>
      </SectionCard>

      <SectionCard
        title="审计事件"
        subtitle={`当前 ${auditQuery.data?.total ?? 0} 条，当前第 ${Math.floor(auditOffset / pageSize) + 1} 页。`}
        actions={
          <div className="inline-actions">
            <button type="button" onClick={() => setAuditOffset((current) => Math.max(0, current - pageSize))} disabled={auditOffset === 0}>
              上一页
            </button>
            <button
              type="button"
              onClick={() => setAuditOffset((current) => current + pageSize)}
              disabled={(auditQuery.data?.items.length ?? 0) < pageSize}
            >
              下一页
            </button>
          </div>
        }
      >
        <DataTable
          columns={auditColumns}
          rows={auditQuery.data?.items ?? []}
          rowKey={(row) => row.event_id ?? `${row.trace_id}-${row.updated_at ?? row.created_at ?? 'unknown'}`}
          emptyTitle="没有审计事件"
          emptyDescription="当前查询条件下没有匹配的审计事件。"
        />
      </SectionCard>

      <SectionCard
        title="待处理外发队列"
        subtitle={`当前 ${outboxQuery.data?.total ?? 0} 条，当前第 ${Math.floor(outboxOffset / pageSize) + 1} 页。`}
        actions={
          <div className="inline-actions">
            <button
              type="button"
              onClick={() => setOutboxOffset((current) => Math.max(0, current - pageSize))}
              disabled={outboxOffset === 0}
            >
              上一页
            </button>
            <button
              type="button"
              onClick={() => setOutboxOffset((current) => current + pageSize)}
              disabled={(outboxQuery.data?.items.length ?? 0) < pageSize}
            >
              下一页
            </button>
          </div>
        }
      >
        <DataTable
          columns={outboxColumns}
          rows={outboxQuery.data?.items ?? []}
          rowKey={(row) => row.event_id ?? `${row.trace_id}-${row.created_at ?? 'unknown'}`}
          emptyTitle="没有待处理外发事件"
          emptyDescription="当前没有待处理的外发队列事件。"
        />
      </SectionCard>
    </div>
  )
}
