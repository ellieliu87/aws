import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  X, Briefcase, LineChart, ShieldAlert, Banknote, Building2, Activity,
  FileSpreadsheet, BarChart3, FlaskConical, Boxes, ListChecks, FileBarChart,
  Database, ArrowRight, Sparkles, ChevronLeft, ChevronRight, BookOpen,
  Wrench, Brain, Check,
} from 'lucide-react'
import api from '@/lib/api'
import type { BusinessFunction } from '@/types'

// ── Category presets — each drives default icon + color ──────────────────
const CATEGORY_PRESETS = [
  { id: 'Treasury',          icon: 'banknote',      color: '#004977', desc: 'ALM, funding, liquidity' },
  { id: 'Capital',           icon: 'building-2',    color: '#D97706', desc: 'CET1, RWA, capital actions' },
  { id: 'CCAR',              icon: 'bar-chart',     color: '#7C3AED', desc: 'Stress testing, PPNR, submissions' },
  { id: 'Markets & Risk',    icon: 'activity',      color: '#FF5C5C', desc: 'VaR, sensitivities, attribution' },
  { id: 'Stress Testing',    icon: 'flask',         color: '#059669', desc: 'Scenario analysis, macro stress' },
  { id: 'Counterparty Risk', icon: 'shield-alert',  color: '#0891B2', desc: 'XVA, exposure, cleared/bilateral' },
  { id: 'Other',             icon: 'briefcase',     color: '#6366F1', desc: 'Custom analytical domain' },
]

// ── Icon picker options ────────────────────────────────────────────────────
const ICON_OPTIONS: { id: string; Icon: any; label: string }[] = [
  { id: 'briefcase',        Icon: Briefcase,       label: 'Briefcase'  },
  { id: 'line-chart',       Icon: LineChart,       label: 'Line chart' },
  { id: 'bar-chart',        Icon: BarChart3,       label: 'Bar chart'  },
  { id: 'activity',         Icon: Activity,        label: 'Activity'   },
  { id: 'shield-alert',     Icon: ShieldAlert,     label: 'Risk'       },
  { id: 'banknote',         Icon: Banknote,        label: 'Treasury'   },
  { id: 'building-2',       Icon: Building2,       label: 'Capital'    },
  { id: 'file-spreadsheet', Icon: FileSpreadsheet, label: 'Reporting'  },
  { id: 'flask',            Icon: FlaskConical,    label: 'Workflow'   },
  { id: 'boxes',            Icon: Boxes,           label: 'Models'     },
  { id: 'checks',           Icon: ListChecks,      label: 'Playbooks'  },
  { id: 'file-bar-chart',   Icon: FileBarChart,    label: 'Reports'    },
  { id: 'database',         Icon: Database,        label: 'Data'       },
]

// ── Color palette ──────────────────────────────────────────────────────────
const COLOR_PRESETS = [
  '#004977', '#0891B2', '#7C3AED', '#DC2626', '#059669',
  '#D97706', '#FF5C5C', '#6366F1', '#0F766E', '#E11D48',
]

// ── Domain packs available for import ─────────────────────────────────────
const DOMAIN_PACKS = [
  {
    id: 'deposits',
    label: 'Deposits',
    color: '#004977',
    desc: 'Retail & commercial deposit attribution, rate models, and BHCS stress analytics.',
    components: [
      { id: 'knowledge_base', label: 'Knowledge Base', Icon: BookOpen, desc: 'Whitepapers, methodology docs, model documentation' },
      { id: 'agent_skills',   label: 'Agent Skills',   Icon: Brain,   desc: 'Variance analyst, methodology researcher, commentary drafter' },
      { id: 'agent_tools',    label: 'Agent Tools',    Icon: Wrench,  desc: 'Data connectors, compute tools, audit logic rules' },
    ],
  },
  {
    id: 'portfolio',
    label: 'Portfolio',
    color: '#7C3AED',
    desc: 'Investment portfolio OAS/OAD analytics, sector allocation, risk limits, and duration management.',
    components: [
      { id: 'knowledge_base', label: 'Knowledge Base', Icon: BookOpen, desc: 'Valuation methodology, risk factor documentation' },
      { id: 'agent_skills',   label: 'Agent Skills',   Icon: Brain,   desc: 'Portfolio analyst, sector reviewer, attribution agent' },
      { id: 'agent_tools',    label: 'Agent Tools',    Icon: Wrench,  desc: 'Bond analytics, OAS calculators, benchmark tools' },
    ],
  },
]

