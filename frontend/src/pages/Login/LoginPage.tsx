import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, ShieldCheck } from 'lucide-react'
import api from '@/lib/api'
import { useAuthStore } from '@/store/authStore'
import { beginLogin } from '@/lib/pkce'

/**
 * Two ways in, and the deployment decides which.
 *
 * With a Cognito Hosted UI configured, this page never sees a password — it
 * sends the browser to Cognito and waits to be redirected back with a code.
 * Without one (local development, or a stack deployed without a domain) it
 * falls back to the form, which posts credentials to the API.
 *
 * The choice is fetched rather than compiled in. A static bundle is built once
 * and served everywhere, and the pool and client ids are chosen by
 * CloudFormation, so they are not knowable at build time.
 */
type AuthConfig = {
  mode: string
  hosted_ui: boolean
  domain?: string
  client_id?: string
  scopes?: string[]
  callback_path?: string
}

export default function LoginPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api
      .get('/api/auth/config')
      .then((r) => setConfig(r.data))
      // A config the app cannot read should not be a blank page. Falling back
      // to the form means a broken endpoint degrades to the older path rather
      // than to no path at all.
      .catch(() => setConfig({ mode: 'password', hosted_ui: false }))
  }, [])

  const startHostedLogin = async () => {
    setError(null)
    setLoading(true)
    try {
      window.location.href = await beginLogin({
        domain: config!.domain!,
        client_id: config!.client_id!,
        scopes: config!.scopes ?? ['openid', 'email', 'profile'],
        callback_path: config!.callback_path ?? '/auth/callback',
      })
    } catch (err: any) {
      setError(err?.message || 'Could not start sign in')
      setLoading(false)
    }
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const r = await api.post('/api/auth/login', { username, password })
      setAuth({
        token: r.data.token,
        username: r.data.username,
        role: r.data.role,
        department: r.data.department,
      })
      navigate('/home')
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Sign in failed')
    } finally {
      setLoading(false)
    }
  }

  const errorBox = error && (
    <div
      className="mb-4 px-3 py-2 rounded-lg text-xs"
      style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
    >
      {error}
    </div>
  )

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{
        background:
          'radial-gradient(circle at 30% 20%, rgba(0,73,119,0.10), transparent 60%), radial-gradient(circle at 80% 80%, rgba(8,145,178,0.10), transparent 60%), var(--bg-page)',
      }}
    >
      <div className="w-full max-w-md mx-auto px-6">
        <div className="text-center mb-8">
          <div
            className="inline-flex w-12 h-12 rounded-2xl items-center justify-center mb-4"
            style={{ background: 'linear-gradient(135deg, var(--accent), var(--teal))' }}
          >
            <Sparkles size={20} color="#fff" />
          </div>
          <h1 className="font-display text-3xl mb-1" style={{ color: 'var(--text-primary)' }}>
            CMA Workbench
          </h1>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Self-serve analytics for Capital Markets &amp; Finance
          </p>
        </div>

        {config === null ? (
          <div
            className="panel text-center text-sm"
            style={{ color: 'var(--text-muted)', boxShadow: '0 24px 48px rgba(0,0,0,0.06)' }}
          >
            Loading…
          </div>
        ) : config.hosted_ui ? (
          <div className="panel" style={{ boxShadow: '0 24px 48px rgba(0,0,0,0.06)' }}>
            <div className="flex items-start gap-3 mb-4">
              <ShieldCheck size={18} style={{ color: 'var(--teal)', marginTop: 2 }} />
              <p className="text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
                You&apos;ll sign in on your organisation&apos;s identity provider. Your
                password is never sent to the workbench.
              </p>
            </div>

            {errorBox}

            <button
              type="button"
              onClick={startHostedLogin}
              disabled={loading}
              className="w-full py-2.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              {loading ? 'Redirecting…' : 'Continue to sign in'}
            </button>
          </div>
        ) : (
          <form
            onSubmit={submit}
            className="panel"
            style={{ boxShadow: '0 24px 48px rgba(0,0,0,0.06)' }}
          >
            <label className="block mb-3">
              <span
                className="block text-[11px] font-semibold uppercase tracking-widest mb-1"
                style={{ color: 'var(--text-secondary)' }}
              >
                Username
              </span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
              />
            </label>
            <label className="block mb-4">
              <span
                className="block text-[11px] font-semibold uppercase tracking-widest mb-1"
                style={{ color: 'var(--text-secondary)' }}
              >
                Password
              </span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
              />
            </label>

            {errorBox}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
