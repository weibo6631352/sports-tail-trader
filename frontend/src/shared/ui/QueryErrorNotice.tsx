import { Alert, Button, Group, Text } from '@mantine/core'
import { IconAlertTriangle, IconRefresh } from '@tabler/icons-react'
import { ApiError, describeError } from '@core/api'

type Props = {
  error: unknown
  onRetry?: () => void
  compact?: boolean
}

export function QueryErrorNotice({ error, onRetry, compact }: Props) {
  const isRateLimited = error instanceof ApiError && error.isRateLimited()
  const isUnavailable = error instanceof ApiError && error.isUnavailable()
  const title = isRateLimited
    ? '请求被限流（429）'
    : isUnavailable
      ? '后端暂不可用（503）'
      : '请求失败'
  return (
    <Alert color={isRateLimited ? 'yellow' : 'red'} variant="light" icon={<IconAlertTriangle size={16} />}>
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <div>
          <Text fw={600} size="sm">
            {title}
          </Text>
          {!compact && (
            <Text size="xs" c="dimmed" mt={2}>
              {describeError(error)}
            </Text>
          )}
        </div>
        {onRetry ? (
          <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />} onClick={onRetry}>
            重试
          </Button>
        ) : null}
      </Group>
    </Alert>
  )
}
