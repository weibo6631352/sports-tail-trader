import { formatApiError } from '../../core/api/client'

interface QueryErrorNoticeProps {
  title: string
  error: unknown
}

export const QueryErrorNotice = ({ title, error }: QueryErrorNoticeProps) => (
  <div className="query-error" role="alert">
    <strong>{title}</strong>
    <span>{formatApiError(error)}</span>
  </div>
)
