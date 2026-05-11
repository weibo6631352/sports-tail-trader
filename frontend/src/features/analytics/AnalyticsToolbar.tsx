import { Button, Group, TextInput } from '@mantine/core'
import { IconSearch } from '@tabler/icons-react'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { useAnalyticsFiltersStore } from '@core/filters/store'

type Props = {
  onQuery: () => void
  loading?: boolean
  showFilters?: boolean
}

// 分析页通用顶部工具条：时间窗口 + (league / market_type / strategy_id) 过滤器 + 查询按钮。
// 过滤器变更不自动 refetch，必须点查询，给分析师可控节奏。

export function AnalyticsToolbar({ onQuery, loading, showFilters = true }: Props) {
  const { league, marketType, strategyId, setLeague, setMarketType, setStrategyId } =
    useAnalyticsFiltersStore()
  return (
    <Group justify="space-between" align="flex-end" mb="md" wrap="wrap">
      <Group gap="sm" wrap="wrap">
        <TimeWindowPicker />
        {showFilters ? (
          <>
            <TextInput
              size="xs"
              placeholder="league"
              value={league ?? ''}
              onChange={(e) => setLeague(e.currentTarget.value || null)}
              w={120}
            />
            <TextInput
              size="xs"
              placeholder="market_type"
              value={marketType ?? ''}
              onChange={(e) => setMarketType(e.currentTarget.value || null)}
              w={150}
            />
            <TextInput
              size="xs"
              placeholder="strategy_id"
              value={strategyId ?? ''}
              onChange={(e) => setStrategyId(e.currentTarget.value || null)}
              w={150}
            />
          </>
        ) : null}
      </Group>
      <Button
        size="xs"
        color="accent"
        leftSection={<IconSearch size={14} />}
        loading={loading}
        onClick={onQuery}
      >
        查询
      </Button>
    </Group>
  )
}
