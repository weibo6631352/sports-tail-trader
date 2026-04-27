import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { adminApi } from '../../core/api/resources'
import type { CandidateRecord, JsonObject, JsonValue, SportsLiveStateRecord } from '../../core/api/types'
import { formatApiError } from '../../core/api/client'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { SectionCard } from '../../shared/ui/SectionCard'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatBool, formatDateTime, formatDecimal } from '../../shared/utils/format'

const pageSize = 50

const rowKey = (row: CandidateRecord): string =>
  [
    row.trace_id,
    row.condition_id,
    row.market_slug,
    row.token_id,
    row.action,
    row.updated_at,
    row.created_at,
  ]
    .filter(Boolean)
    .join(':')

const liveStateRowKey = (row: SportsLiveStateRecord): string =>
  [row.condition_id, row.market_slug, row.event_slug, row.source, row.updated_at].filter(Boolean).join(':')

const liveGameField = (row: SportsLiveStateRecord, field: string): string | number | null => {
  const game = row.metadata?.sports_tail_game
  if (!game || typeof game !== 'object' || Array.isArray(game)) {
    return null
  }
  const value = game[field]
  if (typeof value === 'string' || typeof value === 'number') {
    return value
  }
  return null
}

const asJsonObject = (value: JsonValue | undefined): JsonObject | null => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }
  return value
}

const syncStatusTone = (status: JsonObject | null): 'neutral' | 'success' | 'warning' | 'danger' => {
  if (!status) {
    return 'neutral'
  }
  if (status.last_error) {
    return 'danger'
  }
  if (status.running) {
    return 'warning'
  }
  if (status.last_success_at) {
    return 'success'
  }
  return status.enabled ? 'warning' : 'neutral'
}

const permissionTone = (permission: string | null | undefined): 'neutral' | 'success' | 'warning' | 'danger' => {
  const normalized = permission?.trim().toLowerCase()
  if (!normalized) {
    return 'neutral'
  }
  if (normalized === 'allowed' || normalized === 'allow' || normalized === 'permitted') {
    return 'success'
  }
  if (normalized === 'blocked' || normalized === 'denied' || normalized === 'reject' || normalized === 'rejected') {
    return 'danger'
  }
  return 'warning'
}

