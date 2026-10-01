import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import {
  clearToken,
  getMe,
  getToken,
  setToken,
  signin as apiSignin,
  signup as apiSignup,
  type AuthUser,
} from '../api'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  signup: (email: string, password: string) => Promise<AuthUser>
  signin: (email: string, password: string) => Promise<AuthUser>
  signout: () => void
  refreshMe: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  // On mount: if we hold a token, hydrate the user. A bad/expired token clears itself.
  useEffect(() => {
    let cancelled = false
    async function hydrate() {
      if (!getToken()) {
        setLoading(false)
        return
      }
      try {
        const me = await getMe()
        if (!cancelled) setUser(me)
      } catch {
        clearToken()
        if (!cancelled) setUser(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    hydrate()
    return () => {
      cancelled = true
    }
  }, [])

  async function signup(email: string, password: string): Promise<AuthUser> {
    const res = await apiSignup(email, password)
    setToken(res.token)
    setUser(res.user)
    return res.user
  }

  async function signin(email: string, password: string): Promise<AuthUser> {
    const res = await apiSignin(email, password)
    setToken(res.token)
    setUser(res.user)
    return res.user
  }

  function signout(): void {
    clearToken()
    setUser(null)
  }

  async function refreshMe(): Promise<void> {
    const me = await getMe()
    setUser(me)
  }

  return (
    <AuthContext.Provider value={{ user, loading, signup, signin, signout, refreshMe }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
