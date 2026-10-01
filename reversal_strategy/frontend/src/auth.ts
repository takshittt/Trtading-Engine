// Independent login for the Swing dashboard. The shared Gateway is the identity
// provider, but Swing keeps its OWN session: the user signs in on Swing's own
// form, Swing's backend verifies the credentials against the Gateway (via the
// /api/auth/login proxy) and returns the Gateway-issued token, which we store
// here. The browser never talks to the Gateway directly — same login topology
// as grid_strategy.

const KEY = 'swing_token'

// Unset (dev, or served behind the backend's reverse proxy): same origin, and
// the Vite proxy forwards /api. Set at build time when the UI is hosted apart
// from the API, e.g. VITE_API_BASE=https://reversal-api.example.com
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export interface AuthUser {
  id: number
  email: string
  form_filled: boolean
}

export function getToken(): string | null {
  return localStorage.getItem(KEY)
}
export function setToken(t: string): void {
  localStorage.setItem(KEY, t)
}
export function clearToken(): void {
  localStorage.removeItem(KEY)
}

export function authHeaders(): Record<string, string> {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

/** Sign in against Swing's login proxy (which verifies against the Gateway).
 *  Stores the returned token and returns the user. Throws on bad credentials. */
export async function login(email: string, password: string): Promise<AuthUser> {
  const r = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!r.ok) {
    let detail = 'Invalid email or password'
    try {
      detail = (await r.json()).detail ?? detail
    } catch {
      /* non-JSON error — keep the default */
    }
    throw new Error(detail)
  }
  const data = await r.json()
  setToken(data.token)
  return data.user as AuthUser
}

/** Drop this dashboard's session and reload, which lands back on LoginGate.
 *  Only signs out of Swing — the Gateway's own session is untouched. */
export function logout(): void {
  clearToken()
  window.location.reload()
}
