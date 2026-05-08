import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowRight, Briefcase, LineChart, Droplet, ShieldAlert, Banknote,
  Building2, Activity, FileSpreadsheet, Plus, Trash2,
  BarChart3, FlaskConical, Boxes, ListChecks, FileBarChart, Database,
} from 'lucide-react'
import api from '@/lib/api'
import { useAuthStore } from '@/store/authStore'
import type { BusinessFunction } from '@/types'
import NewWorkspaceDrawer from './NewWorkspaceDrawer'

const ICONS: Record<string, any> = {
  briefcase: Briefcase,
  'line-chart': LineChart,
  'bar-chart': BarChart3,
  droplet: Droplet,
  'shield-alert': ShieldAlert,
  banknote: Banknote,
  'building-2': Building2,
  activity: Activity,
  'file-spreadsheet': FileSpreadsheet,
  flask: FlaskConical,
  boxes: Boxes,
  checks: ListChecks,
  'file-bar-chart': FileBarChart,
  database: Database,
}

export default function HomePage() {
  const { username, role, department } = useAuthStore()
  const navigate = useNavigate()
  const [functions, setFunctions] = useState<BusinessFunction[]>([])
  const [loading, setLoading] = useState(true)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  useEffect(() => {
    api.get<BusinessFunction[]>('/api/functions')
      .then((r) => setFunctions(r.data))
      .finally(() => setLoading(false))
  }, [])

  const handleDelete = async (fn: BusinessFunction) => {
    if (!window.confirm(`Delete workspace "${fn.name}"?\n\nThis removes it from the list. Any data, models, or plots stored in it will no longer be accessible.`)) return
    try {
      await api.delete(`/api/functions/${fn.id}`)
      setFunctions((prev) => prev.filter((f) => f.id !== fn.id))
    } catch (e: any) {
      alert(e?.response?.data?.detail || 'Could not delete workspace.')
    }
  }

  const greeting = (() => {
    const h = new Date().getHours()
    if (h < 12) return 'Good morning'
    if (h < 18) return 'Good afternoon'
    return 'Good evening'
  })()

  // Group by topic, preserving the order the backend listed categories in
  const byCategory: Record<string, BusinessFunction[]> = {}
  const categoryOrder: string[] = []
  for (const f of functions) {
    if (!(f.category in byCategory)) {
      byCategory[f.category] = []
      categoryOrder.push(f.category)
    }
    byCategory[f.category].push(f)
  }

  return (
    <div className="max-w-[1320px] mx-auto animate-fade-in">
      <div className="mb-8">
        <h1 className="page-header">
          {greeting}, {username}.
        </h1>
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          Pick a function below to open its workspace. Each one ships with default analytical
          views and a domain-aware AI agent. Configure data sources, agent skills, and your own
          plots from <button onClick={() => navigate('/settings')} style={{ color: 'var(--accent)', fontWeight: 600 }}>Settings</button>.
        </p>
      </div>

      {/* User context strip */}
      <div className="panel mb-8 flex items-center gap-6 flex-wrap">
        <ContextField label="Analyst" value={username || ''} />
        <Sep />
        <ContextField label="Role" value={role || ''} />
        <Sep />
        <ContextField label="Department" value={department || ''} />
        <Sep />
        <ContextField label="As of" value={new Date().toLocaleDateString()} />
      </div>

      {loading && <div style={{ color: 'var(--text-muted)' }}>Loading functions…</div>}

      {/* Create — placeholder entry point for future workspace authoring.
          The click handler is a stub today; wire it up once the new-workspace
          flow (function scaffolding + data/model/skill picker) lands. */}
      {!loading && (
        <section className="mb-10">
          <div
            className="flex items-center gap-2 mb-4"
            style={{ color: 'var(--text-secondary)' }}
          >
            <span
              style={{
                fontSize: 11, fontWeight: 700, letterSpacing: '0.12em',
                textTransform: 'uppercase',
              }}
            >
              Create
            </span>
            <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <button
              onClick={() => setDrawerOpen(true)}
              className="text-left transition-all group flex flex-col items-start"
              style={{
                background: 'transparent',
                border: '1.5px dashed var(--border)',
                borderRadius: 12,
                padding: 20,
                minHeight: 168,
                color: 'var(--text-secondary)',
              }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.borderColor = 'var(--accent)'
                el.style.color = 'var(--accent)'
                el.style.background = 'var(--accent-light)'
                el.style.transform = 'translateY(-2px)'
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.borderColor = 'var(--border)'
                el.style.color = 'var(--text-secondary)'
                el.style.background = 'transparent'
                el.style.transform = 'translateY(0)'
              }}
            >
              <div className="flex items-start justify-between mb-3 w-full">
                <div
                  className="w-10 h-10 rounded-xl flex items-center justify-center"
                  style={{ background: 'var(--bg-elevated)' }}
                >
                  <Plus size={18} />
                </div>
                <ArrowRight
                  size={16}
                  className="transition-transform group-hover:translate-x-1"
                  style={{ color: 'var(--text-muted)' }}
                />
              </div>
              <div className="font-display text-base font-semibold mb-1">
                Add a new workspace
              </div>
              <p
                className="text-xs"
                style={{ lineHeight: 1.55, color: 'var(--text-muted)' }}
              >
                Stand up a new business function with its own data sources, models, agent skills, and starter tiles.
              </p>
            </button>
          </div>
        </section>
      )}

      {categoryOrder.map((cat) => {
        const fns = byCategory[cat]
        return (
        <section key={cat} className="mb-10">
          <div
            className="flex items-center gap-2 mb-4"
            style={{ color: 'var(--text-secondary)' }}
          >
            <span
              style={{
                fontSize: 11, fontWeight: 700, letterSpacing: '0.12em',
                textTransform: 'uppercase',
              }}
            >
              {cat}
            </span>
            <div
              className="flex-1 h-px"
              style={{ background: 'var(--border)' }}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fns.length} functions</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {fns.map((f) => {
              const Icon = ICONS[f.icon] || Briefcase
              const isHovered = hoveredId === f.id
              return (
                <div
                  key={f.id}
                  onClick={() => navigate(`/workspace/${f.id}`)}
                  className="text-left transition-all"
                  style={{
                    background: 'var(--bg-card)',
                    border: '1px solid var(--border)',
                    borderRadius: 12,
                    padding: 20,
                    cursor: 'pointer',
                    transition: 'border-color 0.15s, transform 0.15s, box-shadow 0.15s',
                  }}
                  onMouseEnter={(e) => {
                    setHoveredId(f.id)
                    const el = e.currentTarget as HTMLElement
                    el.style.borderColor = f.color
                    el.style.transform = 'translateY(-2px)'
                    el.style.boxShadow = `0 12px 28px ${f.color}1F`
                  }}
                  onMouseLeave={(e) => {
                    setHoveredId(null)
                    const el = e.currentTarget as HTMLElement
                    el.style.borderColor = 'var(--border)'
                    el.style.transform = 'translateY(0)'
                    el.style.boxShadow = 'none'
                  }}
                >
                  <div className="flex items-start justify-between mb-3">
                    <div
                      className="w-10 h-10 rounded-xl flex items-center justify-center"
                      style={{ background: `${f.color}1A`, color: f.color }}
                    >
                      <Icon size={18} />
                    </div>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(f) }}
                        title="Delete workspace"
                        className="p-1.5 rounded-lg"
                        style={{
                          color: 'var(--error)',
                          opacity: isHovered ? 1 : 0,
                          pointerEvents: isHovered ? 'auto' : 'none',
                          transition: 'opacity 0.15s',
                        }}
                      >
                        <Trash2 size={14} />
                      </button>
                      <ArrowRight
                        size={16}
                        style={{ color: 'var(--text-muted)', transition: 'transform 0.15s', transform: isHovered ? 'translateX(2px)' : 'none' }}
                      />
                    </div>
                  </div>
                  <div className="font-display text-base font-semibold mb-1" style={{ color: 'var(--text-primary)' }}>
                    {f.name}
                  </div>
                  <p
                    className="text-xs"
                    style={{ color: 'var(--text-secondary)', lineHeight: 1.55 }}
                  >
                    {f.description}
                  </p>
                </div>
              )
            })}
          </div>
        </section>
        )
      })}

      <NewWorkspaceDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        existingCategories={categoryOrder}
        onCreated={(fn) => {
          // Append optimistically so the home grid reflects the new
          // workspace immediately if the user navigates back here.
          setFunctions((prev) => [...prev, fn])
        }}
      />
    </div>
  )
}

function ContextField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="metric-label">{label}</div>
      <div className="text-sm font-semibold mt-0.5" style={{ color: 'var(--text-primary)' }}>
        {value}
      </div>
    </div>
  )
}

function Sep() {
  return <div className="h-8 w-px" style={{ background: 'var(--border)' }} />
}
