/**
 * Self-serve Analytics tab — domain-agnostic.
 *
 * Three primitives the user can choose from:
 *   - aggregate      (group-by + measures)
 *   - compare        (same metric across two slices)
 *   - custom_python  (escape hatch — pandas function)
 *
 * Each saved Analytic Definition is a card. Cards have Run / Edit / Duplicate /
 * Delete. Runs are kept and surfaced in a "Recent runs" rail. The agent can
 * draft a new definition from prose ("show me weekly trend of NIM by region")
 * and narrate any run's result.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Plus, Wand2, Loader2, Play, Pencil, Trash2, Copy, X,
  CheckCircle2, AlertCircle, Sparkles, BarChart3, Layers, Code2, RefreshCw,
  SlidersHorizontal,
} from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import {
  ResponsiveContainer, BarChart, Bar, LineChart, Line, AreaChart, Area, PieChart, Pie, ScatterChart, Scatter,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, Cell,
} from 'recharts'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import api from '@/lib/api'
import type {
  AnalyticDefinition, AnalyticDefinitionRun, AnalyticDraftResponse, AnalyticKind,
  AnalyticOutput, AggregateSpec, CompareSpec, CustomPythonSpec, AggregateMeasure,
  Dataset,
} from '@/types'

interface Props {
  functionId: string
  functionName: string
  onAskAgent: (q: string) => void
  onContextChange: (ctx: string) => void
}

const KIND_META: Record<AnalyticKind, { label: string; color: string; icon: any; sub: string }> = {
  aggregate:     { label: 'Aggregate',     color: '#0891B2', icon: Layers,  sub: 'Group + summarize' },
  compare:       { label: 'Compare',       color: '#7C3AED', icon: BarChart3, sub: 'A vs. B delta' },
  custom_python: { label: 'Custom Python', color: '#D97706', icon: Code2,   sub: 'Pandas escape hatch' },
}

const PALETTE = ['#0891B2', '#7C3AED', '#D97706', '#059669', '#DB2777', '#2563EB', '#EA580C']

export default function SelfServeAnalyticsTab({ functionId, onContextChange }: Props) {
  const [defs, setDefs] = useState<AnalyticDefinition[]>([])
  const [runs, setRuns] = useState<AnalyticDefinitionRun[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [editor, setEditor] = useState<{ open: boolean; def: AnalyticDefinition | null; mode: 'create' | 'edit' }>(
    { open: false, def: null, mode: 'create' }
  )
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [running, setRunning] = useState<Record<string, boolean>>({})

  const loadAll = async () => {
    setLoading(true)
    try {
      const [d, r, ds] = await Promise.all([
        api.get<AnalyticDefinition[]>(`/api/analytics_defs?function_id=${functionId}`),
        api.get<AnalyticDefinitionRun[]>(`/api/analytics_defs/runs?function_id=${functionId}`),
        api.get<Dataset[]>(`/api/datasets?function_id=${functionId}`),
      ])
      setDefs(d.data)
      setRuns(r.data)
      setDatasets(ds.data)
    } catch (e) { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => { loadAll() }, [functionId])

  // Re-run the most recent run for a definition when the plot-tuner mutates
  // its spec via chat. This makes "tune the chart" feel like "the chart
  // updates" without the user having to click Run again.
  useEffect(() => {
    const handler = async (e: Event) => {
      const detail = (e as CustomEvent).detail as { definition_id?: string }
      if (!detail?.definition_id) return
      try {
        const r = await api.post<AnalyticDefinitionRun>(`/api/analytics_defs/${detail.definition_id}/run`)
        await loadAll()
        setActiveRunId(r.data.id)
      } catch { /* surface in UI later if needed */ }
    }
    window.addEventListener('cma-analytic-updated', handler)
    return () => window.removeEventListener('cma-analytic-updated', handler)
  }, [functionId])
  useEffect(() => { onContextChange(`Self-serve Analytics — ${defs.length} definition(s), ${runs.length} run(s)`) }, [defs.length, runs.length, onContextChange])

  const activeRun = useMemo(() => runs.find((r) => r.id === activeRunId) || null, [runs, activeRunId])

  const openCreate = (kind: AnalyticKind = 'aggregate') => {
    const blank: AnalyticDefinition = {
      id: '',
      function_id: functionId,
      name: '',
      description: '',
      kind,
      inputs: { dataset_id: datasets[0]?.id, dataset_id_b: datasets[1]?.id, dataset_ids: datasets[0] ? [datasets[0].id] : [] },
      aggregate_spec: kind === 'aggregate'
        ? { group_by: [], measures: [], filters: [], sort_by: null, sort_desc: true, limit: 100 } : null,
      compare_spec: kind === 'compare'
        ? { group_by: [], measure: { column: '', agg: 'sum' }, label_a: 'A', label_b: 'B', show_pct_change: true } : null,
      custom_python_spec: kind === 'custom_python'
        ? { function_name: 'run', python_source: 'def run(dfs):\n    df = next(iter(dfs.values()))\n    return {\n        "kpis": [{"label": "rows", "value": str(len(df))}],\n    }\n' } : null,
      output: { chart_type: 'bar', y_fields: [] },
      parameters: {},
      created_at: new Date().toISOString(),
    }
    setEditor({ open: true, def: blank, mode: 'create' })
  }

  const openEdit = (d: AnalyticDefinition) => setEditor({ open: true, def: { ...d }, mode: 'edit' })

  const closeEditor = () => setEditor({ open: false, def: null, mode: 'create' })

  const saveDef = async (d: AnalyticDefinition) => {
    const body = {
      function_id: d.function_id,
      name: d.name,
      description: d.description,
      kind: d.kind,
      inputs: d.inputs,
      aggregate_spec: d.kind === 'aggregate' ? d.aggregate_spec : null,
      compare_spec: d.kind === 'compare' ? d.compare_spec : null,
      custom_python_spec: d.kind === 'custom_python' ? d.custom_python_spec : null,
      output: d.output,
      parameters: d.parameters || {},
    }
    if (editor.mode === 'create') {
      await api.post('/api/analytics_defs', body)
    } else {
      await api.patch(`/api/analytics_defs/${d.id}`, body)
    }
    closeEditor()
    await loadAll()
  }

  const deleteDef = async (d: AnalyticDefinition) => {
    if (!confirm(`Delete analytic "${d.name}"?`)) return
    await api.delete(`/api/analytics_defs/${d.id}`)
    await loadAll()
  }

  const duplicateDef = async (d: AnalyticDefinition) => {
    const body = { ...d, id: undefined, name: `${d.name} (copy)`, created_at: undefined, updated_at: undefined }
    delete (body as any).id
    delete (body as any).created_at
    delete (body as any).updated_at
    await api.post('/api/analytics_defs', body)
    await loadAll()
  }

  const runDef = async (d: AnalyticDefinition) => {
    setRunning((r) => ({ ...r, [d.id]: true }))
    try {
      const r = await api.post<AnalyticDefinitionRun>(`/api/analytics_defs/${d.id}/run`)
      await loadAll()
      setActiveRunId(r.data.id)
    } finally {
      setRunning((r) => ({ ...r, [d.id]: false }))
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="font-display text-lg font-semibold mb-0.5" style={{ color: 'var(--text-primary)' }}>
            Analytics
          </h2>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Build custom analytics for your business function. Aggregate, compare, or write Python — the agent can do the first draft for you.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => openCreate('aggregate')}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
          >
            <Plus size={13} /> New analytic
          </button>
        </div>
      </div>

      <AskAgentBar
        functionId={functionId}
        datasets={datasets}
        onApplied={(draft) => {
          const newDef: AnalyticDefinition = {
            id: '',
            function_id: functionId,
            name: draft.name,
            description: draft.description,
            kind: draft.kind,
            inputs: draft.inputs || {},
            aggregate_spec: draft.aggregate_spec || null,
            compare_spec: draft.compare_spec || null,
            custom_python_spec: draft.custom_python_spec || null,
            output: draft.output || { chart_type: 'bar', y_fields: [] },
            parameters: {},
            created_at: new Date().toISOString(),
          }
          setEditor({ open: true, def: newDef, mode: 'create' })
        }}
      />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {loading && (
          <div className="col-span-full text-xs" style={{ color: 'var(--text-muted)' }}>Loading…</div>
        )}
        {!loading && defs.length === 0 && (
          <div
            className="col-span-full panel text-center py-8 text-xs"
            style={{ color: 'var(--text-muted)' }}
          >
            No analytics yet. Describe one in the box above, or click <strong>+ New analytic</strong>.
          </div>
        )}
        {defs.map((d) => (
          <DefinitionCard
            key={d.id}
            def={d}
            running={!!running[d.id]}
            onRun={() => runDef(d)}
            onEdit={() => openEdit(d)}
            onDuplicate={() => duplicateDef(d)}
            onDelete={() => deleteDef(d)}
          />
        ))}
      </div>

      {/* Recent runs rail */}
      {runs.length > 0 && (
        <div className="panel">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-secondary)' }}>
              Recent runs
            </span>
            <button onClick={loadAll} className="text-[10px] flex items-center gap-1" style={{ color: 'var(--text-muted)' }}>
              <RefreshCw size={10} /> Refresh
            </button>
          </div>
          <div className="space-y-1.5 max-h-[280px] overflow-y-auto">
            {runs.slice(0, 30).map((r) => (
              <button
                key={r.id}
                onClick={() => setActiveRunId(r.id)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-xs"
                style={{
                  background: activeRunId === r.id ? 'var(--accent-light)' : 'var(--bg-elevated)',
                  border: '1px solid ' + (activeRunId === r.id ? 'var(--accent)' : 'var(--border-subtle)'),
                }}
              >
                {r.status === 'completed'
                  ? <CheckCircle2 size={11} style={{ color: 'var(--success)' }} />
                  : <AlertCircle size={11} style={{ color: 'var(--error)' }} />}
                <span className="font-mono truncate flex-1">{r.name}</span>
                <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                  {KIND_META[r.kind].label}
                </span>
                <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                  {new Date(r.created_at).toLocaleTimeString()}
                </span>
                <span className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
                  {r.duration_ms.toFixed(0)}ms
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Run viewer */}
      {activeRun && (
        <RunViewer
          run={activeRun}
          onClose={() => setActiveRunId(null)}
          onNarrated={(md) => {
            setRuns((rs) => rs.map((r) => r.id === activeRun.id ? { ...r, narrative: md } : r))
          }}
        />
      )}

      {/* Editor side panel */}
      {editor.open && editor.def && (
        <DefinitionEditor
          def={editor.def}
          datasets={datasets}
          mode={editor.mode}
          onClose={closeEditor}
          onSave={saveDef}
        />
      )}
    </div>
  )
}

// ── "Ask the agent" bar ──────────────────────────────────────────────────
function AskAgentBar({
  functionId, datasets, onApplied,
}: {
  functionId: string
  datasets: Dataset[]
  onApplied: (d: AnalyticDraftResponse) => void
}) {
  const [prompt, setPrompt] = useState('')
  const [drafting, setDrafting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notes, setNotes] = useState<string | null>(null)

  const generate = async () => {
    if (!prompt.trim()) return
    setDrafting(true); setError(null); setNotes(null)
    try {
      const dsSummary = datasets.map((d) => ({
        id: d.id,
        name: d.name,
        columns: d.columns.map((c) => ({ name: c.name, dtype: c.dtype })),
      }))
      const r = await api.post<AnalyticDraftResponse>('/api/analytics_defs/draft', {
        function_id: functionId,
        prompt: prompt.trim(),
        available_datasets: dsSummary,
      })
      if (r.data.notes) setNotes(r.data.notes)
      onApplied(r.data)
      setPrompt('')
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Draft failed')
    } finally {
      setDrafting(false)
    }
  }

  return (
    <div
      className="rounded-lg p-3"
      style={{
        background: 'linear-gradient(135deg, rgba(124,58,237,0.08), rgba(8,145,178,0.06))',
        border: '1px solid var(--border)',
      }}
    >
      <div className="flex items-center gap-2 mb-2">
        <Wand2 size={14} style={{ color: '#7C3AED' }} />
        <span className="text-[12px] font-semibold" style={{ color: 'var(--text-primary)' }}>
          Ask the agent to draft an analytic
        </span>
        <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
          Describe what you want to see — the agent picks the right primitive and pre-fills the form.
        </span>
      </div>
      <div className="flex gap-2">
        <input
          className="input flex-1"
          placeholder="e.g. Top 10 sectors by total market value, weighted average yield by rating, week-over-week NIM change…"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !drafting) generate() }}
        />
        <button
          onClick={generate}
          disabled={drafting || !prompt.trim()}
          className="px-3 py-2 rounded-md text-xs font-semibold flex items-center gap-1.5 disabled:opacity-40"
          style={{ background: '#7C3AED', color: '#fff', whiteSpace: 'nowrap' }}
        >
          {drafting
            ? <><Loader2 size={11} className="animate-spin" /> Drafting…</>
            : <><Wand2 size={11} /> Draft</>
          }
        </button>
      </div>
      {error && (
        <div className="mt-2 px-2 py-1.5 rounded-md text-[11px]" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
          <AlertCircle size={11} className="inline mr-1" /> {error}
        </div>
      )}
      {notes && !error && (
        <div className="mt-2 px-2 py-1.5 rounded-md text-[11px]" style={{ background: 'var(--warning-bg)', color: 'var(--warning)' }}>
          <Sparkles size={11} className="inline mr-1" /> <strong>Agent notes:</strong> {notes}
        </div>
      )}
      <style>{`.input { width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px; background: var(--bg-card); border: 1px solid var(--border); color: var(--text-primary); }`}</style>
    </div>
  )
}

