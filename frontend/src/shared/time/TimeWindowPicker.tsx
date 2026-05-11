import { Group, SegmentedControl, Text } from '@mantine/core'
import { useTimeWindowStore, type TimeWindowPreset } from '@core/time/store'
import { useTimeWindowUrlSync } from '@core/time/useTimeWindowUrlSync'
import { formatEpochMs } from '@shared/format'

const PRESETS: { value: TimeWindowPreset; label: string }[] = [
  { value: 'last_1h', label: '1h' },
  { value: 'last_6h', label: '6h' },
  { value: 'last_24h', label: '24h' },
  { value: 'last_7d', label: '7d' },
  { value: 'last_30d', label: '30d' },
]

export function TimeWindowPicker() {
  // 挂载即接通 URL ↔ store 同步——分享链接、刷新、浏览器后退都保持时间窗。
  useTimeWindowUrlSync()
  const { preset, since, until, setPreset } = useTimeWindowStore()
  return (
    <Group gap="sm">
      <SegmentedControl
        size="xs"
        value={preset === 'custom' ? '' : preset}
        data={PRESETS}
        onChange={(value) => setPreset((value || 'last_24h') as TimeWindowPreset)}
      />
      <Text c="dimmed" size="xs">
        {since !== null && until !== null
          ? `${formatEpochMs(since, 'MM-DD HH:mm')} → ${formatEpochMs(until, 'MM-DD HH:mm')}`
          : '自定义'}
      </Text>
    </Group>
  )
}
