import { Suspense } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { RouteFallback } from '@shared/ui/RouteFallback'
import { RouteErrorBoundary } from '@shared/ui/RouteErrorBoundary'
import styles from './AppShell.module.css'

export function AppShell() {
  // 用 pathname + search 作 key：路由切换 / search 变化时都重建 ErrorBoundary，
  // 避免同路径下不同 query 时一次错误粘住后续页面。
  const location = useLocation()
  return (
    <div className={styles.root}>
      <Sidebar />
      <div className={styles.main}>
        <Topbar />
        <div className={styles.content}>
          <RouteErrorBoundary key={`${location.pathname}${location.search}`}>
            <Suspense fallback={<RouteFallback />}>
              <Outlet />
            </Suspense>
          </RouteErrorBoundary>
        </div>
      </div>
    </div>
  )
}
