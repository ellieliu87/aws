import { useEffect, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import {
  Home, Settings, LogOut, Sparkles,
  LayoutDashboard, Database, Boxes, FlaskConical, BarChart3,
  ListChecks, FileBarChart,
} from 'lucide-react'
import { useAuthStore } from '@/store/authStore'
import api from '@/lib/api'
import { cn } from '@/lib/utils'
import type { BusinessFunction } from '@/types'

const WORKSPACE_TABS = [
  { id: 'overview',  label: 'Overview',  icon: LayoutDashboard },
  { id: 'data',      label: 'Data',      icon: Database },
  { id: 'models',    label: 'Models',    icon: Boxes },
  { id: 'workflow',  label: 'Workflow',  icon: FlaskConical },
  { id: 'playbooks', label: 'Playbooks', icon: ListChecks },
  { id: 'analytics', label: 'Analytics', icon: BarChart3 },
  { id: 'reporting', label: 'Reporting', icon: FileBarChart },
] as const

// Remember the last workspace the user visited so the sidebar's tab nav
// stays visible when they pop over to Settings (or any non-workspace
// route). Persisted to sessionStorage so a hard reload mid-session keeps
// the menu; clears on tab close.
const LAST_FN_KEY = 'cma:sidebar:lastFnId'

function loadLastFnId(): string | null {
  try { return sessionStorage.getItem(LAST_FN_KEY) } catch { return null }
}
function saveLastFnId(id: string | null) {
  try {
    if (id) sessionStorage.setItem(LAST_FN_KEY, id)
    else sessionStorage.removeItem(LAST_FN_KEY)
  } catch {}
}

export default function Sidebar() {
  const { username, role, department, logout } = useAuthStore()
  const navigate = useNavigate()
  const location = useLocation()
  const [activeFunction, setActiveFunction] = useState<BusinessFunction | null>(null)
  const [lastFnId, setLastFnId] = useState<string | null>(() => loadLastFnId())

  // The function (if any) the user is currently inside.
  const fnMatch = location.pathname.match(/^\/workspace\/([^/]+)/)
  const currentFnId = fnMatch ? fnMatch[1] : null
  const tabMatch = location.pathname.match(/^\/workspace\/[^/]+\/([^/]+)/)
  const currentTab = tabMatch ? tabMatch[1] : (currentFnId ? 'overview' : null)

  // The function whose tabs we should display. When the user navigates
  // away from /workspace (e.g. Settings) we keep the previous workspace
  // visible so the analyst can pop back with one click instead of
  // re-picking from Home — except when they're actually on Home, where
  // the function-picker is the surface, so workspace-specific tabs would
  // be redundant noise.
  const onHome = location.pathname === '/home'
  const fnId = onHome ? null : (currentFnId || lastFnId)

  // Track the last workspace seen so it sticks across tab changes.
  useEffect(() => {
    if (!currentFnId || currentFnId === lastFnId) return
    setLastFnId(currentFnId)
    saveLastFnId(currentFnId)
  }, [currentFnId, lastFnId])

  useEffect(() => {
    if (!fnId) { setActiveFunction(null); return }
    api.get<BusinessFunction>(`/api/functions/${fnId}`)
      .then((r) => setActiveFunction(r.data))
      .catch(() => setActiveFunction(null))
  }, [fnId])

  const handleLogout = async () => {
    // Revoke the refresh token first, while the access token is still valid.
    try { await api.post('/api/auth/logout') } catch {}
    logout()

    // With hosted sign-in there is a second session to end: Cognito's own,
    // which lives in a cookie on its domain. Skipping this clears the app and
    // leaves the next "sign in" completing instantly against a session the
    // user thought they had ended — which looks like logout silently failing.
    try {
      const cfg = (await api.get('/api/auth/config')).data
      if (cfg?.hosted_ui) {
        const params = new URLSearchParams({
          client_id: cfg.client_id,
          logout_uri: window.location.origin + (cfg.logout_path ?? '/login'),
        })
        window.location.href = `${cfg.domain}/logout?${params.toString()}`
        return
      }
    } catch {}

    navigate('/login')
  }

  const initials = (username || 'U').slice(0, 2).toUpperCase()

  return (
    <aside
      className="w-[240px] shrink-0 flex flex-col h-screen"
      style={{ background: 'var(--bg-card)', borderRight: '1px solid var(--border)' }}
    >
      {/* Brand */}
      <div className="px-5 pt-5 pb-4" style={{ borderBottom: '1px solid var(--border)' }}>
        <div className="flex items-center gap-2">
          <div
            className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: 'linear-gradient(135deg, var(--accent), var(--teal))' }}
          >
            <Sparkles size={14} color="#fff" />
          </div>
          <div>
            <div className="font-display text-lg leading-none" style={{ color: 'var(--text-primary)' }}>
              CMA Workbench
            </div>
            <div
              className="font-mono mt-0.5"
              style={{ fontSize: '0.6rem', color: 'var(--text-muted)', letterSpacing: '0.1em', textTransform: 'uppercase' }}
            >
              Capital One · Self-Serve
            </div>
          </div>
        </div>
      </div>

      <nav className="flex-1 px-3 pt-3 pb-2 space-y-0.5 overflow-y-auto">
        <div className="px-2 pb-1.5 section-title">Main</div>
        <NavItem to="/home" label="Home" icon={Home} active={location.pathname === '/home'} />

        {fnId && (
          <>
            <div
              className="px-2 pt-3 pb-1.5 section-title flex items-center justify-between"
              title="The function this workspace belongs to. Click Home to switch."
            >
              <span className="truncate">
                {activeFunction?.short_name || activeFunction?.name || fnId.replace(/_/g, ' ')}
              </span>
            </div>
            {WORKSPACE_TABS.map(({ id, label, icon }) => (
              <NavItem
                key={id}
                to={`/workspace/${fnId}/${id}`}
                label={label}
                icon={icon}
                active={currentTab === id}
                accent={activeFunction?.color}
              />
            ))}
          </>
        )}

        <div className="px-2 pt-3 pb-1.5 section-title">Configuration</div>
        <NavItem
          to="/settings"
          label="Settings"
          icon={Settings}
          active={location.pathname.startsWith('/settings')}
        />
      </nav>

      {/* User footer */}
      <div className="px-4 py-4" style={{ borderTop: '1px solid var(--border)' }}>
        <div className="flex items-center gap-2.5">
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center shrink-0 text-xs font-bold"
            style={{ background: 'linear-gradient(135deg, var(--accent), var(--teal))', color: '#fff' }}
          >
            {initials}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
              {username || 'User'}
            </div>
            <div className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>
              {role || 'Analyst'} · {department || 'Capital Markets'}
            </div>
          </div>
          <button
            onClick={handleLogout}
            className="p-1.5 rounded-lg transition-colors shrink-0"
            style={{ color: 'var(--text-muted)' }}
            onMouseEnter={(e) => {
              const el = e.currentTarget
              el.style.color = 'var(--error)'
              el.style.background = 'var(--error-bg)'
            }}
            onMouseLeave={(e) => {
              const el = e.currentTarget
              el.style.color = 'var(--text-muted)'
              el.style.background = 'transparent'
            }}
            title="Logout"
          >
            <LogOut size={13} />
          </button>
        </div>
      </div>
    </aside>
  )
}

function NavItem({
  to, label, icon: Icon, active, accent,
}: {
  to: string
  label: string
  icon: any
  active?: boolean
  accent?: string
}) {
  return (
    <NavLink
      to={to}
      className={cn(
        'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150 border-l-2',
      )}
      style={
        active
          ? {
              background: 'var(--accent-light)',
              borderLeftColor: accent || 'var(--accent)',
              color: 'var(--accent)',
              fontWeight: 600,
            }
          : { borderLeftColor: 'transparent', color: 'var(--text-secondary)' }
      }
      onMouseEnter={(e) => {
        if (active) return
        const el = e.currentTarget as HTMLElement
        el.style.color = 'var(--text-primary)'
        el.style.background = 'var(--bg-elevated)'
      }}
      onMouseLeave={(e) => {
        if (active) return
        const el = e.currentTarget as HTMLElement
        el.style.color = 'var(--text-secondary)'
        el.style.background = 'transparent'
      }}
    >
      <Icon size={15} className="shrink-0" />
      <span className="truncate">{label}</span>
    </NavLink>
  )
}
