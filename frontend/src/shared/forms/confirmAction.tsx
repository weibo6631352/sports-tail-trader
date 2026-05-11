/* eslint-disable react-refresh/only-export-components */
// 同文件导出 confirmAction 函数 + 内部组件，是惯用法——fast-refresh 提示忽略。
import { modals } from '@mantine/modals'
import { Button, Group, Stack, Text } from '@mantine/core'
import { useRef, useState, type ReactNode } from 'react'
import { useOperatorStore } from '@core/identity/store'
import { OperatorReasonFields } from './OperatorReasonFields'
import { DiffPreview, type DiffRow } from './DiffPreview'
import { generateManualTraceId } from './manualTraceId'

type Tone = 'danger' | 'warning' | 'info'

export type ConfirmedOperatorContext = {
  operator: string
  reason: string
  /** 由 confirmAction 自动生成的人工操作追踪 ID，格式 `manual-{uuid hex 12}`。
   *  callsite 把它带给后端 mutation，写操作的审计链自动串起来。 */
  trace_id: string
}

type ConfirmActionInput = {
  title: string
  description?: ReactNode
  diff?: DiffRow[]
  confirmLabel?: string
  cancelLabel?: string
  tone?: Tone
  defaultReason?: string
  requireReason?: boolean
  onConfirm: (params: ConfirmedOperatorContext) => Promise<unknown> | void
}

// 通用二次确认：所有写操作必须经过此函数 → 弹层带 diff + operator + reason，
// 确认按钮在 reason 为空时禁用；onConfirm 抛错自动回到表单态。
// 自动生成 trace_id 注入到 onConfirm，让 callsite 不用各自维护——审计链统一可串。

export function confirmAction(input: ConfirmActionInput): void {
  modals.open({
    title: input.title,
    centered: true,
    size: 'lg',
    children: <ConfirmBody {...input} />,
  })
}

function ConfirmBody(props: ConfirmActionInput) {
  // operator 在 modal 打开时取最新 store 值；之后由用户编辑接管。
  const storedOperator = useOperatorStore((s) => s.operator)
  const [operator, setOperator] = useState(storedOperator)
  const [reason, setReason] = useState(props.defaultReason ?? '')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // trace_id 在 modal mount 时生成一次；失败重试复用同一 trace_id 让审计能串成一条意图。
  const traceIdRef = useRef<string>(generateManualTraceId())

  const requireReason = props.requireReason ?? true
  const canSubmit = operator.trim().length > 0 && (!requireReason || reason.trim().length > 0)

  const handleConfirm = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await props.onConfirm({
        operator: operator.trim(),
        reason: reason.trim(),
        trace_id: traceIdRef.current,
      })
      modals.closeAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const buttonColor = props.tone === 'danger' ? 'red' : props.tone === 'warning' ? 'yellow' : 'accent'

  return (
    <Stack gap="md">
      {props.description ? (
        <Text size="sm" c="dimmed">
          {props.description}
        </Text>
      ) : null}
      {props.diff && props.diff.length > 0 ? <DiffPreview rows={props.diff} /> : null}
      <OperatorReasonFields
        operator={operator}
        reason={reason}
        onOperatorChange={setOperator}
        onReasonChange={setReason}
        reasonRequired={requireReason}
      />
      {error ? (
        <Text size="xs" c="red">
          {error}
        </Text>
      ) : null}
      <Group justify="flex-end" gap="xs">
        <Button variant="default" size="xs" onClick={() => modals.closeAll()} disabled={submitting}>
          {props.cancelLabel ?? '取消'}
        </Button>
        <Button
          color={buttonColor}
          size="xs"
          loading={submitting}
          disabled={!canSubmit || submitting}
          onClick={handleConfirm}
        >
          {props.confirmLabel ?? '确认'}
        </Button>
      </Group>
    </Stack>
  )
}
