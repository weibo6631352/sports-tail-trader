export type Tone = 'neutral' | 'success' | 'warning' | 'danger'

interface StatusPillProps {
  label: string
  tone?: Tone
}

export const StatusPill = ({ label, tone = 'neutral' }: StatusPillProps) => {
  return <span className={`status-pill status-pill--${tone}`}>{label}</span>
}