// ── Definition card ──────────────────────────────────────────────────────
function DefinitionCard({
  def, running, onRun, onEdit, onDuplicate, onDelete,
}: {
  def: AnalyticDefinition
  running: boolean
  onRun: () => void
  onEdit: () => void
  onDuplicate: () => void
  onDelete: () => void
}) {
  const meta = KIND_META[def.kind]
  const Icon = meta.icon
  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column' }}>
      <div className="flex items-start gap-2 mb-2">
        <div className="w-8 h-8 rounded-md flex items-center justify-center shrink-0" style={{ background: `${meta.color}20`, color: meta.color }}>
          <Icon size={14} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold truncate">{def.name || '(untitled)'}</div>
          <div className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
            {meta.label} · {meta.sub}
          </div>
        </div>
      </div>
      {def.description && (
        <p className="text-xs flex-1" style={{ color: 'var(--text-secondary)' }}>{def.description}</p>
      )}
      <div className="flex items-center justify-end gap-1 mt-3 pt-3" style={{ borderTop: '1px solid var(--border-subtle)' }}>
        <button
          onClick={onRun}
          disabled={running}
          className="px-2.5 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 disabled:opacity-40"
          style={{ background: 'var(--accent)', color: '#fff' }}
        >
          {running ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}
          {running ? 'Running…' : 'Run'}
        </button>
        <button onClick={onEdit} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }} title="Edit"><Pencil size={11} /></button>
        <button onClick={onDuplicate} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }} title="Duplicate"><Copy size={11} /></button>
        <button onClick={onDelete} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }} title="Delete"><Trash2 size={11} /></button>
      </div>
    </div>
  )
}

