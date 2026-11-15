import { StrictMode, useState, useEffect, lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import Landing from './Landing.jsx'
import { AuthProvider } from './contexts/AuthContext'

const App = lazy(() => import('./App.jsx'))

function Root() {
  // The app lives at #app (and #app?tab=… deep links). Everything else,
  // including a bare / and the section anchors, is the landing page.
  const resolveView = () => {
    const hash = window.location.hash || '';
    if (hash === '#app' || hash.startsWith('#app?') || hash === '#/app') return 'app';
    return 'landing';
  };

  const [view, setView] = useState(resolveView);

  useEffect(() => {
    const handleHashChange = () => setView(resolveView());
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  const handleLaunchApp = () => {
    window.location.hash = '#app';
    setView('app');
  };

  if (view === 'app') return <App />;
  return <Landing onLaunchApp={handleLaunchApp} />;
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <AuthProvider>
      <Suspense fallback={<div className="min-h-screen bg-paper flex items-center justify-center text-muted text-sm lowercase">загрузка…</div>}>
        <Root />
      </Suspense>
    </AuthProvider>
  </StrictMode>,
)
