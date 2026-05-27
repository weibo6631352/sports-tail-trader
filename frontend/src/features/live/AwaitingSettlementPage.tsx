import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import { Alert, Stack, Text } from '@mantine/core'
import { qk, qkRoots } from '@core/api/keys'
import { positionsApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { confirmAction } from '@shared/forms/confirmAction'
import { describeError } from '@core/api/errors'
import { PositionAnalysisCard } from './PositionAnalysisCard'

// 等结算盘口——赛事已结束(Goalserve 推过 ended/cancelled OR 停推 > 5min)
// 但 Polymarket 还没 resolve.决策器不再评估,操盘观察"最后快照"等链上结算.
// 卡片复用 PositionAnalysisCard(决策接管盘口同款 3 大 section):
// 比赛最后帧 / Polymarket 最后盘口 / 持仓 + 量化决策最后输出.

export function AwaitingSettlementPage() {
  const client = useQueryClient()
  const params = { level: 'detail' as const, limit: 100 }

  const query = useQuery({
    queryKey: qk.positions.list(params),
    queryFn: ({ signal }) => positionsApi.list(params, signal),
    refetchInterval: 10000,
  })

  const forceExit = useMutation({
    mutationFn: positionsApi.forceExit,
    onSuccess: (data) => {
      if (data.status === 'failed') {
        notifications.show({
          title: 'force exit 失败',
          message: data.reason ?? 'market_not_operable',
          color: 'red',
        })
        return
      }
      notifications.show({ title: 'force exit 已提交', message: '后续以 audit_events 为准', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.positions })
      client.invalidateQueries({ queryKey: qkRoots.orders })
      client.invalidateQueries({ queryKey: qkRoots.auditEvents })
    },
    onError: (err) => {
      notifications.show({ title: 'force exit 失败', message: describeError(err), color: 'red' })
    },
  })

  const allItems = query.data?.positions ?? []
  const awaitingItems = allItems.filter((p) => p.awaiting_settlement && !p.redeemable)

  return (
    <>
      <PageHeader
        title="等结算盘口"
        subtitle="赛事已结束 / Goalserve 停推 > 5min · Polymarket 未 resolve · 决策器不再评估 · 显示最后一帧快照（比赛/盘口/赔率）"
      />

      {query.error ? <QueryErrorNotice error={query.error} compact /> : null}

      <Alert color="grape" mb="sm" variant="light">
        <Text size="sm">
          <strong>判定依据</strong>：live_state.status ∈ {`{ended, cancelled, retired, postponed, disputed, finished, ft}`}
          {' '}OR Goalserve 停推 &gt; 5 分钟。Polymarket trading_status 仍 ELIGIBLE，等链上结算。
        </Text>
      </Alert>

      <Text size="xs" c="dimmed" mb="sm">
        {awaitingItems.length} 个仓位 · 10s 自动刷新
        {query.isFetching ? ' · 刷新中…' : ''}
      </Text>

      {awaitingItems.length === 0 ? (
        <EmptyState
          title="当前无等结算盘口"
          description="所有持仓的直播源仍在推送 OR 已转入 redeemable 状态（请到 决策器接管的盘口 / 持仓 redeem 列表 查看）"
        />
      ) : (
        <Stack gap="md">
          {awaitingItems.map((p) => (
            <PositionAnalysisCard
              key={`${p.condition_id}_${p.token_id}`}
              position={p}
              onForceExit={() => {
                confirmAction({
                  title: `Force Exit · ${p.market_slug ?? p.condition_id}`,
                  description: '等结算窗口内强制平仓——若直播源已停推，盘口流动性可能极差，注意滑点。',
                  tone: 'danger',
                  diff: [
                    {
                      field: 'shares',
                      before: p.shares,
                      after: '0',
                      risk: 'high',
                    },
                  ],
                  onConfirm: async ({ operator, reason, trace_id }) => {
                    await forceExit.mutateAsync({
                      condition_id: p.condition_id,
                      token_id: p.token_id,
                      operator,
                      reason,
                      trace_id,
                    })
                  },
                })
              }}
            />
          ))}
        </Stack>
      )}
    </>
  )
}
