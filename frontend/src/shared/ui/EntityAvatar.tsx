import { useMemo, useState } from 'react'

interface EntityAvatarProps {
  label: string
  imageUrl?: string | null
  size?: 'sm' | 'md'
}

export const EntityAvatar = ({ label, imageUrl, size = 'sm' }: EntityAvatarProps) => {
  const [imageFailed, setImageFailed] = useState(false)
  const fallbackLabel = useMemo(() => {
    const normalized = label.trim()
    if (!normalized) {
      return '?'
    }
    return normalized[0]?.toUpperCase() ?? '?'
  }, [label])

  return (
    <span className={`entity-avatar entity-avatar--${size}`} aria-hidden="true">
      {imageUrl && !imageFailed ? (
        <img src={imageUrl} alt="" loading="lazy" onError={() => setImageFailed(true)} />
      ) : (
        <span>{fallbackLabel}</span>
      )}
    </span>
  )
}