// ── Run viewer (chart + table + narrative) ────────────────────────────────
function RunViewer({
  run, onClose, onNarrated, onChartUpdated,
}: {
  run: AnalyticDefinitionRun
  onClose: () => void
  onNarrated: (md: string) => void
  onChartUpdated?: () => void
}) {
  const [narrating, setNarrating] = useState(false)
  const [narrErr, setNarrErr] = useState<string | null>(null)
  const setEntity = useChatStore((s) => s.setEntity)
  const setOpen = useChatStore((s) => s.setOpen)

  const tuneChart = () => {
    // Send the agent the analytic definition (not the run snapshot) so the
    // plot-tuner can mutate the persisted spec; the next run picks it up.
    setEntity('analytic_def', run.definition_id)
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `Tune the "${run.name}" chart — what would you like to change? (sort, filter, chart type, colors, axis labels, font size, legend)`,
    }))
  }

  const askNarrate = async () => {
    setNarrating(true); setNarrErr(null)
    try {
      const r = await api.post<{ markdown: string }>(`/api/analytics_defs/runs/${run.id}/narrate`)
      onNarrated(r.data.markdown)
    } catch (e: any) {
      setNarrErr(e?.response?.data?.detail || 'Narration failed')
    } finally {
      setNarrating(false)
    }
  }

  return (
    <div className="panel">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="text-sm font-semibold">{run.name}</div>
          <div className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
            {run.id} · {run.kind} · {run.duration_ms.toFixed(0)} ms · {new Date(run.created_at).toLocaleString()}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={tuneChart}
            className="px-2 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 transition-all"
            style={{
              background: 'rgba(15,118,110,0.10)',
              color: '#0F766E',
              border: '1px solid rgba(15,118,110,0.25)',
            }}
            title="Edit this chart with the Plot Tuner agent — sort, filter, change chart type, recolor, rename axes, change fonts"
            onMouseEnter={(e) => {
              const el = e.currentTarget as HTMLElement
              el.style.background = '#0F766E'
              el.style.color = '#fff'
            }}
            onMouseLeave={(e) => {
              const el = e.currentTarget as HTMLElement
              el.style.background = 'rgba(15,118,110,0.10)'
              el.style.color = '#0F766E'
            }}
          >
            <SlidersHorizontal size={11} />
            Tune
          </button>
          <button onClick={onClose} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }}>
            <X size={14} />
          </button>
        </div>
      </div>

      {run.status === 'failed' && (
        <div className="px-3 py-2 rounded-md text-xs font-mono" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
          {run.error}
        </div>
      )}

      {run.status === 'completed' && run.result && (
        <>
          {run.result.kpis.length > 0 && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
              {run.result.kpis.map((k, i) => (
                <div key={i} className="rounded-md p-2.5" style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)' }}>
                  <div className="text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>{k.label}</div>
                  <div className="text-base font-display font-semibold">{k.value}</div>
                  {k.sublabel && <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{k.sublabel}</div>}
                </div>
              ))}
            </div>
          )}

          {run.result.chart && <ChartRenderer chart={run.result.chart} />}

          {run.result.table && (
            <div className="mt-3 overflow-auto" style={{ maxHeight: 320 }}>
              <table className="w-full text-xs">
                <thead>
                  <tr>
                    {run.result.table.columns.map((c) => (
                      <th key={c} className="text-left px-2 py-1 font-mono text-[10px] uppercase tracking-wider"
                          style={{ color: 'var(--text-muted)', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {run.result.table.rows.slice(0, 200).map((row, i) => (
                    <tr key={i}>
                      {row.map((cell, j) => (
                        <td key={j} className="px-2 py-1 font-mono" style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                          {cell === null || cell === undefined ? <span style={{ color: 'var(--text-muted)' }}>—</span> :
                           typeof cell === 'number' ? cell.toLocaleString(undefined, { maximumFractionDigits: 4 }) :
                           String(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {run.result.table.rows.length > 200 && (
                <div className="text-[10px] py-1 px-2" style={{ color: 'var(--text-muted)' }}>
                  Showing first 200 of {run.result.table.rows.length} rows
                </div>
              )}
            </div>
          )}

          {/* Narrative */}
          <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--border-subtle)' }}>
            {run.narrative
              ? (
                <div className="text-xs" style={{ color: 'var(--text-secondary)' }}>
                  <div className="text-[10px] uppercase tracking-widest mb-1" style={{ color: 'var(--text-muted)' }}>Agent narrative</div>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{run.narrative}</ReactMarkdown>
                </div>
              )
              : (
                <button
                  onClick={askNarrate}
                  disabled={narrating}
                  className="px-3 py-1.5 rounded-md text-[11px] font-semibold flex items-center gap-1 disabled:opacity-40"
                  style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                >
                  {narrating ? <Loader2 size={11} className="animate-spin" /> : <Sparkles size={11} />}
                  {narrating ? 'Asking the agent…' : 'Ask the agent to narrate this'}
                </button>
              )}
            {narrErr && (
              <div className="mt-2 px-2 py-1 rounded-md text-[11px]" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
                {narrErr}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function ChartRenderer({ chart }: { chart: NonNullable<AnalyticDefinitionRun['result']>['chart'] }) {
  if (!chart) return null
  const { type, x_field, y_fields, data, style } = chart
  if (!data || data.length === 0) return null

  // Resolve style overrides — falling back to defaults when fields are unset.
  const palette = (style?.palette && style.palette.length > 0) ? style.palette : PALETTE
  const fontSize = style?.font_size ?? 10
  const legendPosition = style?.legend_position || 'bottom'
  const legendVisible = legendPosition !== 'none'
  const legendVAlign: 'top' | 'middle' | 'bottom' =
    legendPosition === 'top' ? 'top' : legendPosition === 'bottom' ? 'bottom' : 'middle'
  const legendHAlign: 'left' | 'center' | 'right' =
    legendPosition === 'left' ? 'left' : legendPosition === 'right' ? 'right' : 'center'
  const legendLayout: 'horizontal' | 'vertical' =
    (legendPosition === 'left' || legendPosition === 'right') ? 'vertical' : 'horizontal'

  // Apply client-side sort if the style asks for one.
  const sortedData = (() => {
    if (!style?.sort_field || !data.length || !(style.sort_field in data[0])) return data
    const f = style.sort_field
    const desc = !!style.sort_desc
    return [...data].sort((a, b) => {
      const va = a[f]; const vb = b[f]
      if (va == null && vb == null) return 0
      if (va == null) return 1
      if (vb == null) return -1
      if (typeof va === 'number' && typeof vb === 'number') return desc ? vb - va : va - vb
      return desc ? String(vb).localeCompare(String(va)) : String(va).localeCompare(String(vb))
    })
  })()

  // Number formatter (very small subset of Excel-style codes).
  const fmtNum = (v: any): string => {
    if (typeof v !== 'number') return String(v)
    const code = style?.number_format
    if (!code) return v.toLocaleString(undefined, { maximumFractionDigits: 4 })
    if (code.includes('%')) return (v * (code.includes('0.') ? 100 : 1)).toFixed((code.split('.')[1] || '').replace(/[^0]/g, '').length || 0) + '%'
    if (code.startsWith('$')) return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    if (code.endsWith('a')) {
      const abs = Math.abs(v)
      if (abs >= 1e9) return (v / 1e9).toFixed(2) + 'B'
      if (abs >= 1e6) return (v / 1e6).toFixed(2) + 'M'
      if (abs >= 1e3) return (v / 1e3).toFixed(2) + 'K'
      return v.toFixed(2)
    }
    const decimals = (code.split('.')[1] || '').length
    return v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  }

  // Optional axis labels — wired to Recharts' `label` prop with offsets.
  const xLabel = style?.x_axis_label
  const yLabel = style?.y_axis_label

  const common = (
    <>
      <CartesianGrid stroke="var(--border-subtle)" strokeDasharray="3 3" />
      <XAxis
        dataKey={x_field || ''}
        stroke="var(--text-muted)"
        tick={{ fontSize }}
        label={xLabel ? { value: xLabel, position: 'insideBottom', offset: -4, style: { fontSize, fill: 'var(--text-secondary)' } } : undefined}
      />
      <YAxis
        stroke="var(--text-muted)"
        tick={{ fontSize }}
        tickFormatter={style?.number_format ? fmtNum : undefined}
        label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft', style: { fontSize, fill: 'var(--text-secondary)' } } : undefined}
      />
      <Tooltip
        contentStyle={{
          background: 'var(--bg-card)', border: '1px solid var(--border)',
          fontSize: fontSize + 1, borderRadius: 8,
        }}
        formatter={style?.number_format ? (v: any) => fmtNum(v) : undefined}
      />
      {legendVisible && (
        <Legend
          wrapperStyle={{ fontSize }}
          verticalAlign={legendVAlign}
          align={legendHAlign}
          layout={legendLayout}
        />
      )}
    </>
  )

  return (
    <div>
      {style?.title && (
        <div className="text-sm font-semibold mb-1" style={{ color: 'var(--text-primary)' }}>{style.title}</div>
      )}
      <div style={{ height: 280 }}>
      <ResponsiveContainer width="100%" height="100%">
        {type === 'line' ? (
          <LineChart data={sortedData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            {common}
            {y_fields.map((y, i) => <Line key={y} type="monotone" dataKey={y} stroke={palette[i % palette.length]} strokeWidth={1.5} dot={false} />)}
          </LineChart>
        ) : type === 'area' ? (
          <AreaChart data={sortedData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            {common}
            {y_fields.map((y, i) => <Area key={y} type="monotone" dataKey={y} stroke={palette[i % palette.length]} fill={palette[i % palette.length]} fillOpacity={0.2} />)}
          </AreaChart>
        ) : type === 'scatter' ? (
          // Scatter needs numeric axes on BOTH dimensions — without
          // type="number" on the XAxis, recharts treats x_field values as
          // category labels and the dots collapse onto evenly-spaced
          // ticks (so "historical_beta" appears unused). Each axis also
          // needs its own dataKey for value-based positioning.
          <ScatterChart margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            <CartesianGrid stroke="var(--border-subtle)" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey={x_field || ''}
              stroke="var(--text-muted)"
              tick={{ fontSize }}
              tickFormatter={style?.number_format ? fmtNum : undefined}
              label={xLabel ? { value: xLabel, position: 'insideBottom', offset: -4, style: { fontSize, fill: 'var(--text-secondary)' } } : undefined}
            />
            <YAxis
              type="number"
              dataKey={y_fields[0]}
              stroke="var(--text-muted)"
              tick={{ fontSize }}
              tickFormatter={style?.number_format ? fmtNum : undefined}
              label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft', style: { fontSize, fill: 'var(--text-secondary)' } } : undefined}
            />
            <Tooltip
              cursor={{ strokeDasharray: '3 3' }}
              contentStyle={{
                background: 'var(--bg-card)', border: '1px solid var(--border)',
                fontSize: fontSize + 1, borderRadius: 8,
              }}
              formatter={(v: any, n: any, p: any) => {
                const r = p?.payload || {}
                const label = r.product || r.name || r[x_field || ''] || ''
                const fmtV = style?.number_format ? fmtNum(v) : v
                return [`${fmtV}`, `${n}${label ? ` · ${label}` : ''}`]
              }}
            />
            {legendVisible && (
              // Float the legend inside the plot area (top-right corner)
              // so the chart isn't dominated by a single-series legend strip
              // below it. `position: absolute` is honored because Recharts
              // wraps the Legend in a relatively-positioned container.
              <Legend
                wrapperStyle={{
                  fontSize,
                  position: 'absolute',
                  top: 4,
                  right: 12,
                  background: 'var(--bg-card)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 6,
                  padding: '2px 8px',
                  pointerEvents: 'none',
                }}
                verticalAlign="top"
                align="right"
                layout="vertical"
                iconSize={10}
              />
            )}
            {y_fields.map((y, i) => (
              <Scatter key={y} name={y} data={sortedData} fill={palette[i % palette.length]} />
            ))}
          </ScatterChart>
        ) : type === 'pie' ? (
          <PieChart>
            <Tooltip formatter={style?.number_format ? (v: any) => fmtNum(v) : undefined} />
            {legendVisible && <Legend wrapperStyle={{ fontSize }} verticalAlign={legendVAlign} align={legendHAlign} layout={legendLayout} />}
            <Pie data={sortedData} dataKey={y_fields[0]} nameKey={x_field || ''} outerRadius={90} label={{ fontSize }}>
              {sortedData.map((_, i) => <Cell key={i} fill={palette[i % palette.length]} />)}
            </Pie>
          </PieChart>
        ) : (
          // bar / stacked_bar / fallback
          <BarChart data={sortedData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            {common}
            {y_fields.map((y, i) => (
              <Bar
                key={y}
                dataKey={y}
                stackId={type === 'stacked_bar' ? 'stack' : undefined}
                fill={palette[i % palette.length]}
              />
            ))}
          </BarChart>
        )}
      </ResponsiveContainer>
      </div>
    </div>
  )
}

// ── Definition editor (side panel) ────────────────────────────────────────
function DefinitionEditor({
  def, datasets, mode, onClose, onSave,
}: {
  def: AnalyticDefinition
  datasets: Dataset[]
  mode: 'create' | 'edit'
  onClose: () => void
  onSave: (d: AnalyticDefinition) => Promise<void>
}) {
  const [d, setD] = useState<AnalyticDefinition>(def)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // Switching kinds wipes the spec for the previous kind to keep payloads clean.
  const switchKind = (kind: AnalyticKind) => {
    setD((prev) => ({
      ...prev,
      kind,
      aggregate_spec: kind === 'aggregate'
        ? prev.aggregate_spec || { group_by: [], measures: [], filters: [], sort_by: null, sort_desc: true, limit: 100 }
        : null,
      compare_spec: kind === 'compare'
        ? prev.compare_spec || { group_by: [], measure: { column: '', agg: 'sum' }, label_a: 'A', label_b: 'B', show_pct_change: true }
        : null,
      custom_python_spec: kind === 'custom_python'
        ? prev.custom_python_spec || { function_name: 'run', python_source: 'def run(dfs):\n    df = next(iter(dfs.values()))\n    return {"kpis": [{"label": "rows", "value": str(len(df))}]}\n' }
        : null,
    }))
  }

  // Pre-flight validity — same shape as the backend runner's checks, but
  // surfaced inline before the user clicks Run.
  const validityError: string | null = (() => {
    if (!d.name.trim()) return 'Name is required'
    if (d.kind === 'aggregate') {
      if (!d.inputs.dataset_id) return 'Aggregate needs a dataset'
      const bad = (d.aggregate_spec?.measures || []).find((m) => !m.column)
      if (bad) return 'A measure has no column selected'
    }
    if (d.kind === 'compare') {
      if (!d.inputs.dataset_id || !d.inputs.dataset_id_b) {
        return 'Compare needs both Dataset A and Dataset B'
      }
      if (d.inputs.dataset_id === d.inputs.dataset_id_b) {
        return 'Compare needs two different datasets'
      }
      if (!d.compare_spec?.measure?.column) {
        return 'Compare needs a measure column'
      }
    }
    if (d.kind === 'custom_python') {
      if (!(d.inputs.dataset_ids && d.inputs.dataset_ids.length > 0) && !d.inputs.dataset_id) {
        return 'Custom Python needs at least one dataset bound'
      }
      if (!d.custom_python_spec?.python_source?.includes('def ')) {
        return 'Custom Python needs a function definition'
      }
    }
    return null
  })()

  const submit = async () => {
    if (validityError) { setErr(validityError); return }
    setSaving(true); setErr(null)
    try {
      await onSave(d)
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const colsForDataset = (id?: string | null) => {
    const ds = datasets.find((x) => x.id === id)
    return ds ? ds.columns.map((c) => c.name) : []
  }
  const cols = colsForDataset(d.inputs.dataset_id)

  return (
    <>
      <div
        className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto"
        style={{ background: 'rgba(11,15,25,0.45)', padding: '32px 24px' }}
        onClick={onClose}
      >
      <div
        onClick={(e) => e.stopPropagation()}
        className="z-50 flex flex-col rounded-xl overflow-hidden"
        style={{
          width: 'min(960px, 100%)',
          // 64px = the wrapper's 32px top + 32px bottom padding. Without
          // subtracting it, `92vh` plus the padding overflowed the
          // viewport on shorter screens and the modal header got pushed
          // above the visible area. Anchoring at the top (items-start +
          // top padding) also guarantees the title is always visible.
          maxHeight: 'min(820px, calc(100vh - 64px))',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          boxShadow: '0 24px 72px rgba(0,0,0,0.32)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-4"
          style={{
            background: `linear-gradient(135deg, ${KIND_META[d.kind].color}, var(--accent))`,
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div>
            <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>
              {mode === 'create' ? 'New analytic' : 'Edit analytic'}
            </div>
            <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.75)' }}>
              {d.id || 'Drafting…'}
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'rgba(255,255,255,0.85)' }}><X size={16} /></button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {/* Kind picker */}
          <div className="grid grid-cols-3 gap-2">
            {(['aggregate', 'compare', 'custom_python'] as AnalyticKind[]).map((k) => {
              const m = KIND_META[k]
              const active = d.kind === k
              const Icon = m.icon
              return (
                <button
                  key={k}
                  onClick={() => switchKind(k)}
                  className="rounded-lg p-2.5 flex items-center gap-2 text-left"
                  style={{
                    background: active ? `${m.color}20` : 'var(--bg-elevated)',
                    border: `1px solid ${active ? m.color : 'var(--border)'}`,
                  }}
                >
                  <div className="w-7 h-7 rounded-md flex items-center justify-center shrink-0" style={{ background: `${m.color}30`, color: m.color }}>
                    <Icon size={13} />
                  </div>
                  <div className="min-w-0">
                    <div className="text-[12px] font-semibold">{m.label}</div>
                    <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>{m.sub}</div>
                  </div>
                </button>
              )
            })}
          </div>

          <Field label="Name">
            <input className="input" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} placeholder="e.g. Top sectors by AUM" />
          </Field>
          <Field label="Description">
            <textarea rows={2} className="input resize-none" value={d.description || ''} onChange={(e) => setD({ ...d, description: e.target.value })} placeholder="What this analytic answers" />
          </Field>

          {/* ── Aggregate ──────────────────────────── */}
          {d.kind === 'aggregate' && d.aggregate_spec && (
            <AggregateEditor
              spec={d.aggregate_spec}
              dataset_id={d.inputs.dataset_id}
              datasets={datasets}
              onChangeDataset={(id) => setD({ ...d, inputs: { ...d.inputs, dataset_id: id } })}
              onChange={(s) => setD({ ...d, aggregate_spec: s })}
            />
          )}

          {/* ── Compare ────────────────────────────── */}
          {d.kind === 'compare' && d.compare_spec && (
            <CompareEditor
              spec={d.compare_spec}
              datasets={datasets}
              dataset_id={d.inputs.dataset_id}
              dataset_id_b={d.inputs.dataset_id_b}
              onChangeA={(id) => setD({ ...d, inputs: { ...d.inputs, dataset_id: id } })}
              onChangeB={(id) => setD({ ...d, inputs: { ...d.inputs, dataset_id_b: id } })}
              onChange={(s) => setD({ ...d, compare_spec: s })}
            />
          )}

          {/* ── Custom Python ──────────────────────── */}
          {d.kind === 'custom_python' && d.custom_python_spec && (
            <CustomPythonEditor
              spec={d.custom_python_spec}
              datasets={datasets}
              dataset_ids={d.inputs.dataset_ids || []}
              onChangeDatasetIds={(ids) => setD({ ...d, inputs: { ...d.inputs, dataset_ids: ids } })}
              onChange={(s) => setD({ ...d, custom_python_spec: s })}
            />
          )}

          {/* ── Output ─────────────────────────────── */}
          <OutputEditor
            output={d.output}
            availableFields={d.kind === 'compare'
              ? [...(d.compare_spec?.group_by || []), 'delta', 'pct_change', d.compare_spec?.label_a || 'A', d.compare_spec?.label_b || 'B']
              : d.kind === 'aggregate'
                ? [...(d.aggregate_spec?.group_by || []), ...(d.aggregate_spec?.measures.map((m) => m.alias || `${m.agg}_${m.column}`) || [])]
                : cols}
            onChange={(o) => setD({ ...d, output: o })}
          />

          {err && (
            <div className="px-3 py-2 rounded-md text-xs" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
              <AlertCircle size={11} className="inline mr-1" /> {err}
            </div>
          )}
          {!err && validityError && (
            <div className="px-3 py-2 rounded-md text-xs" style={{ background: 'var(--warning-bg)', color: 'var(--warning)' }}>
              <AlertCircle size={11} className="inline mr-1" /> {validityError}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
          <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            {validityError ? '⚠ ' + validityError : 'Ready to save'}
          </span>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-3 py-2 text-xs rounded-lg" style={{ color: 'var(--text-muted)' }}>Cancel</button>
            <button
              onClick={submit}
              disabled={saving || !!validityError}
              title={validityError || undefined}
              className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              {saving ? 'Saving…' : mode === 'create' ? 'Create' : 'Save'}
            </button>
          </div>
        </div>
      </div>
      </div>

      <style>{`.input { width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px; background: var(--bg-card); border: 1px solid var(--border); color: var(--text-primary); }`}</style>
    </>
  )
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="block text-[11px] font-semibold uppercase tracking-widest mb-1" style={{ color: 'var(--text-secondary)' }}>
        {label} {hint && <span style={{ color: 'var(--text-muted)', fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>· {hint}</span>}
      </span>
      {children}
    </label>
  )
}

// ── Sub-editors per primitive ──────────────────────────────────────────
function DatasetPicker({ value, datasets, onChange, label }: { value?: string | null; datasets: Dataset[]; onChange: (id: string) => void; label: string }) {
  return (
    <Field label={label}>
      <select className="input" value={value || ''} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select dataset…</option>
        {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </Field>
  )
}

function AggregateEditor({
  spec, dataset_id, datasets, onChangeDataset, onChange,
}: {
  spec: AggregateSpec
  dataset_id?: string | null
  datasets: Dataset[]
  onChangeDataset: (id: string) => void
  onChange: (s: AggregateSpec) => void
}) {
  const ds = datasets.find((x) => x.id === dataset_id)
  const cols = ds ? ds.columns : []
  const colNames = cols.map((c) => c.name)
  const numCols = cols.filter((c) => /int|float|num|decim/i.test(c.dtype)).map((c) => c.name)

  const addMeasure = () => onChange({ ...spec, measures: [...spec.measures, { column: numCols[0] || '', agg: 'sum' }] })
  const updateMeasure = (i: number, patch: Partial<AggregateMeasure>) => onChange({ ...spec, measures: spec.measures.map((m, j) => j === i ? { ...m, ...patch } : m) })
  const removeMeasure = (i: number) => onChange({ ...spec, measures: spec.measures.filter((_, j) => j !== i) })

  return (
    <div className="space-y-3">
      <DatasetPicker value={dataset_id} datasets={datasets} onChange={onChangeDataset} label="Dataset" />
      <Field label="Group by" hint="0 or more dimensions">
        <MultiSelect options={colNames} value={spec.group_by} onChange={(v) => onChange({ ...spec, group_by: v })} placeholder="Pick dimensions…" />
      </Field>
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-secondary)' }}>
            Measures ({spec.measures.length})
          </span>
          <button onClick={addMeasure} className="text-[11px] flex items-center gap-1" style={{ color: 'var(--accent)', fontWeight: 600 }}>
            <Plus size={11} /> Add measure
          </button>
        </div>
        <div className="rounded-lg p-2 space-y-2" style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
          {spec.measures.length === 0 && (
            <div className="text-xs px-1 py-2" style={{ color: 'var(--text-muted)' }}>
              No measures yet. Add one to compute a sum/avg/percentile/etc.
            </div>
          )}
          {spec.measures.map((m, i) => (
            <div key={i} className="grid grid-cols-12 gap-2 items-center">
              <select className="input col-span-3" value={m.column} onChange={(e) => updateMeasure(i, { column: e.target.value })}>
                <option value="">column…</option>
                {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className="input col-span-2" value={m.agg} onChange={(e) => updateMeasure(i, { agg: e.target.value as any })}>
                {['sum', 'avg', 'count', 'min', 'max', 'median', 'p25', 'p75', 'p90', 'p99', 'weighted_avg', 'stddev'].map((a) =>
                  <option key={a} value={a}>{a}</option>)}
              </select>
              {m.agg === 'weighted_avg' && (
                <select className="input col-span-2" value={m.weight_by || ''} onChange={(e) => updateMeasure(i, { weight_by: e.target.value })}>
                  <option value="">weight by…</option>
                  {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              <input className={`input ${m.agg === 'weighted_avg' ? 'col-span-4' : 'col-span-6'}`} placeholder="alias (optional)" value={m.alias || ''} onChange={(e) => updateMeasure(i, { alias: e.target.value })} />
              <button onClick={() => removeMeasure(i)} className="col-span-1 p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }}><Trash2 size={12} /></button>
            </div>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Sort by">
          <select className="input" value={spec.sort_by || ''} onChange={(e) => onChange({ ...spec, sort_by: e.target.value || null })}>
            <option value="">(none)</option>
            {[...spec.group_by, ...spec.measures.map((m) => m.alias || `${m.agg}_${m.column}`)].map((c) =>
              <option key={c} value={c}>{c}</option>)}
          </select>
        </Field>
        <Field label="Direction">
          <select className="input" value={spec.sort_desc ? 'desc' : 'asc'} onChange={(e) => onChange({ ...spec, sort_desc: e.target.value === 'desc' })}>
            <option value="desc">desc</option>
            <option value="asc">asc</option>
          </select>
        </Field>
        <Field label="Limit">
          <input className="input" type="number" value={spec.limit ?? ''} onChange={(e) => onChange({ ...spec, limit: e.target.value ? parseInt(e.target.value, 10) : null })} />
        </Field>
      </div>
    </div>
  )
}

function CompareEditor({
  spec, datasets, dataset_id, dataset_id_b, onChangeA, onChangeB, onChange,
}: {
  spec: CompareSpec
  datasets: Dataset[]
  dataset_id?: string | null
  dataset_id_b?: string | null
  onChangeA: (id: string) => void
  onChangeB: (id: string) => void
  onChange: (s: CompareSpec) => void
}) {
  const dsA = datasets.find((x) => x.id === dataset_id)
  const dsB = datasets.find((x) => x.id === dataset_id_b)
  const colsA = dsA ? dsA.columns.map((c) => c.name) : []
  const colsB = dsB ? dsB.columns.map((c) => c.name) : []
  // Compare runs the same group_by + measure on BOTH datasets and joins the
  // results, so every column referenced by the spec must exist in both. The
  // editor restricts pickers to that intersection so users can't construct
  // an invalid spec, and surfaces the A-only / B-only columns as a hint.
  const setA = new Set(colsA)
  const setB = new Set(colsB)
  const cols = colsA.filter((c) => setB.has(c))
  const onlyA = colsA.filter((c) => !setB.has(c))
  const onlyB = colsB.filter((c) => !setA.has(c))
  const bothPicked = !!(dsA && dsB) && dsA.id !== dsB.id

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <DatasetPicker value={dataset_id} datasets={datasets} onChange={onChangeA} label="Dataset A" />
        <DatasetPicker value={dataset_id_b} datasets={datasets} onChange={onChangeB} label="Dataset B" />
      </div>

      {bothPicked && (onlyA.length > 0 || onlyB.length > 0) && (
        <div
          className="text-[11px] rounded-md px-3 py-2"
          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', color: 'var(--text-secondary)' }}
        >
          <div className="mb-1">
            <strong>{cols.length}</strong> column{cols.length === 1 ? '' : 's'} common to both — Compare can only use these.
          </div>
          {onlyA.length > 0 && (
            <div style={{ color: 'var(--text-muted)' }}>
              Only in <strong>A ({dsA!.name})</strong>: <span className="font-mono">{onlyA.join(', ')}</span>
            </div>
          )}
          {onlyB.length > 0 && (
            <div style={{ color: 'var(--text-muted)' }}>
              Only in <strong>B ({dsB!.name})</strong>: <span className="font-mono">{onlyB.join(', ')}</span>
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <Field label="Label A"><input className="input" value={spec.label_a} onChange={(e) => onChange({ ...spec, label_a: e.target.value })} /></Field>
        <Field label="Label B"><input className="input" value={spec.label_b} onChange={(e) => onChange({ ...spec, label_b: e.target.value })} /></Field>
      </div>
      <Field label="Group by" hint={bothPicked ? 'common columns only' : ''}>
        <MultiSelect options={cols} value={spec.group_by} onChange={(v) => onChange({ ...spec, group_by: v })} placeholder={bothPicked ? 'Dimensions…' : 'Pick A and B first'} />
      </Field>
      <div className="rounded-lg p-2" style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
        <div className="text-[11px] font-semibold uppercase tracking-widest mb-1" style={{ color: 'var(--text-secondary)' }}>Measure (common columns only)</div>
        <div className="grid grid-cols-12 gap-2 items-center">
          <select className="input col-span-3" value={spec.measure.column} onChange={(e) => onChange({ ...spec, measure: { ...spec.measure, column: e.target.value } })}>
            <option value="">column…</option>
            {cols.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select className="input col-span-3" value={spec.measure.agg} onChange={(e) => onChange({ ...spec, measure: { ...spec.measure, agg: e.target.value as any } })}>
            {['sum', 'avg', 'count', 'min', 'max', 'median', 'weighted_avg'].map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          {spec.measure.agg === 'weighted_avg' && (
            <select className="input col-span-3" value={spec.measure.weight_by || ''} onChange={(e) => onChange({ ...spec, measure: { ...spec.measure, weight_by: e.target.value } })}>
              <option value="">weight by…</option>
              {cols.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          )}
          <input className={`input ${spec.measure.agg === 'weighted_avg' ? 'col-span-3' : 'col-span-6'}`} placeholder="alias" value={spec.measure.alias || ''} onChange={(e) => onChange({ ...spec, measure: { ...spec.measure, alias: e.target.value } })} />
        </div>
      </div>
      <label className="flex items-center gap-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
        <input type="checkbox" checked={spec.show_pct_change} onChange={(e) => onChange({ ...spec, show_pct_change: e.target.checked })} />
        Compute % change column
      </label>
    </div>
  )
}

function CustomPythonEditor({
  spec, datasets, dataset_ids, onChangeDatasetIds, onChange,
}: {
  spec: CustomPythonSpec
  datasets: Dataset[]
  dataset_ids: string[]
  onChangeDatasetIds: (ids: string[]) => void
  onChange: (s: CustomPythonSpec) => void
}) {
  return (
    <div className="space-y-3">
      <Field label="Datasets bound" hint="Available inside as dfs[<id>]">
        <MultiSelect
          options={datasets.map((d) => d.id)}
          labels={Object.fromEntries(datasets.map((d) => [d.id, d.name]))}
          value={dataset_ids}
          onChange={onChangeDatasetIds}
          placeholder="Pick datasets…"
        />
      </Field>
      <Field label="Function name"><input className="input font-mono" value={spec.function_name} onChange={(e) => onChange({ ...spec, function_name: e.target.value })} placeholder="run" /></Field>
      <Field label="Python source" hint="Receive dfs: dict[id→DataFrame]; return {kpis, chart, table}">
        <textarea
          rows={14}
          className="input resize-y font-mono"
          style={{ fontSize: 12, lineHeight: 1.55, tabSize: 4 }}
          value={spec.python_source}
          onChange={(e) => onChange({ ...spec, python_source: e.target.value })}
          spellCheck={false}
        />
      </Field>
    </div>
  )
}

function OutputEditor({
  output, availableFields, onChange,
}: {
  output: AnalyticOutput
  availableFields: string[]
  onChange: (o: AnalyticOutput) => void
}) {
  return (
    <div className="space-y-3">
      <div className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-secondary)' }}>Output</div>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Chart type">
          <select className="input" value={output.chart_type} onChange={(e) => onChange({ ...output, chart_type: e.target.value as any })}>
            {['bar', 'line', 'area', 'stacked_bar', 'scatter', 'pie', 'table', 'kpi'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="X field">
          <select className="input" value={output.x_field || ''} onChange={(e) => onChange({ ...output, x_field: e.target.value || null })}>
            <option value="">(auto)</option>
            {availableFields.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
        </Field>
        <Field label="Y fields">
          <MultiSelect options={availableFields} value={output.y_fields} onChange={(v) => onChange({ ...output, y_fields: v })} placeholder="(auto)" />
        </Field>
      </div>
    </div>
  )
}

function MultiSelect({
  options, labels, value, onChange, placeholder,
}: {
  options: string[]
  labels?: Record<string, string>
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
}) {
  const toggle = (opt: string) => onChange(value.includes(opt) ? value.filter((v) => v !== opt) : [...value, opt])
  return (
    <div
      className="rounded-md flex flex-wrap gap-1 p-1.5"
      style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', minHeight: 38 }}
    >
      {options.length === 0 && <span className="text-xs px-1" style={{ color: 'var(--text-muted)' }}>{placeholder || 'No options'}</span>}
      {options.map((o) => {
        const on = value.includes(o)
        return (
          <button
            key={o}
            onClick={() => toggle(o)}
            className="px-2 py-0.5 rounded-full text-[10px] font-mono"
            style={{
              background: on ? 'var(--accent)' : 'var(--bg-elevated)',
              color: on ? '#fff' : 'var(--text-secondary)',
              border: `1px solid ${on ? 'var(--accent)' : 'var(--border-subtle)'}`,
            }}
          >
            {labels?.[o] || o}
          </button>
        )
      })}
    </div>
  )
}
