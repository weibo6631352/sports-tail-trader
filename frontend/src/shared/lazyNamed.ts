import { lazy, type ComponentType } from 'react'

/**
 * React.lazy 适配命名导出：所有 page module 必须导出与文件 basename 一致的命名导出。
 * 返回 ComponentType（不是 ReactElement）—— 让 React Router 在路由切到时自己实例化，
 * 符合习惯做法、未来 data loader 扩展更顺。
 *
 * 之前在 app/routes.tsx + strategy/current/index.tsx 两处复制，本文件作为单一实现。
 */
export function lazyNamed<P extends string>(
  loader: () => Promise<Record<string, unknown>>,
  name: P,
): ComponentType {
  return lazy(() =>
    loader().then((mod) => {
      const exported = mod[name]
      if (typeof exported !== 'function') {
        throw new Error(`[lazyNamed] module missing export: ${name}`)
      }
      return { default: exported as ComponentType }
    }),
  )
}
