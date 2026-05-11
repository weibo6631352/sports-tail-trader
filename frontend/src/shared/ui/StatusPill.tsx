import { Badge, type BadgeProps } from '@mantine/core'

type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral' | 'accent'

const toneToColor: Record<Tone, BadgeProps['color']> = {
  success: 'teal',
  warning: 'yellow',
  danger: 'red',
  info: 'blue',
  neutral: 'gray',
  accent: 'accent',
}

type Props = {
  tone?: Tone
  children: React.ReactNode
  size?: BadgeProps['size']
  variant?: BadgeProps['variant']
  title?: string
}

export function StatusPill({ tone = 'neutral', size = 'sm', variant = 'light', children, title }: Props) {
  return (
    <Badge color={toneToColor[tone]} size={size} variant={variant} title={title}>
      {children}
    </Badge>
  )
}
