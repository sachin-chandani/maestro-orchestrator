import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/tokens.css'
import './styles/base.css'
import App from './App.tsx'
import { ClientProvider } from './app/ClientProvider.tsx'
import { chooseClientKind, createClient, readDevChoice } from './app/createClient.ts'

const root = document.getElementById('root')
if (!root) throw new Error('index.html is missing the #root element')

// Development-only tools: the component gallery at /app/?gallery and the dev panel (#101, #102).
// `import.meta.env.DEV` is false in production builds, so these imports are dropped and not shipped.
const Gallery = import.meta.env.DEV ? lazy(() => import('./dev/Gallery.tsx')) : null
const DevPanel = import.meta.env.DEV ? lazy(() => import('./dev/DevPanel.tsx')) : null
const showGallery = Gallery !== null && new URLSearchParams(location.search).has('gallery')

const kind = chooseClientKind(import.meta.env, import.meta.env.DEV ? readDevChoice() : null)
const client = createClient(kind)

createRoot(root).render(
  <StrictMode>
    {showGallery ? (
      <Suspense>
        <Gallery />
      </Suspense>
    ) : (
      <ClientProvider client={client} kind={kind}>
        <App
          devTools={
            DevPanel
              ? (reload) => (
                  <Suspense>
                    <DevPanel reload={reload} />
                  </Suspense>
                )
              : undefined
          }
        />
      </ClientProvider>
    )}
  </StrictMode>,
)
