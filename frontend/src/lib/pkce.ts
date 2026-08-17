/**
 * PKCE — Proof Key for Code Exchange.
 *
 * The authorization-code flow hands the browser a short-lived `code` in the
 * address bar, which then gets exchanged for tokens. That code passes through
 * browser history and anywhere a URL is logged, so whoever redeems it has to
 * prove they are the app that *started* the flow. A server proves it with a
 * stored client secret. A static bundle served from a CDN cannot: everything
 * it ships is readable by anyone who opens devtools.
 *
 * So instead of a secret that lives in the code, PKCE invents one per login:
 *
 *   1. generate a random `verifier`, keep it in this tab only
 *   2. send SHA-256(verifier) — the `challenge` — when starting the flow
 *   3. send the original `verifier` when redeeming the code
 *   4. the server hashes it and compares
 *
 * A stolen code is useless without the verifier, and the verifier never leaves
 * the browser. Note what this does *not* do: it does not protect the tokens
 * once issued. Those live in the same place they always have.
 */

const VERIFIER_KEY = 'cma-pkce-verifier'
const STATE_KEY = 'cma-pkce-state'

function randomUrlSafe(bytes: number): string {
  const raw = new Uint8Array(bytes)
  crypto.getRandomValues(raw)
  return base64UrlEncode(raw)
}

/** base64url: base64 with the URL-hostile characters swapped and no padding. */
function base64UrlEncode(bytes: Uint8Array): string {
  let binary = ''
  bytes.forEach((b) => {
    binary += String.fromCharCode(b)
  })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function sha256(text: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return new Uint8Array(digest)
}

/**
 * Start a login. Returns the URL to send the browser to, having stashed the
 * verifier where the callback can find it.
 *
 * sessionStorage rather than localStorage: this is per-tab and should not
 * outlive the tab that started the flow. A verifier left lying around in
 * localStorage after an abandoned login is a small thing, but it is a secret
 * with no reason to persist.
 */
export async function beginLogin(cfg: {
  domain: string
  client_id: string
  scopes: string[]
  callback_path: string
}): Promise<string> {
  const verifier = randomUrlSafe(32)
  const state = randomUrlSafe(16)
  sessionStorage.setItem(VERIFIER_KEY, verifier)
  sessionStorage.setItem(STATE_KEY, state)

  const challenge = base64UrlEncode(await sha256(verifier))
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: cfg.client_id,
    redirect_uri: window.location.origin + cfg.callback_path,
    scope: cfg.scopes.join(' '),
    code_challenge_method: 'S256',
    code_challenge: challenge,
    // Round-tripped and checked on the way back. Without it, an attacker can
    // feed a victim's browser a code of their own choosing and log them into
    // the attacker's account — the login succeeds, which is what makes it
    // easy to miss.
    state,
  })
  return `${cfg.domain}/oauth2/authorize?${params.toString()}`
}

/**
 * Finish a login: swap the code for tokens.
 *
 * No client secret in this request, which is the whole point — the
 * `code_verifier` takes its place. Cognito's token endpoint accepts a form
 * body, not JSON.
 */
export async function completeLogin(
  cfg: { domain: string; client_id: string; callback_path: string },
  code: string,
  returnedState: string | null,
): Promise<{ id_token: string; access_token: string }> {
  const expectedState = sessionStorage.getItem(STATE_KEY)
  const verifier = sessionStorage.getItem(VERIFIER_KEY)
  sessionStorage.removeItem(STATE_KEY)
  sessionStorage.removeItem(VERIFIER_KEY)

  if (!verifier) {
    throw new Error(
      'No login in progress in this tab. Start again from the sign-in page.',
    )
  }
  if (!expectedState || returnedState !== expectedState) {
    throw new Error('Sign-in response did not match this browser session.')
  }

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: cfg.client_id,
    code,
    redirect_uri: window.location.origin + cfg.callback_path,
    code_verifier: verifier,
  })

  const response = await fetch(`${cfg.domain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!response.ok) {
    throw new Error(`Token exchange failed (${response.status}). ${await response.text()}`)
  }
  return response.json()
}

/**
 * Read the claims out of an ID token without verifying it.
 *
 * Safe *here* and nowhere else: this only decides what name to show in the
 * corner. Every request still carries the token, and the backend verifies the
 * signature against the pool's public keys before trusting a single claim.
 * Client-side decoding is for display; it is never authorisation.
 */
export function readClaims(idToken: string): Record<string, unknown> {
  const payload = idToken.split('.')[1]
  const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'))
  return JSON.parse(
    decodeURIComponent(
      json
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join(''),
    ),
  )
}
