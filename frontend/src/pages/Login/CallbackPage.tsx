import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles } from 'lucide-react'
import api from '@/lib/api'
import { useAuthStore } from '@/store/authStore'
import { completeLogin, readClaims } from '@/lib/pkce'

/**
 * Where Cognito sends the browser back to, carrying `?code=`.
 *
 * Swaps the code for tokens using the verifier this tab stashed before
 * leaving, reads the display claims out of the ID token, and hands off to the
 * app. Failure lands the user back on the sign-in page with a reason rather
 * than on a blank screen — an abandoned or replayed login is a normal thing to
 * happen, not an exception.
 */
export default function CallbackPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [error, setError] = useState<string | null>(null)
  // React runs effects twice in development StrictMode. A code is single-use,
  // so the second attempt would fail against Cognito and overwrite a login
  // that had already succeeded.
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return
    started.current = true

    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    const state = params.get('state')

    // Cognito reports its own failures here too, e.g. a cancelled login.
    const oauthError = params.get('error')
    if (oauthError) {
      setError(params.get('error_description') || oauthError)
      return
    }
    if (!code) {
      setError('No authorization code in the response.')
      return
    }

    ;(async () => {
      try {
        const cfg = (await api.get('/api/auth/config')).data
        if (!cfg?.hosted_ui) throw new Error('This deployment is not using hosted sign-in.')

        const tokens = await completeLogin(
          {
            domain: cfg.domain,
            client_id: cfg.client_id,
            callback_path: cfg.callback_path ?? '/auth/callback',
          },
          code,
          state,
        )

        // The ID token, not the access token. The API needs `custom:role` and
        // `custom:department`, which Cognito puts only on the ID token, and
        // `cognito_auth.verify` asserts `token_use == "id"` rather than
        // inferring it — so sending the wrong one fails loudly.
        const claims = readClaims(tokens.id_token) as Record<string, string>
        setAuth({
          token: tokens.id_token,
          username: claims['cognito:username'] || claims.email || 'user',
          role: claims['custom:role'] || '',
          department: claims['custom:department'] || '',
        })

        // Drop the code from the address bar before the app renders, so a
        // refresh does not retry a code that has already been spent.
        window.history.replaceState({}, '', '/home')
        navigate('/home', { replace: true })
      } catch (err: any) {
        setError(err?.message || 'Sign in could not be completed.')
      }
    })()
  }, [navigate, setAuth])

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{
        background:
          'radial-gradient(circle at 30% 20%, rgba(0,73,119,0.10), transparent 60%), radial-gradient(circle at 80% 80%, rgba(8,145,178,0.10), transparent 60%), var(--bg-page)',
      }}
    >
      <div className="w-full max-w-md mx-auto px-6 text-center">
        <div
          className="inline-flex w-12 h-12 rounded-2xl items-center justify-center mb-4"
          style={{ background: 'linear-gradient(135deg, var(--accent), var(--teal))' }}
        >
          <Sparkles size={20} color="#fff" />
        </div>

        {error ? (
          <div className="panel">
            <div
              className="mb-4 px-3 py-2 rounded-lg text-xs"
              style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
            >
              {error}
            </div>
            <button
              type="button"
              onClick={() => navigate('/login', { replace: true })}
              className="w-full py-2.5 rounded-lg text-sm font-semibold"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              Back to sign in
            </button>
          </div>
        ) : (
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Completing sign in…
          </p>
        )}
      </div>
    </div>
  )
}
