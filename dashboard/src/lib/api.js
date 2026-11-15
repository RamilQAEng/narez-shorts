// Centralized API client. Single place every call goes through.
import { getApiUrl } from '../config';

export const AUTH_TOKEN_KEY = 'openshorts_auth';

// Kept for SiteNav (landing): there are no sessions, so this is always ''.
export const getToken = () => localStorage.getItem(AUTH_TOKEN_KEY) || '';

export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(getApiUrl(path), { ...options, headers });
}

// A failed request, carrying the server's own explanation.
export class ApiError extends Error {
  constructor(status, detail, raw) {
    super(`${status}: ${raw}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

// Convenience JSON helper.
export async function apiJson(path, options = {}) {
  const res = await apiFetch(path, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    let detail = '';
    try {
      const d = JSON.parse(text).detail;
      if (typeof d === 'string') detail = d;
    } catch (_) { /* not JSON */ }
    throw new ApiError(res.status, detail, text);
  }
  return res.json();
}