export const CandidatesPage = () => {
  const queryClient = useQueryClient()
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [marketSlug, setMarketSlug] = useState('')
  const [marketType, setMarketType] = useState('')
  const [gameStatus, setGameStatus] = useState('')
  const [actionFilter, setActionFilter] = useState('')
  const [permissionFilter, setPermissionFilter] = useState('')
  const [acceptedFilter, setAcceptedFilter] = useState('')
  const [confirmableFilter, setConfirmableFilter] = useState('')
  const [league, setLeague] = useState('')
  const [offset, setOffset] = useState(0)
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateRecord | null>(null)
  const [operator, setOperator] = useState('人工')
  const [note, setNote] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const candidatesQuery = useQuery({
    queryKey: [
      'candidates',
      {
        conditionId,
        tokenId,
        marketSlug,
        marketType,
        gameStatus,
        actionFilter,
        permissionFilter,
        acceptedFilter,
        confirmableFilter,
        league,
        offset,
      },
    ],
    queryFn: () =>
      adminApi.listCandidates({
        limit: pageSize,
        offset,
        condition_id: conditionId || undefined,
        token_id: tokenId || undefined,
        market_slug: marketSlug || undefined,
        market_type: marketType || undefined,
        game_status: gameStatus || undefined,
        action: actionFilter || undefined,
        execution_permission: permissionFilter || undefined,
        accepted: acceptedFilter || undefined,
        confirmable: confirmableFilter || undefined,
        league: league || undefined,
      }),
    refetchInterval: 8_000,
  })

  const liveStatesQuery = useQuery({
    queryKey: ['candidates-live-states'],
    queryFn: () => adminApi.listSportsLiveStates({ limit: 20, offset: 0 }),
    refetchInterval: 8_000,
  })

  const workersQuery = useQuery({
    queryKey: ['workers', 'sports-live-sync'],
    queryFn: adminApi.getWorkers,
    refetchInterval: 8_000,
  })

  const confirmMutation = useMutation({
    mutationFn: (candidate: CandidateRecord) =>
      adminApi.confirmCandidate({
        condition_id: candidate.condition_id ?? undefined,
        market_slug: candidate.market_slug ?? undefined,
        token_id: candidate.token_id,
        operator: operator.trim() || undefined,
        note: note.trim() || undefined,
        trace_id: candidate.trace_id ?? undefined,
      }),
    onSuccess: async () => {
      setFormError(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['candidates'] }),
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
        queryClient.invalidateQueries({ queryKey: ['positions'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
        queryClient.invalidateQueries({ queryKey: ['audit-events'] }),
        queryClient.invalidateQueries({ queryKey: ['outbox'] }),
        queryClient.invalidateQueries({ queryKey: ['outbox-pending'] }),
      ])
    },
  })

  const columns: Array<DataColumn<CandidateRecord>> = useMemo(
    () => [
      {
        key: 'market',
        header: '市场',
        cell: (row) => (
          <div className="table-primary">
            {row.market_slug ? (
              <MarketExternalLink
                className="market-list__title"
                eventSlug={row.event_slug}
                onClick={(event) => {
                  event.stopPropagation()
                }}
              >
                {row.event_title ?? row.market_slug}
              </MarketExternalLink>
            ) : (
              <strong>{row.condition_id ?? '—'}</strong>
            )}
            <span>{row.market_slug ?? row.condition_id ?? '—'}</span>
          </div>
        ),
      },
      {
        key: 'token',
        header: 'Token / outcome',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.outcome ?? '—'}</strong>
            <span>{row.token_id}</span>
          </div>
        ),
      },
      {
        key: 'action',
        header: 'action',
        cell: (row) => row.action ?? '—',
      },
      {
        key: 'accepted',
        header: 'accepted',
        cell: (row) => (
          <StatusPill label={formatBool(Boolean(row.accepted))} tone={row.accepted ? 'success' : 'neutral'} />
        ),
      },
      {
        key: 'permission',
        header: 'execution_permission',
        cell: (row) => (
          <StatusPill
            label={row.execution_permission ?? '—'}
            tone={permissionTone(row.execution_permission)}
          />
        ),
      },
      {
        key: 'reason',
        header: 'reason',
        cell: (row) => row.reason ?? '—',
      },
      {
        key: 'state',
        header: '盘口 / 状态',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.market_type ?? '—'}</strong>
            <span>{[row.league, row.game_status, row.period].filter(Boolean).join(' / ') || '—'}</span>
          </div>
        ),
      },
      {
        key: 'bestAsk',
        header: 'best_ask',
        align: 'right',
        cell: (row) => formatDecimal(row.best_ask),
      },
      {
        key: 'seconds',
        header: 'seconds_remaining',
        align: 'right',
        cell: (row) => formatDecimal(row.seconds_remaining),
      },
      {
        key: 'confirmable',
        header: 'confirmable',
        cell: (row) => (
          <StatusPill label={formatBool(row.confirmable)} tone={row.confirmable ? 'success' : 'neutral'} />
        ),
      },
    ],
    [],
  )

  const liveStateColumns: Array<DataColumn<SportsLiveStateRecord>> = [
    {
      key: 'market',
      header: '市场',
      cell: (row) => (
        <div className="table-primary">
          <strong>{row.market_slug ?? row.condition_id ?? '—'}</strong>
          <span>{row.event_slug ?? row.source ?? '—'}</span>
        </div>
      ),
    },
    {
      key: 'status',
      header: '状态',
      cell: (row) => liveGameField(row, 'status') ?? liveGameField(row, 'period') ?? '—',
    },
    {
      key: 'clock',
      header: '时间',
      cell: (row) => formatDecimal(liveGameField(row, 'seconds_remaining')),
    },
    {
      key: 'updated',
      header: '更新时间',
      cell: (row) => formatDateTime(row.updated_at),
    },
  ]

  const selectedCandidateKey = selectedCandidate ? rowKey(selectedCandidate) : null
  const requestError = confirmMutation.error ? formatApiError(confirmMutation.error) : null
  const canConfirm = Boolean(selectedCandidate?.confirmable) && !confirmMutation.isPending
  const syncStatus = asJsonObject(workersQuery.data?.sports_live_sync)

  const handleConfirm = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    confirmMutation.reset()

    if (!selectedCandidate) {
      setFormError('请先从左侧选择一个候选。')
      return
    }

    setFormError(null)
    confirmMutation.mutate(selectedCandidate)
  }

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">候选</p>
          <h1>体育扫尾候选</h1>
          <p>查看策略输出的候选，并通过人工确认入口提交确认请求。</p>
        </div>
      </header>

      <SectionCard title="筛选条件" subtitle="候选列表自动刷新，确认可用性以服务端 confirmable 字段为准。">
        <div className="form-grid form-grid--filters">
          <label>
            <span>条件 ID</span>
            <input value={conditionId} onChange={(event) => setConditionId(event.target.value)} />
          </label>
          <label>
            <span>代币 ID</span>
            <input value={tokenId} onChange={(event) => setTokenId(event.target.value)} />
          </label>
          <label>
            <span>市场标识</span>
            <input value={marketSlug} onChange={(event) => setMarketSlug(event.target.value)} />
          </label>
          <label>
            <span>盘口类型</span>
            <input value={marketType} onChange={(event) => setMarketType(event.target.value)} />
          </label>
          <label>
            <span>比赛状态</span>
            <input value={gameStatus} onChange={(event) => setGameStatus(event.target.value)} />
          </label>
          <label>
            <span>动作</span>
            <input value={actionFilter} onChange={(event) => setActionFilter(event.target.value)} />
          </label>
          <label>
            <span>执行权限</span>
            <input value={permissionFilter} onChange={(event) => setPermissionFilter(event.target.value)} />
          </label>
          <label>
            <span>accepted</span>
            <select value={acceptedFilter} onChange={(event) => setAcceptedFilter(event.target.value)}>
              <option value="">全部</option>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </label>
          <label>
            <span>confirmable</span>
            <select value={confirmableFilter} onChange={(event) => setConfirmableFilter(event.target.value)}>
              <option value="">全部</option>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </label>
          <label>
            <span>联赛</span>
            <input value={league} onChange={(event) => setLeague(event.target.value)} />
          </label>
        </div>
      </SectionCard>

      <div className="content-grid content-grid--wide-aside">
        <SectionCard
          title="候选列表"
          subtitle={`服务端共 ${candidatesQuery.data?.total ?? 0} 条，当前第 ${Math.floor(offset / pageSize) + 1} 页。`}
          actions={
            <div className="inline-actions">
              <button type="button" onClick={() => setOffset((current) => Math.max(0, current - pageSize))} disabled={offset === 0}>
                上一页
              </button>
              <button
                type="button"
                onClick={() => setOffset((current) => current + pageSize)}
                disabled={(candidatesQuery.data?.items.length ?? 0) < pageSize}
              >
                下一页
              </button>
            </div>
          }
        >
          <DataTable
            columns={columns}
            rows={candidatesQuery.data?.items ?? []}
            rowKey={rowKey}
            emptyTitle="没有候选"
            emptyDescription="当前筛选下没有体育扫尾候选。"
            onRowClick={setSelectedCandidate}
            selectedRowKey={selectedCandidateKey}
          />
        </SectionCard>

        <div className="detail-stack">
          <SectionCard title="人工确认" subtitle="只提交选中候选，前端不重算策略确认条件。">
            <form className="form-grid" onSubmit={handleConfirm}>
              <label>
                <span>操作者</span>
                <input value={operator} onChange={(event) => setOperator(event.target.value)} />
              </label>
              <label>
                <span>备注</span>
                <input value={note} onChange={(event) => setNote(event.target.value)} />
              </label>
              <button type="submit" disabled={!canConfirm}>
                {confirmMutation.isPending ? '确认中...' : '确认候选'}
              </button>
            </form>
            {selectedCandidate && !selectedCandidate.confirmable ? (
              <ul className="message-list form-feedback">
                <li>
                  <strong>不可确认</strong>
                  <span>当前候选的 confirmable 为否。</span>
                </li>
              </ul>
            ) : null}
            {formError ? (
              <ul className="message-list form-feedback">
                <li>
                  <strong>表单校验</strong>
                  <span>{formError}</span>
                </li>
              </ul>
            ) : null}
            {requestError ? (
              <ul className="message-list form-feedback">
                <li>
                  <strong>请求失败</strong>
                  <span>{requestError}</span>
                </li>
              </ul>
            ) : null}
          </SectionCard>

          <SectionCard title="当前候选" subtitle="点击左侧候选查看原始 payload。">
            <JsonPanel
              value={selectedCandidate?.payload ?? selectedCandidate}
              emptyLabel={selectedCandidate ? '候选 payload 为空。' : '尚未选择候选。'}
              detailsLabel="查看候选 payload"
              defaultOpen={Boolean(selectedCandidate)}
            />
          </SectionCard>

          <SectionCard title="确认结果" subtitle="展示 confirmCandidate 返回内容。">
            <JsonPanel
              value={confirmMutation.data}
              emptyLabel="尚未确认候选。"
              detailsLabel="查看确认结果原始数据"
              summary={
                confirmMutation.data ? (
                  <div className="detail-list">
                    <div>
                      <dt>状态</dt>
                      <dd>{confirmMutation.data.status}</dd>
                    </div>
                    <div>
                      <dt>追踪 ID</dt>
                      <dd>{confirmMutation.data.trace_id ?? '—'}</dd>
                    </div>
                    <div>
                      <dt>说明</dt>
                      <dd>{confirmMutation.data.reason ?? '—'}</dd>
                    </div>
                  </div>
                ) : null
              }
            />
          </SectionCard>

          <SectionCard title="直播同步" subtitle="展示外部体育状态源的运行时快照。">
            <JsonPanel
              value={syncStatus}
              emptyLabel="尚无直播同步状态。"
              detailsLabel="查看直播同步原始状态"
              summary={
                syncStatus ? (
                  <div className="detail-list">
                    <div>
                      <dt>状态</dt>
                      <dd>
                        <StatusPill
                          label={
                            syncStatus.last_error
                              ? '异常'
                              : syncStatus.running
                                ? '同步中'
                                : syncStatus.enabled
                                  ? '已启用'
                                  : '未启用'
                          }
                          tone={syncStatusTone(syncStatus)}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt>来源</dt>
                      <dd>{String(syncStatus.source ?? '—')}</dd>
                    </div>
                    <div>
                      <dt>最近成功</dt>
                      <dd>{formatDateTime(String(syncStatus.last_success_at ?? ''))}</dd>
                    </div>
                    <div>
                      <dt>匹配</dt>
                      <dd>{String(syncStatus.last_matches ?? 0)} / {String(syncStatus.last_markets_seen ?? 0)}</dd>
                    </div>
                    <div>
                      <dt>信号</dt>
                      <dd>{String(syncStatus.last_entry_signals_published ?? 0)}</dd>
                    </div>
                  </div>
                ) : null
              }
            />
          </SectionCard>

          <SectionCard title="现场状态" subtitle={`最近 ${liveStatesQuery.data?.items.length ?? 0} 条现场状态。`}>
            <DataTable
              columns={liveStateColumns}
              rows={liveStatesQuery.data?.items ?? []}
              rowKey={liveStateRowKey}
              emptyTitle="没有现场状态"
              emptyDescription="当前没有可展示的体育现场状态。"
            />
          </SectionCard>
        </div>
      </div>
    </div>
  )
}