interface Props {
  open: boolean
  onClose: () => void
  existingCategories?: string[]   // kept for API compatibility; modal uses preset categories
  onCreated?: (fn: BusinessFunction) => void
}

export default function NewWorkspaceModal({ open, onClose, onCreated }: Props) {
  const navigate = useNavigate()
  const [step, setStep] = useState<1 | 2>(1)

  // Step 1 state
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState(CATEGORY_PRESETS[0].id)
  const [customCategory, setCustomCategory] = useState('')
  const [iconId, setIconId] = useState(CATEGORY_PRESETS[0].icon)
  const [color, setColor] = useState(CATEGORY_PRESETS[0].color)
  const [iconDirty, setIconDirty] = useState(false)
  const [colorDirty, setColorDirty] = useState(false)

  // Step 2 state: packId → Set of selected component ids
  const [packImports, setPackImports] = useState<Record<string, Set<string>>>({})

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setStep(1)
    setName(''); setDescription('')
    setCategory(CATEGORY_PRESETS[0].id)
    setCustomCategory('')
    setIconId(CATEGORY_PRESETS[0].icon)
    setColor(CATEGORY_PRESETS[0].color)
    setIconDirty(false); setColorDirty(false)
    setPackImports({})
    setSaving(false); setError(null)
  }, [open])

  const handleCategorySelect = (catId: string) => {
    setCategory(catId)
    const preset = CATEGORY_PRESETS.find((c) => c.id === catId)
    if (preset) {
      if (!iconDirty) setIconId(preset.icon)
      if (!colorDirty) setColor(preset.color)
    }
  }

  const previewId = useMemo(() => {
    const s = name.trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '')
    return s || '(derived from name)'
  }, [name])

  const effectiveCategory = category === 'Other'
    ? (customCategory.trim() || 'Other')
    : category

  const ActiveIcon = ICON_OPTIONS.find((i) => i.id === iconId)?.Icon || Briefcase

  const toggleComponent = (packId: string, compId: string) => {
    setPackImports((prev) => {
      const s = new Set(prev[packId] || [])
      if (s.has(compId)) s.delete(compId); else s.add(compId)
      return { ...prev, [packId]: s }
    })
  }

  const toggleAllPack = (packId: string, allIds: string[]) => {
    setPackImports((prev) => {
      const s = prev[packId] || new Set<string>()
      const allOn = allIds.every((c) => s.has(c))
      return { ...prev, [packId]: allOn ? new Set() : new Set(allIds) }
    })
  }

  const totalSelected = Object.values(packImports).reduce((n, s) => n + s.size, 0)

  const submit = async () => {
    if (name.trim().length < 2) { setError('Name needs at least 2 characters.'); return }
    setSaving(true); setError(null)
    try {
      const importedPacks = Object.entries(packImports)
        .filter(([, s]) => s.size > 0)
        .map(([packId, s]) => ({ pack_id: packId, components: Array.from(s) }))

      const r = await api.post<BusinessFunction>('/api/functions', {
        name: name.trim(),
        description: description.trim(),
        category: effectiveCategory,
        icon: iconId,
        color,
        imported_packs: importedPacks,
      })
      onCreated?.(r.data)
      onClose()
      navigate(`/workspace/${r.data.id}`)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Could not create workspace.')
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11,15,25,0.55)' }}
        onClick={onClose}
      />

      {/* Modal */}
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          className="w-full flex flex-col"
          style={{
            maxWidth: 600,
            maxHeight: '92vh',
            background: 'var(--bg-card)',
            borderRadius: 16,
            border: '1px solid var(--border)',
            boxShadow: '0 24px 64px rgba(0,0,0,0.28)',
            overflow: 'hidden',
          }}
        >
          {/* ── Header ── */}
          <div
            className="px-6 py-4 flex items-center justify-between shrink-0"
            style={{
              background: `linear-gradient(135deg, ${color}, color-mix(in srgb, ${color} 65%, #0891B2))`,
              borderBottom: '1px solid rgba(255,255,255,0.10)',
            }}
          >
            <div className="flex items-center gap-3">
              <div
                className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0"
                style={{ background: 'rgba(255,255,255,0.20)' }}
              >
                <ActiveIcon size={16} color="#fff" />
              </div>
              <div>
                <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>
                  New workspace
                </div>
                <div className="font-mono" style={{ fontSize: 10, color: 'rgba(255,255,255,0.70)' }}>
                  {step === 1 ? previewId : effectiveCategory}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-3">
              {/* Step indicator */}
              <div className="flex items-center gap-1.5">
                {([1, 2] as const).map((s) => (
                  <div
                    key={s}
                    className="rounded-full transition-all duration-200"
                    style={{
                      width: s === step ? 18 : 6,
                      height: 6,
                      background: s === step ? '#fff' : 'rgba(255,255,255,0.38)',
                    }}
                  />
                ))}
              </div>
              <button
                onClick={onClose}
                className="p-1.5 rounded-lg transition-colors"
                style={{ color: 'rgba(255,255,255,0.80)' }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.18)' }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
              >
                <X size={16} />
              </button>
            </div>
          </div>

          {/* ── Body (scrollable) ── */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '24px 24px 12px' }}>
            {step === 1 ? (
              <Step1
                name={name} onName={setName}
                description={description} onDesc={setDescription}
                category={category} onCategory={handleCategorySelect}
                customCategory={customCategory} onCustomCategory={setCustomCategory}
                iconId={iconId} onIcon={(id) => { setIconId(id); setIconDirty(true) }}
                color={color} onColor={(c) => { setColor(c); setColorDirty(true) }}
                previewId={previewId}
                effectiveCategory={effectiveCategory}
                ActiveIcon={ActiveIcon}
              />
            ) : (
              <Step2
                packImports={packImports}
                onToggleComponent={toggleComponent}
                onToggleAll={toggleAllPack}
              />
            )}

            {error && (
              <div
                className="mt-4 text-sm px-3 py-2 rounded-md"
                style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
              >
                {error}
              </div>
            )}
          </div>

          {/* ── Footer ── */}
          <div
            className="px-6 py-4 flex items-center justify-between gap-2 shrink-0"
            style={{ borderTop: '1px solid var(--border)' }}
          >
            {step === 1 ? (
              <>
                <button
                  onClick={onClose}
                  className="px-3 py-2 text-xs rounded-lg"
                  style={{ color: 'var(--text-muted)' }}
                >
                  Cancel
                </button>
                <button
                  onClick={() => { if (name.trim().length >= 2) setStep(2) }}
                  disabled={name.trim().length < 2}
                  className="px-4 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-all disabled:opacity-40"
                  style={{ background: color, color: '#fff' }}
                >
                  Next — import packs <ChevronRight size={12} />
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={() => setStep(1)}
                  className="px-3 py-2 text-xs rounded-lg flex items-center gap-1"
                  style={{ color: 'var(--text-muted)' }}
                >
                  <ChevronLeft size={12} /> Back
                </button>
                <div className="flex items-center gap-2">
                  {totalSelected === 0 && (
                    <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      No packs selected — workspace starts empty
                    </span>
                  )}
                  <button
                    onClick={submit}
                    disabled={saving}
                    className="px-4 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-all disabled:opacity-40"
                    style={{ background: color, color: '#fff' }}
                  >
                    {saving ? 'Creating…' : 'Create workspace'} <ArrowRight size={12} />
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      <style>{`
        .nw-input {
          width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px;
          background: var(--bg-elevated); border: 1px solid var(--border);
          color: var(--text-primary); outline: none;
        }
        .nw-input:focus { border-color: var(--accent); background: var(--bg-card); }
      `}</style>
    </>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Identity: name, description, category, icon, color, review
// ─────────────────────────────────────────────────────────────────────────────
function Step1({
  name, onName, description, onDesc,
  category, onCategory, customCategory, onCustomCategory,
  iconId, onIcon, color, onColor,
  previewId, effectiveCategory, ActiveIcon,
}: {
  name: string; onName: (v: string) => void
  description: string; onDesc: (v: string) => void
  category: string; onCategory: (v: string) => void
  customCategory: string; onCustomCategory: (v: string) => void
  iconId: string; onIcon: (v: string) => void
  color: string; onColor: (v: string) => void
  previewId: string; effectiveCategory: string; ActiveIcon: any
}) {
  return (
    <div className="space-y-6">

      {/* Name + Description */}
      <div className="space-y-3">
        <SectionLabel>Identity</SectionLabel>
        <FieldLabel label="Name" required>
          <input
            className="nw-input"
            value={name}
            autoFocus
            onChange={(e) => onName(e.target.value)}
            placeholder="e.g. Counterparty Credit Risk"
            maxLength={80}
          />
        </FieldLabel>
        <FieldLabel label="Description" hint="One sentence on what this workspace tracks.">
          <textarea
            className="nw-input resize-none"
            rows={2}
            value={description}
            onChange={(e) => onDesc(e.target.value)}
            placeholder="e.g. Exposure, EE, EPE and PFE across cleared and bilateral books."
            maxLength={500}
          />
        </FieldLabel>
      </div>

      {/* Category */}
      <div>
        <SectionLabel>Category</SectionLabel>
        <div className="grid grid-cols-2 gap-2 mt-2">
          {CATEGORY_PRESETS.map((p) => {
            const active = category === p.id
            return (
              <button
                key={p.id}
                onClick={() => onCategory(p.id)}
                className="text-left rounded-xl px-3 py-2.5 transition-all"
                style={{
                  background: active ? `${p.color}18` : 'var(--bg-elevated)',
                  border: `1.5px solid ${active ? p.color : 'var(--border)'}`,
                  color: active ? p.color : 'var(--text-secondary)',
                }}
              >
                <div className="text-[12px] font-semibold">{p.id}</div>
                <div className="text-[10px] mt-0.5" style={{ color: active ? `${p.color}AA` : 'var(--text-muted)' }}>
                  {p.desc}
                </div>
              </button>
            )
          })}
        </div>
        {category === 'Other' && (
          <input
            className="nw-input mt-2"
            value={customCategory}
            onChange={(e) => onCustomCategory(e.target.value)}
            placeholder="Enter category name…"
            maxLength={60}
            autoFocus
          />
        )}
      </div>

      {/* Icon */}
      <div>
        <SectionLabel>Icon <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0, color: 'var(--text-muted)', fontSize: 10 }}>— auto-selected from category, tap to change</span></SectionLabel>
        <div
          className="grid gap-2 p-3 rounded-xl mt-2"
          style={{
            gridTemplateColumns: 'repeat(7, 1fr)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
          }}
        >
          {ICON_OPTIONS.map(({ id, Icon, label }) => {
            const active = iconId === id
            return (
              <button
                key={id}
                onClick={() => onIcon(id)}
                title={label}
                className="flex items-center justify-center rounded-lg transition-colors"
                style={{
                  height: 38,
                  background: active ? color : 'var(--bg-card)',
                  color: active ? '#fff' : 'var(--text-secondary)',
                  border: `1px solid ${active ? color : 'var(--border)'}`,
                }}
              >
                <Icon size={15} />
              </button>
            )
          })}
        </div>
      </div>

      {/* Color */}
      <div>
        <SectionLabel>Accent color <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0, color: 'var(--text-muted)', fontSize: 10 }}>— auto-selected from category, tap to change</span></SectionLabel>
        <div className="flex flex-wrap gap-2 mt-2">
          {COLOR_PRESETS.map((c) => {
            const active = color.toLowerCase() === c.toLowerCase()
            return (
              <button
                key={c}
                onClick={() => onColor(c)}
                title={c}
                className="rounded-lg transition-all"
                style={{
                  width: 30, height: 30, background: c,
                  border: `2.5px solid ${active ? 'var(--text-primary)' : 'transparent'}`,
                  transform: active ? 'scale(1.12)' : 'scale(1)',
                  boxShadow: active ? `0 0 0 1px var(--bg-card)` : 'none',
                }}
              />
            )
          })}
          <input
            className="nw-input font-mono"
            value={color}
            onChange={(e) => {
              const v = e.target.value.trim()
              onColor(/^#?[0-9a-f]{6}$/i.test(v) ? (v.startsWith('#') ? v : `#${v}`) : v)
            }}
            placeholder="#004977"
            maxLength={7}
            style={{ width: 90, padding: '4px 8px', fontSize: 11 }}
          />
        </div>
      </div>

      {/* Review */}
      <div>
        <SectionLabel>Review</SectionLabel>
        <div
          className="rounded-xl p-4 flex items-start gap-3 mt-2"
          style={{ background: 'var(--bg-elevated)', border: `1px dashed ${color}` }}
        >
          <div
            className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
            style={{ background: `${color}1A`, color }}
          >
            <ActiveIcon size={18} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-display text-base font-semibold" style={{ color: 'var(--text-primary)' }}>
              {name.trim() || 'Workspace name'}
            </div>
            <div className="text-[11px] mb-1" style={{ color: 'var(--text-muted)' }}>
              <span style={{ color }}>{effectiveCategory}</span>
              <span className="mx-1.5">·</span>
              <span className="font-mono">{previewId}</span>
            </div>
            <div className="text-xs" style={{ color: 'var(--text-secondary)' }}>
              {description.trim() || 'No description yet — add one to give analysts context.'}
            </div>
          </div>
        </div>
        <div className="flex items-start gap-1.5 mt-2.5" style={{ color: 'var(--text-muted)' }}>
          <Sparkles size={11} className="mt-0.5 shrink-0" />
          <span className="text-[11px]">
            Ships with all standard tabs — Overview (Today's Insights), Data, Models, Analytics, Playbooks, and Reporting.
          </span>
        </div>
      </div>

    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Domain pack import
// ─────────────────────────────────────────────────────────────────────────────
function Step2({
  packImports, onToggleComponent, onToggleAll,
}: {
  packImports: Record<string, Set<string>>
  onToggleComponent: (packId: string, compId: string) => void
  onToggleAll: (packId: string, allIds: string[]) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <SectionLabel>Import from domain packs</SectionLabel>
        <p className="text-[12px] mt-1" style={{ color: 'var(--text-muted)', lineHeight: 1.6 }}>
          Optionally import pre-built knowledge bases, agent skills, and tools from an existing domain pack. You can add more later from Settings.
        </p>
      </div>

      {DOMAIN_PACKS.map((pack) => {
        const selected = packImports[pack.id] || new Set<string>()
        const allIds = pack.components.map((c) => c.id)
        const allOn = allIds.every((id) => selected.has(id))
        const someOn = allIds.some((id) => selected.has(id))

        return (
          <div
            key={pack.id}
            className="rounded-xl overflow-hidden"
            style={{ border: `1.5px solid ${someOn ? pack.color : 'var(--border)'}` }}
          >
            {/* Pack header */}
            <div
              className="px-4 py-3 flex items-center justify-between"
              style={{ background: someOn ? `${pack.color}10` : 'var(--bg-elevated)' }}
            >
              <div className="flex items-center gap-2.5">
                <div
                  className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                  style={{ background: `${pack.color}20`, color: pack.color }}
                >
                  <BookOpen size={13} />
                </div>
                <div>
                  <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                    {pack.label}
                  </div>
                  <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{pack.desc}</div>
                </div>
              </div>
              {/* Select all toggle */}
              <button
                onClick={() => onToggleAll(pack.id, allIds)}
                className="text-[11px] font-semibold px-2.5 py-1 rounded-md transition-colors"
                style={{
                  background: allOn ? `${pack.color}18` : 'var(--bg-card)',
                  border: `1px solid ${allOn ? pack.color : 'var(--border)'}`,
                  color: allOn ? pack.color : 'var(--text-muted)',
                }}
              >
                {allOn ? 'Deselect all' : 'Select all'}
              </button>
            </div>

            {/* Components */}
            <div className="divide-y" style={{ borderTop: '1px solid var(--border)' }}>
              {pack.components.map((comp) => {
                const on = selected.has(comp.id)
                return (
                  <button
                    key={comp.id}
                    onClick={() => onToggleComponent(pack.id, comp.id)}
                    className="w-full flex items-center gap-3 px-4 py-3 text-left transition-colors"
                    style={{ background: on ? `${pack.color}08` : 'var(--bg-card)' }}
                  >
                    {/* Checkbox */}
                    <div
                      className="w-4 h-4 rounded flex items-center justify-center shrink-0 transition-all"
                      style={{
                        background: on ? pack.color : 'var(--bg-elevated)',
                        border: `1.5px solid ${on ? pack.color : 'var(--border)'}`,
                      }}
                    >
                      {on && <Check size={10} color="#fff" strokeWidth={3} />}
                    </div>
                    {/* Icon */}
                    <div
                      className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                      style={{ background: on ? `${pack.color}18` : 'var(--bg-elevated)', color: on ? pack.color : 'var(--text-muted)' }}
                    >
                      <comp.Icon size={13} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[12px] font-semibold" style={{ color: on ? 'var(--text-primary)' : 'var(--text-secondary)' }}>
                        {comp.label}
                      </div>
                      <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{comp.desc}</div>
                    </div>
                  </button>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── Small helpers ─────────────────────────────────────────────────────────
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-bold uppercase tracking-widest" style={{ color: 'var(--text-secondary)' }}>
      {children}
    </div>
  )
}

function FieldLabel({
  label, hint, required, children,
}: { label: string; hint?: string; required?: boolean; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[11px] font-semibold uppercase tracking-widest mb-1" style={{ color: 'var(--text-secondary)' }}>
        {label}{required && <span style={{ color: 'var(--error)' }}> *</span>}
      </span>
      {children}
      {hint && <div className="text-[11px] mt-1" style={{ color: 'var(--text-muted)' }}>{hint}</div>}
    </label>
  )
}
