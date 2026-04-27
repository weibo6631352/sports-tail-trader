import type { ReactNode } from 'react'

interface SectionCardProps {
  title: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
}

export const SectionCard = ({
  title,
  subtitle,
  actions,
  children,
}: SectionCardProps) => {
  return (
    <section className="section-card">
      <div className="section-card__header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        {actions ? <div className="section-card__actions">{actions}</div> : null}
      </div>
      <div className="section-card__body">{children}</div>
    </section>
  )
}
