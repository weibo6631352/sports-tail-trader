import { useRoutes } from 'react-router-dom'
import { Providers } from './providers'
import { routes } from './routes'
import { SseProvider } from '@core/sse'

function Router() {
  return useRoutes(routes)
}

export function App() {
  return (
    <Providers>
      <SseProvider>
        <Router />
      </SseProvider>
    </Providers>
  )
}
