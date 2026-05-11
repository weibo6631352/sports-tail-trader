import { Stack, TextInput, Textarea } from '@mantine/core'
import { useOperatorStore } from '@core/identity/store'

type Props = {
  operator: string
  reason: string
  onOperatorChange: (value: string) => void
  onReasonChange: (value: string) => void
  reasonPlaceholder?: string
  reasonRequired?: boolean
}

// 通用 operator + reason 表单字段；任何写操作都必须经过这两个字段 → 审计可回看。
// operator 默认值来自 Zustand store（持久化）；只在 blur 时回写 store，
// 避免每次按键都触发 localStorage 写 + store 订阅者全部重渲染。

export function OperatorReasonFields({
  operator,
  reason,
  onOperatorChange,
  onReasonChange,
  reasonPlaceholder = '简述原因（必填，写入审计）',
  reasonRequired = true,
}: Props) {
  const setOperator = useOperatorStore((s) => s.setOperator)
  return (
    <Stack gap="xs">
      <TextInput
        size="xs"
        label="Operator"
        value={operator}
        onChange={(e) => onOperatorChange(e.currentTarget.value)}
        onBlur={(e) => {
          // 失焦时一次性回写 store；空值不覆盖（防误删默认 operator）。
          const trimmed = e.currentTarget.value.trim()
          if (trimmed) setOperator(trimmed)
        }}
        required
      />
      <Textarea
        size="xs"
        label="Reason"
        value={reason}
        onChange={(e) => onReasonChange(e.currentTarget.value)}
        placeholder={reasonPlaceholder}
        required={reasonRequired}
        minRows={2}
        autosize
      />
    </Stack>
  )
}
