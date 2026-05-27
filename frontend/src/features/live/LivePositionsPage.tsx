import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import { Alert, Checkbox, Group, Stack, Text } from '@mantine/core'
import { qk, qkRoots } from '@core/api/keys'
import { positionsApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { confirmAction } from '@shared/forms/confirmAction'
import { describeError } from '@core/api/errors'
import { PositionAnalysisCard } from './PositionAnalysisCard'

// 决策器接管的盘口——已入场每个 token 一张实时分析卡:
// 比赛 / 持仓 / 订单簿深度 / 量化决策 4 维度一站可视化, 5s 自动刷新.
// 操盘人验证量化决策正确性的"凭证页"——所有输入(盘口/赔率/比分)+所有
// 输出(Kelly 内核 / 最新 decision reason)直接显示.

export function LivePositionsPage() {
  const client = useQueryClient()
  const [activeOnly, setActiveOnly] = useState(true)
  const params = { level: 'detail' as const, limit: 100 }

  const query = useQuery({
    queryKey: qk.positions.list(params),
    queryFn: ({ signal }) => positionsApi.list(params, signal),
    refetchInterval: 5000,
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
  const redeemableCount = allItems.filter((p) => p.redeemable).length
  const awaitingCount = allItems.filter((p) => p.awaiting_settlement).length
  // 决策器接管 = 仍 LIVE 的活仓: 排除 redeemable(已 closed)与 awaiting_settlement(赛事已结等 resolve)
  const displayItems = activeOnly
    ? allItems.filter((p) => !p.redeemable && !p.awaiting_settlement)
    : allItems

  return (
    <>
      <PageHeader
        title="决策器接管的盘口"
        subtitle="已入场 = WS 订阅活跃 · market_tick 实时驱动 quant_decide · 每秒级评估退出/调仓 · 每仓位一张实时分析卡（比赛/持仓/订单簿/量化）"
      />

      {query.error ? <QueryErrorNotice error={query.error} compact /> : null}

      {redeemableCount > 0 && (
        <Alert color="orange" mb="sm" variant="light">
          <Text size="sm">
            <strong>{redeemableCount} 个持仓已结算（待赎回）</strong>——决策器不再评估，需链上 redeem 清出账户。
            正确押注 → 拿对应金额；错误押注 → $0。
          </Text>
        </Alert>
      )}

      {awaitingCount > 0 && (
        <Alert color="grape" mb="sm" variant="light">
          <Text size="sm">
            <strong>{awaitingCount} 个持仓等结算中</strong>——赛事已结束 / Goalserve 停推 &gt; 5min，但 Polymarket 还没 resolve。
            看快照与等结算列表请到 <a href="/live/awaiting-settlement">等结算盘口</a>。
          </Text>
        </Alert>
      )}

      <Group mb="sm" justify="space-between">
        <Checkbox
          size="xs"
          label="只看决策器接管中（隐藏已结算 / 等结算）"
          checked={activeOnly}
          onChange={(e) => setActiveOnly(e.currentTarget.checked)}
        />
        <Text size="xs" c="dimmed">
          {displayItems.length} 个仓位 · 5s 自动刷新
          {query.isFetching ? ' · 刷新中…' : ''}
        </Text>
      </Group>

      {displayItems.length === 0 ? (
        <EmptyState
          title="当前无被决策器接管的盘口"
          description={
            activeOnly && redeemableCount > 0
              ? '取消勾选可显示已结算待赎回的仓位'
              : '量化决策器在新候选满足 Kelly 入场条件时建仓后此页将显示对应卡片'
          }
        />
      ) : (
        <Stack gap="md">
          {displayItems.map((p) => (
            <PositionAnalysisCard
              key={`${p.condition_id}_${p.token_id}`}
              position={p}
              onForceExit={() => {
                confirmAction({
                  title: `Force Exit · ${p.market_slug ?? p.condition_id}`,
                  description: '将以 market 价格强制平仓；不可撤销。',
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
