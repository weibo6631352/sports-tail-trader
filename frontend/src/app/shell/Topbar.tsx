import { ActionIcon, Group, Text, TextInput, Tooltip } from '@mantine/core'
import { IconSearch } from '@tabler/icons-react'
import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { StatusPill } from '@shared/ui/StatusPill'
import { useRuntimeIdentity } from '@core/identity/useRuntimeIdentity'
import { SseStatusBadge } from './SseStatusBadge'
import styles from './Topbar.module.css'

// trace_id 一等公民：顶栏全局搜索可一键跳转到 timeline / decisions 过滤视图。
// 输入条件：trace_id（uuid 风格）或 condition_id（0x... 风格）→ 自动分发。

export function Topbar() {
  const { automaticTradingEnabled, phase, extensionModule } = useRuntimeIdentity()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')

  const handleSearch = () => {
    const v = search.trim()
    if (!v) return
    // 简单规则：以 0x 开头 → condition_id → timeline 直接打开；否则按 trace_id 过滤决策。
    if (v.startsWith('0x')) {
      navigate(`/investigate/timeline?condition_id=${encodeURIComponent(v)}`)
    } else {
      navigate(`/investigate/decisions?trace_id=${encodeURIComponent(v)}`)
    }
  }

  return (
    <Group className={styles.topbar} justify="space-between" wrap="nowrap">
      <Group gap="md" wrap="nowrap">
        <TextInput
          size="xs"
          placeholder="trace_id 或 condition_id → 一键跳转"
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleSearch()
          }}
          leftSection={
            <Tooltip label="按 trace_id 跳决策；0x 开头跳 timeline" withArrow>
              <ActionIcon variant="subtle" size="xs" onClick={handleSearch} aria-label="搜索">
                <IconSearch size={14} />
              </ActionIcon>
            </Tooltip>
          }
          w={320}
        />
        {phase ? (
          <StatusPill tone="info" size="xs">
            phase: {phase}
          </StatusPill>
        ) : null}
        <StatusPill
          tone={automaticTradingEnabled ? 'success' : 'warning'}
          size="xs"
          title={automaticTradingEnabled ? '自动交易已启用' : '自动交易已暂停'}
        >
          {automaticTradingEnabled ? '自动交易：开' : '自动交易：停'}
        </StatusPill>
      </Group>
      <Group gap="sm" wrap="nowrap">
        {extensionModule ? (
          <Text size="xs" c="dimmed">
            策略：{extensionModule}
          </Text>
        ) : null}
        <SseStatusBadge />
      </Group>
    </Group>
  )
}
