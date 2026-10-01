// Independent login for the grid dashboard. The Gateway is the identity
// provider, but grid keeps its OWN session: the user signs in on grid's
// own form, grid's backend verifies the credentials against the Gateway and
// returns the Gateway-issued token, which we store here (separate from the
// Gateway's own gw_token). So you can be a different Gateway user on each app.

import { API_BASE } from './api'

const TOKEN_KEY = 'sb_token'

export interface AuthUser {
  id: number
  email: string
  form_filled: boolean
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}
export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t)
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/** Sign in against grid's login proxy (which verifies against the Gateway).
 * Stores the returned token and returns the user. Throws on bad credentials. */
export async function login(email: string, password: string): Promise<AuthUser> {
  const r = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!r.ok) {
    let detail = 'Sign in failed'
    try {
      detail = (await r.json()).detail ?? detail
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  const data = await r.json()
  setToken(data.token)
  return data.user as AuthUser
}

export function logout(): void {
  clearToken()
  window.location.reload()
}
