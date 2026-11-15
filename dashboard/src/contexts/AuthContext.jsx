// Server config for the dashboard (/api/config). There are no accounts and no
// billing: the context keeps its old name and the account-shaped fields
// (billingEnabled, isSignedIn) as constants because the landing components
// still read them.
import { createContext, useContext, useState, useEffect } from 'react';
import { getApiUrl } from '../config';

const AuthContext = createContext(null);
// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }) {
  const [config, setConfig] = useState({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(getApiUrl('/api/config'))
      .then((r) => (r.ok ? r.json() : {}))
      .then(setConfig)
      .catch(() => { /* config unavailable: defaults */ })
      .finally(() => setLoading(false));
  }, []);

  const value = {
    billingEnabled: false,
    isSignedIn: false,
    loading,
    agentClips: !!config.agentClips,
    localLlm: config.localLlm || null,
    youtubeUrlEnabled: config.youtubeUrlEnabled !== false,
    jobRetentionSeconds: config.jobRetentionSeconds || null,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
