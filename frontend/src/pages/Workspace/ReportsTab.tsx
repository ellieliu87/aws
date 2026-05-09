/**
 * Analytics tab — interactive tiles (plot OR table) built from any source:
 *   - Bound dataset (from the Data tab — Snowflake / OneLake / IHS / uploads)
 *   - Workflow output (a saved analytics run; what the workflow tab calls a destination)
 *   - Ad-hoc upload (CSV / Parquet / XLSX / JSON dropped into the designer; under the hood
 *     this creates a tile-scoped Dataset so the existing preview pipeline can read it)
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BarChart3, Plus, Trash2, X, Sparkles, LineChart as LineIcon, PieChart as PieIcon,
  TrendingUp, GitCommit, Layers, Database, FlaskConical, Table as TableIcon,
  Upload, ZoomIn, Pin, PinOff, SlidersHorizontal, Gauge,
} from 'lucide-react'
import api from '@/lib/api'
import { useChatStore } from '@/store/chatStore'
import Chart from '@/components/charts/Chart'
import InteractiveTable from '@/components/charts/InteractiveTable'
import AgentTileDesigner from './AgentTileDesigner'
import type {
  ChartSpec, Dataset, AnalyticsRun, PlotConfig, KpiPreview,
} from '@/types'

const CHART_TYPES: { id: PlotConfig['chart_type']; label: string; icon: any }[] = [
  { id: 'line',        label: 'Line',        icon: LineIcon },
  { id: 'bar',         label: 'Bar',         icon: BarChart3 },
  { id: 'area',        label: 'Area',        icon: TrendingUp },
  { id: 'pie',         label: 'Pie',         icon: PieIcon },
  { id: 'scatter',     label: 'Scatter',     icon: GitCommit },
  { id: 'stacked_bar', label: 'Stacked Bar', icon: Layers },
]

interface Props {
  functionId: string
  functionName: string
  onAskAgent: (q: string) => void
  onContextChange: (ctx: string | null) => void
}

interface PreviewBundle {
  spec: ChartSpec
  rows: Record<string, any>[]
  columns: string[]
  source: string
  kpi?: KpiPreview
}

export default function ReportsTab({ functionId, functionName, onAskAgent, onContextChange }: Props) {
  const setEntity = useChatStore((s) => s.setEntity)
  const setOpen = useChatStore((s) => s.setOpen)
  const setPageContext = useChatStore((s) => s.setPageContext)
  const [tiles, setTiles] = useState<PlotConfig[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [runs, setRuns] = useState<AnalyticsRun[]>([])
  const [previews, setPreviews] = useState<Record<string, PreviewBundle>>({})
  const [designerOpen, setDesignerOpen] = useState(false)
  const [agentDesignerOpen, setAgentDesignerOpen] = useState(false)
  const [editingTile, setEditingTile] = useState<PlotConfig | null>(null)
  const [loading, setLoading] = useState(true)

  const tunePile = (t: PlotConfig) => {
    // Bind the tile and open the chat — but do NOT auto-send a message.
    // A canned opener like "Tune the X tile." was being read by
    // plot-tuner as an instruction, prompting unsolicited mutations
    // before the analyst typed anything.
    setEntity('tile', t.id)
    setPageContext(`Tuning tile "${t.name}" — describe the change you want (sort, filter, chart type, colors, axis labels, font size, legend).`)
    setOpen(true)
  }

  // The "Explain" sparkle on a tile routes to either the tile-explainer or
  // the macro-economist (router decides based on the tile's variables).
  // Phrasing is deliberate: leads with "Explain", avoids any of the verbs
  // ("change", "switch", "filter to a bar/line", etc.) that trip the
  // tune-intent detector and bounce us into plot-tuner.
  const explainTile = (t: PlotConfig) => {
    setEntity('tile', t.id)
    setOpen(true)
    const kind = t.tile_type === 'kpi' ? 'KPI' : t.tile_type === 'table' ? 'table' : 'chart'
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `Explain this ${kind} ("${t.name}"). Cover the headline, trend or shape, the most notable outlier, and the so-what.`,
    }))
  }

  // Refresh a tile's preview when chat applies a filter
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { plot_id: string }
      if (!detail?.plot_id) return
      api.get(`/api/plots/${detail.plot_id}/preview`).then((r) => {
        const p = tiles.find((t) => t.id === detail.plot_id)
        if (!p) return
        const spec: ChartSpec = {
          id: p.id, title: p.name, type: p.chart_type,
          data: r.data.preview_data, x_key: p.x_field, y_keys: p.y_fields,
          description: p.description || null,
          style: r.data.plot?.style || p.style || null,
        }
        const cols: string[] =
          (r.data.columns || []).map((c: any) => c.name) ||
          (r.data.preview_data[0] ? Object.keys(r.data.preview_data[0]) : [])
        setPreviews((prev) => ({
          ...prev,
          [p.id]: { spec, rows: r.data.preview_data, columns: cols, source: r.data.source, kpi: r.data.kpi },
        }))
      })
    }
    window.addEventListener('cma-tile-updated', handler)
    return () => window.removeEventListener('cma-tile-updated', handler)
  }, [tiles])

  const load = () => {
    setLoading(true)
    Promise.all([
      api.get<PlotConfig[]>('/api/plots', { params: { function_id: functionId } }),
      api.get<Dataset[]>('/api/datasets', { params: { function_id: functionId } }),
      api.get<AnalyticsRun[]>('/api/analytics/runs', { params: { function_id: functionId } }),
    ])
      .then(([p, d, r]) => { setTiles(p.data); setDatasets(d.data); setRuns(r.data) })
      .finally(() => setLoading(false))
  }
  useEffect(load, [functionId])

  useEffect(() => {
    onContextChange(`${functionName} (Analytics): ${tiles.length} tile${tiles.length === 1 ? '' : 's'}`)
    return () => onContextChange(null)
  }, [tiles.length, functionName, onContextChange])

  // Render previews for saved tiles
  useEffect(() => {
    tiles.forEach((p) => {
      api.get(`/api/plots/${p.id}/preview`).then((r) => {
        const spec: ChartSpec = {
          id: p.id,
          title: p.name,
          type: p.chart_type,
          data: r.data.preview_data,
          x_key: p.x_field,
          y_keys: p.y_fields,
          description: p.description || null,
          style: r.data.plot?.style || p.style || null,
        }
        const cols: string[] =
          (r.data.columns || []).map((c: any) => c.name) ||
          (r.data.preview_data[0] ? Object.keys(r.data.preview_data[0]) : [])
        setPreviews((prev) => ({
          ...prev,
          [p.id]: { spec, rows: r.data.preview_data, columns: cols, source: r.data.source, kpi: r.data.kpi },
        }))
      })
    })
  }, [tiles])

  const remove = async (id: string) => {
    if (!confirm('Delete this tile?')) return
    await api.delete(`/api/plots/${id}`)
    setPreviews((p) => {
      const next = { ...p }
      delete next[id]
      return next
    })
    load()
  }

  const togglePin = async (id: string) => {
    await api.post(`/api/plots/${id}/pin`)
    load()
  }

  const openNew = () => { setEditingTile(null); setDesignerOpen(true) }
  const openEdit = (t: PlotConfig) => { setEditingTile(t); setDesignerOpen(true) }

  return (
    <div>
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <div>
          <div className="font-display text-base font-semibold" style={{ color: 'var(--text-primary)' }}>
            Tiles
          </div>
          <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {loading
              ? 'Loading…'
              : `${tiles.length} tile${tiles.length === 1 ? '' : 's'} · build plots or interactive tables from datasets, workflow outputs, or ad-hoc uploads.`}
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => onAskAgent(`Suggest a tile for ${functionName} based on what's available.`)}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{
              background: 'var(--bg-card)', border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
            }}
          >
            <Sparkles size={13} /> Ask Agent
          </button>
          <button
            onClick={() => setAgentDesignerOpen(true)}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{
              background: 'var(--bg-card)', border: '1px solid #7C3AED',
              color: '#7C3AED',
            }}
          >
            <Sparkles size={13} /> Design with AI
          </button>
          <button
            onClick={openNew}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{ background: 'var(--accent)', color: '#fff' }}
          >
            <Plus size={13} /> New Tile
          </button>
        </div>
      </div>

      {!loading && tiles.length === 0 && (
        <div
          className="panel text-center"
          style={{ padding: '40px 20px', borderStyle: 'dashed' }}
        >
          <BarChart3 size={28} style={{ color: 'var(--text-muted)', margin: '0 auto 12px' }} />
          <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
            No tiles yet
          </div>
          <div className="text-xs mt-1 mb-4" style={{ color: 'var(--text-muted)' }}>
            Build a plot or interactive table from any data source, or let AI design tiles from a narrative.
          </div>
          <div className="flex gap-2 justify-center">
            <button
              onClick={() => setAgentDesignerOpen(true)}
              className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
              style={{ background: 'var(--bg-card)', border: '1px solid #7C3AED', color: '#7C3AED' }}
            >
              <Sparkles size={13} /> Design with AI
            </button>
            <button
              onClick={openNew}
              className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              <Plus size={13} /> New Tile
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {tiles.map((p) => {
          const preview = previews[p.id]
          return (
            <TileCard
              key={p.id}
              tile={p}
              preview={preview}
              datasets={datasets}
              runs={runs}
              onEdit={() => openEdit(p)}
              onDelete={() => remove(p.id)}
              onAskAgent={() => explainTile(p)}
              onTogglePin={() => togglePin(p.id)}
              onTune={() => tunePile(p)}
            />
          )
        })}
      </div>

      {designerOpen && (
        <TileDesigner
          functionId={functionId}
          datasets={datasets}
          runs={runs}
          editing={editingTile}
          onClose={() => setDesignerOpen(false)}
          onSaved={() => { setDesignerOpen(false); load() }}
        />
      )}

      {agentDesignerOpen && (
        <AgentTileDesigner
          functionId={functionId}
          datasets={datasets}
          onClose={() => setAgentDesignerOpen(false)}
          onTilesCreated={() => { setAgentDesignerOpen(false); load() }}
        />
      )}
    </div>
  )
}

// ── Tile card ──────────────────────────────────────────────────────────
function TileCard({
  tile, preview, datasets, runs, onEdit, onDelete, onAskAgent, onTogglePin, onTune,
}: {
  tile: PlotConfig
  preview?: PreviewBundle
  datasets: Dataset[]
  runs: AnalyticsRun[]
  onEdit: () => void
  onDelete: () => void
  onAskAgent: () => void
  onTogglePin: () => void
  onTune: () => void
}) {
  const isTable = tile.tile_type === 'table'
  const isKpi = tile.tile_type === 'kpi'
  const sourceLabel = (() => {
    if (tile.dataset_id) {
      const d = datasets.find((x) => x.id === tile.dataset_id)
      return d ? `Dataset: ${d.name}` : 'Dataset'
    }
    if (tile.run_id) {
      const r = runs.find((x) => x.id === tile.run_id)
      return r ? `Workflow output: ${r.name}` : 'Workflow output'
    }
    return 'Sample data'
  })()

  return (
    <div className="panel">
      <div className="flex items-start justify-between mb-3 gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <div
              className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
              style={{
                background: isKpi ? 'rgba(124,58,237,0.10)' : isTable ? 'rgba(15,118,110,0.10)' : 'var(--accent-light)',
                color: isKpi ? '#7C3AED' : isTable ? '#0F766E' : 'var(--accent)',
              }}
            >
              {isKpi ? <Gauge size={14} /> : isTable ? <TableIcon size={14} /> : <BarChart3 size={14} />}
            </div>
            <div className="min-w-0">
              <div className="font-display text-base font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
                {tile.name}
              </div>
              <div className="text-[11px] truncate" style={{ color: 'var(--text-muted)' }}>
                {isKpi ? `KPI · ${tile.kpi_aggregation || 'sum'}(${tile.kpi_field || '?'})` : isTable ? 'Table' : tile.chart_type} · {sourceLabel}
                {preview?.source === 'live' && (
                  <span className="pill ml-2" style={{ fontSize: 9, background: 'var(--success-bg)', color: 'var(--success)', borderColor: 'transparent' }}>
                    LIVE
                  </span>
                )}
                {preview?.source === 'sample' && (
                  <span className="pill ml-2" style={{ fontSize: 9, background: 'var(--bg-elevated)', color: 'var(--text-muted)' }}>
                    SAMPLE
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>
        <div className="flex gap-1 shrink-0">
          <button
            onClick={onTune}
            className="px-2 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 transition-all"
            style={{
              background: 'rgba(15,118,110,0.10)',
              color: '#0F766E',
              border: '1px solid rgba(15,118,110,0.25)',
            }}
            title="Edit this tile with the Plot Tuner agent — sort, filter, change chart type, recolor, rename axes, change fonts"
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
          {(tile.filters || []).length > 0 && (
            <button
              onClick={async () => {
                await api.post(`/api/plots/${tile.id}/filters`, { clear: true })
                window.dispatchEvent(new CustomEvent('cma-tile-updated', { detail: { plot_id: tile.id } }))
              }}
              className="px-1.5 rounded-md text-[10px] font-bold flex items-center gap-1"
              style={{ background: 'rgba(15,118,110,0.10)', color: '#0F766E' }}
              title={`Clear ${tile.filters!.length} filter(s)`}
            >
              {tile.filters!.length}× ✕
            </button>
          )}
          <button
            onClick={onTogglePin}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: tile.pinned_to_overview ? 'var(--accent)' : 'var(--text-muted)' }}
            title={tile.pinned_to_overview ? 'Unpin from Overview' : 'Pin to Overview'}
          >
            {tile.pinned_to_overview ? <Pin size={13} fill="currentColor" /> : <Pin size={13} />}
          </button>
          <button
            onClick={onAskAgent}
            className="p-1.5 rounded-md"
            style={{ color: 'var(--text-muted)' }}
            title="Explain this chart"
          >
            <Sparkles size={13} />
          </button>
          <button onClick={onEdit} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }} title="Edit">
            <ZoomIn size={13} />
          </button>
          <button onClick={onDelete} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }} title="Delete">
            <Trash2 size={13} />
          </button>
        </div>
      </div>

      {!preview ? (
        <div
          className="flex items-center justify-center text-xs"
          style={{ height: 240, color: 'var(--text-muted)' }}
        >
          Loading preview…
        </div>
      ) : isKpi ? (
        <KpiTileRender kpi={preview.kpi} />
      ) : isTable ? (
        <InteractiveTable
          rows={preview.rows}
          columns={(tile.table_columns && tile.table_columns.length > 0)
            ? tile.table_columns
            : preview.columns}
          defaultSort={tile.table_default_sort || null}
          defaultSortDesc={!!tile.table_default_sort_desc}
          height={300}
        />
      ) : (
        <Chart spec={preview.spec} height={240} brushable />
      )}

      {tile.description && (
        <div className="text-xs mt-2" style={{ color: 'var(--text-muted)' }}>
          {tile.description}
        </div>
      )}
    </div>
  )
}

// ── Designer (slide-out) ──────────────────────────────────────────────
type SourceKind = 'dataset' | 'run' | 'sample'

function TileDesigner({
  functionId, datasets, runs, editing, onClose, onSaved,
}: {
  functionId: string
  datasets: Dataset[]
  runs: AnalyticsRun[]
  editing: PlotConfig | null
  onClose: () => void
  onSaved: () => void
}) {
  const isEditing = !!editing
  const [tileType, setTileType] = useState<PlotConfig['tile_type']>(editing?.tile_type || 'plot')
  const [name, setName] = useState(editing?.name || '')
  const [description, setDescription] = useState(editing?.description || '')
  const [chartType, setChartType] = useState<PlotConfig['chart_type']>(editing?.chart_type || 'line')

  const initialSourceKind: SourceKind =
    editing?.dataset_id ? 'dataset'
    : editing?.run_id ? 'run'
    : 'sample'
  const [sourceKind, setSourceKind] = useState<SourceKind>(initialSourceKind)
  const [datasetId, setDatasetId] = useState(editing?.dataset_id || datasets[0]?.id || '')
  const [runId, setRunId] = useState(editing?.run_id || runs[0]?.id || '')

  const [aggregation, setAggregation] = useState<PlotConfig['aggregation']>(editing?.aggregation || 'none')
  const [fields, setFields] = useState<string[]>([])
  const [xField, setXField] = useState(editing?.x_field || '')
  const [yFields, setYFields] = useState<string[]>(editing?.y_fields || [])
  const [tableColumns, setTableColumns] = useState<string[]>(editing?.table_columns || [])
  const [tableSort, setTableSort] = useState<string>(editing?.table_default_sort || '')
  const [tableSortDesc, setTableSortDesc] = useState<boolean>(!!editing?.table_default_sort_desc)

  // KPI tile config — single number computed from one column.
  const [kpiField, setKpiField] = useState(editing?.kpi_field || '')
  const [kpiAgg, setKpiAgg] = useState<NonNullable<PlotConfig['kpi_aggregation']>>(
    editing?.kpi_aggregation || 'latest',
  )
  const [kpiWeightField, setKpiWeightField] = useState(editing?.kpi_weight_field || '')
  const [kpiLatestField, setKpiLatestField] = useState(editing?.kpi_latest_field || '')
  const [kpiPrefix, setKpiPrefix] = useState(editing?.kpi_prefix || '')
  const [kpiSuffix, setKpiSuffix] = useState(editing?.kpi_suffix || '')
  const [kpiDecimals, setKpiDecimals] = useState<number>(editing?.kpi_decimals ?? 2)
  const [kpiScale, setKpiScale] = useState<number>(editing?.kpi_scale ?? 1)
  const [kpiSublabel, setKpiSublabel] = useState(editing?.kpi_sublabel || '')

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  // Load fields whenever the source changes; drop selections that don't exist
  // in the new source so the live preview / saved chart never trails old lines.
  useEffect(() => {
    const params: Record<string, string> = {}
    if (sourceKind === 'dataset' && datasetId) params.dataset_id = datasetId
    else if (sourceKind === 'run' && runId) params.run_id = runId
    else params.data_source_id = 'ds-snowflake-prod'
    api.get<{ fields: string[] }>('/api/plots/fields', { params })
      .then((r) => {
        const fs = r.data.fields ?? []
        setFields(fs)
        // Prune stale selections — keep names that still exist; drop names that don't
        setXField((prev) => (prev && fs.includes(prev) ? prev : ''))
        setYFields((prev) => prev.filter((f) => fs.includes(f)))
        setTableColumns((prev) => {
          const kept = prev.filter((f) => fs.includes(f))
          // Auto-select all columns for a fresh table tile (no prior selection)
          if (!isEditing && tileType === 'table' && kept.length === 0 && fs.length > 0) {
            return fs
          }
          return kept
        })
      })
  }, [sourceKind, datasetId, runId])

  const toggleY = (f: string) => {
    setYFields((prev) => (prev.includes(f) ? prev.filter((y) => y !== f) : [...prev, f]))
  }
  const toggleColumn = (f: string) => {
    setTableColumns((prev) => (prev.includes(f) ? prev.filter((y) => y !== f) : [...prev, f]))
  }

  const handleAdHocUpload = async (file: File) => {
    setUploading(true)
    setError(null)
    try {
      const fd = new FormData()
      fd.append('function_id', functionId)
      fd.append('file', file)
      fd.append('name', `[ad-hoc] ${file.name.split('.').slice(0, -1).join('.') || file.name}`)
      fd.append('description', 'Ad-hoc upload from Analytics tile designer')
      const r = await api.post<Dataset>('/api/datasets/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      // Add to local datasets list and select it
      datasets.unshift(r.data)
      setSourceKind('dataset')
      setDatasetId(r.data.id)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const submit = async () => {
    if (!name) { setError('Tile name is required'); return }
    if (tileType === 'plot' && (!xField || yFields.length === 0)) {
      setError('Plot tiles need an X field and at least one Y field'); return
    }
    if (tileType === 'table' && tableColumns.length === 0) {
      setError('Table tiles need at least one column selected'); return
    }
    if (tileType === 'kpi' && !kpiField) {
      setError('KPI tiles need a metric field selected'); return
    }
    if (tileType === 'kpi' && kpiAgg === 'weighted_avg' && !kpiWeightField) {
      setError('Weighted average needs a weight field'); return
    }
    setSaving(true)
    setError(null)
    try {
      const body = {
        function_id: functionId,
        name,
        tile_type: tileType,
        chart_type: chartType,
        dataset_id: sourceKind === 'dataset' ? datasetId : null,
        run_id: sourceKind === 'run' ? runId : null,
        data_source_id: sourceKind === 'sample' ? 'ds-snowflake-prod' : null,
        pinned_to_overview: editing?.pinned_to_overview || false,
        x_field: tileType === 'plot' ? xField : '',
        y_fields: tileType === 'plot' ? yFields : [],
        aggregation: tileType === 'plot' ? aggregation : 'none',
        table_columns: tileType === 'table' ? tableColumns : null,
        table_default_sort: tileType === 'table' && tableSort ? tableSort : null,
        table_default_sort_desc: tableSortDesc,
        filters: [],
        description,
        // KPI fields — sent regardless of type so a user can flip an
        // existing tile to/from KPI without losing the configuration.
        kpi_field: kpiField,
        kpi_aggregation: kpiAgg,
        kpi_weight_field: kpiWeightField || null,
        kpi_latest_field: kpiLatestField || null,
        kpi_prefix: kpiPrefix,
        kpi_suffix: kpiSuffix,
        kpi_decimals: kpiDecimals,
        kpi_scale: kpiScale,
        kpi_sublabel: kpiSublabel || null,
      }
      if (isEditing) {
        // For edits, just delete + recreate to keep code simple (no PATCH endpoint yet)
        await api.delete(`/api/plots/${editing!.id}`)
      }
      await api.post('/api/plots', body)
      onSaved()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  // Live preview spec
  const previewSpec: ChartSpec | null = (() => {
    if (tileType !== 'plot' || !xField || yFields.length === 0) return null
    const sample =
      chartType === 'pie'
        ? [
            { [xField]: 'A', [yFields[0]]: 38 },
            { [xField]: 'B', [yFields[0]]: 27 },
            { [xField]: 'C', [yFields[0]]: 18 },
            { [xField]: 'D', [yFields[0]]: 17 },
          ]
        : ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'].map((m, i) => ({
            [xField]: m,
            ...Object.fromEntries(yFields.map((y, j) => [y, 100 + i * 12 + j * 8])),
          }))
    return {
      id: 'preview', title: name || 'Preview', type: chartType,
      data: sample, x_key: xField, y_keys: yFields,
    }
  })()

  // Sample table rows for live preview
  const previewTableRows = useMemo(() => {
    if (tileType !== 'table' || tableColumns.length === 0) return []
    return Array.from({ length: 8 }, (_, i) => Object.fromEntries(
      tableColumns.map((c, j) => [c, j === 0 ? `row-${i + 1}` : Math.round(Math.random() * 10000) / 100])
    ))
  }, [tileType, tableColumns])

  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: 'rgba(11,15,25,0.45)' }} onClick={onClose} />
      <div
        className="fixed top-0 right-0 bottom-0 z-50 flex flex-col"
        style={{
          width: 'min(880px, 92vw)',
          background: 'var(--bg-card)', borderLeft: '1px solid var(--border)',
          boxShadow: '-12px 0 48px rgba(0,0,0,0.18)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-4"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--teal))',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>
            {isEditing ? 'Edit Tile' : 'Design Tile'}
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'rgba(255,255,255,0.85)' }}>
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto grid grid-cols-1 lg:grid-cols-5 gap-0">
          {/* Designer column */}
          <div className="lg:col-span-2 p-5 space-y-4" style={{ borderRight: '1px solid var(--border)' }}>
            <Field label="Tile Name">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. NAV by Sector" />
            </Field>

            <Field label="Tile Type">
              <div className="grid grid-cols-3 gap-1 p-1 rounded-lg" style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
                {([
                  { k: 'plot', l: 'Plot', I: BarChart3 },
                  { k: 'table', l: 'Table', I: TableIcon },
                  { k: 'kpi', l: 'KPI', I: Gauge },
                ] as const).map(({ k, l, I }) => {
                  const active = tileType === k
                  return (
                    <button
                      key={k}
                      onClick={() => setTileType(k as PlotConfig['tile_type'])}
                      className="py-2 rounded-md text-xs font-semibold transition-colors flex items-center justify-center gap-1.5"
                      style={{
                        background: active ? 'var(--bg-card)' : 'transparent',
                        color: active ? 'var(--accent)' : 'var(--text-secondary)',
                        boxShadow: active ? '0 1px 4px rgba(0,0,0,0.05)' : 'none',
                      }}
                    >
                      <I size={12} /> {l}
                    </button>
                  )
                })}
              </div>
            </Field>

            <Field label="Source">
              <div className="grid grid-cols-3 gap-1 mb-2 p-1 rounded-lg" style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
                {([
                  { k: 'dataset', l: 'Bound Dataset', I: Database, count: datasets.length, disabled: datasets.length === 0 },
                  { k: 'run',     l: 'Workflow Output', I: FlaskConical, count: runs.length, disabled: runs.length === 0 },
                  { k: 'sample',  l: 'Sample',  I: BarChart3, count: 0, disabled: false },
                ] as const).map(({ k, l, I, disabled }) => {
                  const active = sourceKind === k
                  return (
                    <button
                      key={k}
                      onClick={() => setSourceKind(k as SourceKind)}
                      disabled={disabled}
                      className="py-2 rounded-md text-[11px] font-semibold transition-colors flex items-center justify-center gap-1 disabled:opacity-40"
                      style={{
                        background: active ? 'var(--bg-card)' : 'transparent',
                        color: active ? 'var(--accent)' : 'var(--text-secondary)',
                        boxShadow: active ? '0 1px 4px rgba(0,0,0,0.05)' : 'none',
                      }}
                    >
                      <I size={11} /> {l}
                    </button>
                  )
                })}
              </div>

              {sourceKind === 'dataset' && (
                <>
                  <select className="input" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                    {datasets.length === 0 && <option value="">No datasets bound</option>}
                    {datasets.map((d) => (
                      <option key={d.id} value={d.id}>{d.name}</option>
                    ))}
                  </select>
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".csv,.parquet,.xlsx,.xls,.json"
                    onChange={(e) => {
                      const f = e.target.files?.[0]
                      if (f) handleAdHocUpload(f)
                      if (fileRef.current) fileRef.current.value = ''
                    }}
                    className="hidden"
                  />
                  <button
                    onClick={() => fileRef.current?.click()}
                    disabled={uploading}
                    className="text-[11px] mt-2 flex items-center gap-1 disabled:opacity-50"
                    style={{ color: 'var(--accent)', fontWeight: 600 }}
                  >
                    <Upload size={11} /> {uploading ? 'Uploading…' : 'Or upload an ad-hoc file (CSV / Parquet / XLSX / JSON)'}
                  </button>
                </>
              )}
              {sourceKind === 'run' && (
                <select className="input" value={runId} onChange={(e) => setRunId(e.target.value)}>
                  {runs.length === 0 && <option value="">No workflow outputs yet</option>}
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>{r.name}</option>
                  ))}
                </select>
              )}
              {sourceKind === 'sample' && (
                <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                  Synthetic data — useful for sketching layouts.
                </div>
              )}
            </Field>

            {tileType === 'kpi' ? (
              <>
                <Field label="Metric Field">
                  <select className="input" value={kpiField} onChange={(e) => setKpiField(e.target.value)}>
                    <option value="">Select…</option>
                    {fields.map((f) => <option key={f} value={f}>{f}</option>)}
                  </select>
                </Field>

                <Field label="Aggregation">
                  <select
                    className="input"
                    value={kpiAgg}
                    onChange={(e) => setKpiAgg(e.target.value as any)}
                  >
                    <option value="latest">Latest (most recent row)</option>
                    <option value="sum">Sum</option>
                    <option value="avg">Average</option>
                    <option value="weighted_avg">Weighted Average</option>
                    <option value="min">Min</option>
                    <option value="max">Max</option>
                    <option value="count">Count</option>
                  </select>
                </Field>

                {kpiAgg === 'weighted_avg' && (
                  <Field label="Weight Field">
                    <select className="input" value={kpiWeightField} onChange={(e) => setKpiWeightField(e.target.value)}>
                      <option value="">Select…</option>
                      {fields.filter((f) => f !== kpiField).map((f) => <option key={f} value={f}>{f}</option>)}
                    </select>
                  </Field>
                )}

                {kpiAgg === 'latest' && (
                  <Field label="Sort Field (defines 'latest')">
                    <select className="input" value={kpiLatestField} onChange={(e) => setKpiLatestField(e.target.value)}>
                      <option value="">Use the metric field itself</option>
                      {fields.filter((f) => f !== kpiField).map((f) => <option key={f} value={f}>{f}</option>)}
                    </select>
                  </Field>
                )}

                <div className="grid grid-cols-2 gap-2">
                  <Field label="Prefix">
                    <input className="input" value={kpiPrefix} onChange={(e) => setKpiPrefix(e.target.value)} placeholder="$" />
                  </Field>
                  <Field label="Suffix">
                    <input className="input" value={kpiSuffix} onChange={(e) => setKpiSuffix(e.target.value)} placeholder="B / % / bps" />
                  </Field>
                </div>

                <div className="grid grid-cols-2 gap-2">
                  <Field label="Decimals">
                    <input
                      type="number" min={0} max={6} className="input"
                      value={kpiDecimals}
                      onChange={(e) => setKpiDecimals(Math.max(0, Math.min(6, parseInt(e.target.value) || 0)))}
                    />
                  </Field>
                  <Field label="Scale (× value)">
                    <input
                      type="number" step="0.001" className="input"
                      value={kpiScale}
                      onChange={(e) => setKpiScale(parseFloat(e.target.value) || 1)}
                      placeholder="1 — use 0.001 to convert MM to B"
                    />
                  </Field>
                </div>

                <Field label="Sublabel (optional)">
                  <input
                    className="input" value={kpiSublabel}
                    onChange={(e) => setKpiSublabel(e.target.value)}
                    placeholder="e.g. weighted, vs prior month"
                  />
                </Field>
              </>
            ) : tileType === 'plot' ? (
              <>
                <Field label="Chart Type">
                  <div className="grid grid-cols-3 gap-2">
                    {CHART_TYPES.map(({ id, label, icon: Icon }) => {
                      const active = chartType === id
                      return (
                        <button
                          key={id}
                          onClick={() => setChartType(id)}
                          className="flex flex-col items-center gap-1 py-2 rounded-lg text-[11px] font-medium transition-all"
                          style={{
                            background: active ? 'var(--accent-light)' : 'var(--bg-elevated)',
                            border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                            color: active ? 'var(--accent)' : 'var(--text-secondary)',
                          }}
                        >
                          <Icon size={14} />
                          {label}
                        </button>
                      )
                    })}
                  </div>
                </Field>

                <Field label="X Axis Field">
                  <select className="input" value={xField} onChange={(e) => setXField(e.target.value)}>
                    <option value="">Select…</option>
                    {fields.map((f) => <option key={f} value={f}>{f}</option>)}
                  </select>
                </Field>

                <Field label={`Y Axis Fields (${yFields.length} selected)`}>
                  <FieldChecklist
                    fields={fields.filter((f) => f !== xField)}
                    selected={yFields}
                    onToggle={toggleY}
                  />
                </Field>

                <Field label="Aggregation">
                  <select
                    className="input"
                    value={aggregation}
                    onChange={(e) => setAggregation(e.target.value as PlotConfig['aggregation'])}
                  >
                    <option value="none">None</option>
                    <option value="sum">Sum</option>
                    <option value="avg">Average</option>
                    <option value="count">Count</option>
                    <option value="min">Min</option>
                    <option value="max">Max</option>
                  </select>
                </Field>
              </>
            ) : (
              <>
                <Field label={`Columns (${tableColumns.length} selected)`}>
                  <div className="flex justify-end mb-1 gap-2 text-[11px]">
                    <button
                      onClick={() => setTableColumns(fields)}
                      style={{ color: 'var(--accent)', fontWeight: 600 }}
                    >
                      Select all
                    </button>
                    <button
                      onClick={() => setTableColumns([])}
                      style={{ color: 'var(--text-muted)' }}
                    >
                      Clear
                    </button>
                  </div>
                  <FieldChecklist
                    fields={fields}
                    selected={tableColumns}
                    onToggle={toggleColumn}
                  />
                </Field>

                <Field label="Default Sort (optional)">
                  <div className="flex gap-2">
                    <select className="input flex-1" value={tableSort} onChange={(e) => setTableSort(e.target.value)}>
                      <option value="">No default sort</option>
                      {tableColumns.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                    <select
                      className="input"
                      style={{ width: 120 }}
                      value={tableSortDesc ? 'desc' : 'asc'}
                      onChange={(e) => setTableSortDesc(e.target.value === 'desc')}
                    >
                      <option value="asc">Ascending</option>
                      <option value="desc">Descending</option>
                    </select>
                  </div>
                </Field>
              </>
            )}

            <Field label="Description (optional)">
              <textarea
                rows={2}
                className="input resize-none"
                value={description || ''}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>

            {error && (
              <div className="text-xs px-3 py-2 rounded-md" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
                {error}
              </div>
            )}
          </div>

          {/* Live preview column */}
          <div className="lg:col-span-3 p-5 space-y-3">
            <div className="font-display text-base font-semibold" style={{ color: 'var(--text-primary)' }}>
              Live Preview
            </div>
            <div
              className="panel"
              style={{ minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            >
              {tileType === 'kpi' ? (
                kpiField ? (
                  <div className="text-center" style={{ width: '100%' }}>
                    <div
                      className="text-[10px] font-semibold uppercase tracking-widest mb-2"
                      style={{ color: 'var(--text-secondary)' }}
                    >
                      {name || 'KPI'}
                    </div>
                    <div
                      className="font-mono"
                      style={{
                        fontSize: 36, fontWeight: 700,
                        color: 'var(--text-primary)',
                        letterSpacing: '-0.02em',
                      }}
                    >
                      {kpiPrefix}
                      {(123.456 * (kpiScale || 1)).toLocaleString(undefined, {
                        minimumFractionDigits: kpiDecimals,
                        maximumFractionDigits: kpiDecimals,
                      })}
                      {kpiSuffix}
                    </div>
                    {kpiSublabel && (
                      <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
                        {kpiSublabel}
                      </div>
                    )}
                    <div className="text-[11px] mt-3" style={{ color: 'var(--text-muted)' }}>
                      Sample value — actual rendering uses {kpiAgg.replace('_', ' ')} of <code style={{ background: 'var(--accent-light)', padding: '0 4px', borderRadius: 3, color: 'var(--accent)' }}>{kpiField}</code> from the source you picked.
                    </div>
                  </div>
                ) : (
                  <div className="text-sm" style={{ color: 'var(--text-muted)' }}>
                    Pick a metric field to see a live preview.
                  </div>
                )
              ) : tileType === 'plot' && previewSpec ? (
                <div style={{ width: '100%' }}>
                  <Chart spec={previewSpec} height={320} brushable />
                  <div className="text-[11px] mt-2 text-center" style={{ color: 'var(--text-muted)' }}>
                    Sample data — actual rendering uses the source you picked when saved.
                  </div>
                </div>
              ) : tileType === 'table' && previewTableRows.length > 0 ? (
                <div style={{ width: '100%' }}>
                  <InteractiveTable
                    rows={previewTableRows}
                    columns={tableColumns}
                    defaultSort={tableSort || null}
                    defaultSortDesc={tableSortDesc}
                    height={340}
                  />
                  <div className="text-[11px] mt-2 text-center" style={{ color: 'var(--text-muted)' }}>
                    Sample data — actual rendering uses the source you picked when saved.
                  </div>
                </div>
              ) : (
                <div className="text-sm" style={{ color: 'var(--text-muted)' }}>
                  {tileType === 'plot'
                    ? 'Pick a chart type, X / Y fields to see a live preview.'
                    : 'Select at least one column to see a live preview.'}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="flex justify-between gap-2 px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
          <div
            className="text-[11px] flex items-center gap-1"
            style={{ color: 'var(--text-muted)' }}
          >
            <Sparkles size={11} /> Tip: workflow outputs (destination datasets) are listed under "Workflow Output".
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-3 py-2 text-xs rounded-lg" style={{ color: 'var(--text-muted)' }}>
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={!name || saving}
              className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              {saving ? 'Saving…' : isEditing ? 'Save Changes' : 'Save Tile'}
            </button>
          </div>
        </div>
      </div>

      <style>{`
        .input {
          width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px;
          background: var(--bg-elevated); border: 1px solid var(--border); color: var(--text-primary);
        }
      `}</style>
    </>
  )
}

function KpiTileRender({ kpi }: { kpi?: KpiPreview }) {
  if (!kpi || kpi.value == null) {
    return (
      <div
        className="flex items-center justify-center text-xs"
        style={{ height: 200, color: 'var(--text-muted)' }}
      >
        No data — pick a metric and source.
      </div>
    )
  }
  return (
    <div
      className="flex flex-col items-start justify-center"
      style={{ minHeight: 180, padding: '8px 4px' }}
    >
      <div
        className="font-mono"
        style={{
          fontSize: 36, fontWeight: 700,
          color: 'var(--text-primary)',
          letterSpacing: '-0.02em', lineHeight: 1.1,
        }}
      >
        {kpi.display}
      </div>
      {kpi.sublabel && (
        <div className="text-xs mt-2" style={{ color: 'var(--text-muted)' }}>
          {kpi.sublabel}
        </div>
      )}
    </div>
  )
}

function FieldChecklist({
  fields, selected, onToggle,
}: {
  fields: string[]
  selected: string[]
  onToggle: (f: string) => void
}) {
  return (
    <div
      className="grid grid-cols-2 gap-1 p-2 rounded-lg"
      style={{
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        maxHeight: 220, overflowY: 'auto',
      }}
    >
      {fields.length === 0 && (
        <div className="text-xs col-span-2 py-2" style={{ color: 'var(--text-muted)' }}>
          Pick a source to see fields
        </div>
      )}
      {fields.map((f) => {
        const sel = selected.includes(f)
        return (
          <label
            key={f}
            className="flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer"
            style={{ background: sel ? 'var(--accent-light)' : 'transparent', color: sel ? 'var(--accent)' : 'var(--text-secondary)' }}
          >
            <input type="checkbox" checked={sel} onChange={() => onToggle(f)} />
            {f}
          </label>
        )
      })}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span
        className="block text-[11px] font-semibold uppercase tracking-widest mb-1"
        style={{ color: 'var(--text-secondary)' }}
      >
        {label}
      </span>
      {children}
    </label>
  )
}

