/**
 * AgentTileDesigner — modal that accepts a natural-language narrative and calls
 * the reporting-tile-designer skill to generate PlotConfig blueprints.
 * The analyst previews the proposed tiles and can add them to the dashboard
 * individually or all at once.
 */
import { useState } from 'react'
import {
  X, Sparkles, Loader2, Plus, CheckCircle2, BarChart3,
  Table as TableIcon, Gauge, Code2, ChevronDown, ChevronUp,
} from 'lucide-react'
import api from '@/lib/api'
import type { Dataset, PlotConfig } from '@/types'

interface TileBlueprint {
  tile_type: 'plot' | 'table' | 'kpi'
  name: string
  chart_type?: string
  x_field?: string
  y_fields?: string[]
  aggregation?: string
  filters?: Record<string, any>[]
  kpi_field?: string
  kpi_aggregation?: string
  kpi_prefix?: string
  kpi_suffix?: string
  description?: string
  python_snippet?: string
}

interface DesignerResponse {
  tiles: TileBlueprint[]
  narrative_summary: string
  dataset_id?: string | null
}

interface Props {
  functionId: string
  datasets: Dataset[]
  onClose: () => void
  onTilesCreated: () => void
}

const TYPE_ICON: Record<string, any> = {
  plot: BarChart3,
  table: TableIcon,
  kpi: Gauge,
}

const TYPE_COLOR: Record<string, string> = {
  plot: '#0891B2',
  table: '#059669',
  kpi: '#D97706',
}

export default function AgentTileDesigner({ functionId, datasets, onClose, onTilesCreated }: Props) {
  const [narrative, setNarrative] = useState('')
  const [datasetId, setDatasetId] = useState(datasets[0]?.id || '')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DesignerResponse | null>(null)
  const [added, setAdded] = useState<Set<number>>(new Set())
  const [adding, setAdding] = useState<Set<number>>(new Set())
  const [expandedCode, setExpandedCode] = useState<Set<number>>(new Set())

  const generate = async () => {
    if (!narrative.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    setAdded(new Set())
    try {
      const r = await api.post<DesignerResponse>('/api/tile-designer/generate', {
        function_id: functionId,
        narrative: narrative.trim(),
        dataset_id: datasetId || null,
      })
      setResult(r.data)
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Generation failed')
    } finally {
      setLoading(false)
    }
  }

  const addTile = async (blueprint: TileBlueprint, idx: number) => {
    if (added.has(idx)) return
    setAdding((prev) => new Set(prev).add(idx))
    try {
      const payload: Record<string, any> = {
        function_id: functionId,
        name: blueprint.name,
        tile_type: blueprint.tile_type,
        chart_type: blueprint.chart_type || 'line',
        dataset_id: datasetId || result?.dataset_id || null,
        x_field: blueprint.x_field || 'snap_date',
        y_fields: blueprint.y_fields || ['variable_value'],
        aggregation: blueprint.aggregation || 'none',
        filters: blueprint.filters || [],
        description: blueprint.description || '',
      }
      if (blueprint.tile_type === 'kpi') {
        payload.kpi_field = blueprint.kpi_field || 'variable_value'
        payload.kpi_aggregation = blueprint.kpi_aggregation || 'latest'
        payload.kpi_prefix = blueprint.kpi_prefix || ''
        payload.kpi_suffix = blueprint.kpi_suffix || ''
      }
      await api.post<PlotConfig>('/api/plots', payload)
      setAdded((prev) => new Set(prev).add(idx))
    } catch (e: any) {
      setError(`Failed to add "${blueprint.name}": ${e?.response?.data?.detail || e?.message}`)
    } finally {
      setAdding((prev) => { const s = new Set(prev); s.delete(idx); return s })
    }
  }

  const addAll = async () => {
    if (!result) return
    for (let i = 0; i < result.tiles.length; i++) {
      if (!added.has(i)) {
        await addTile(result.tiles[i], i)
      }
    }
    onTilesCreated()
  }

  const toggleCode = (idx: number) => {
    setExpandedCode((prev) => {
      const s = new Set(prev)
      s.has(idx) ? s.delete(idx) : s.add(idx)
      return s
    })
  }

  const allAdded = result && result.tiles.length > 0 && result.tiles.every((_, i) => added.has(i))

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0,0,0,0.55)' }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="relative flex flex-col rounded-xl shadow-2xl"
        style={{
          width: '780px', maxWidth: '95vw', maxHeight: '88vh',
          background: 'var(--bg-card)', border: '1px solid var(--border)',
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-5 py-4 border-b"
          style={{ borderColor: 'var(--border)' }}
        >
          <div className="flex items-center gap-2">
            <Sparkles size={16} style={{ color: '#7C3AED' }} />
            <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
              AI Tile Designer
            </span>
          </div>
          <button onClick={onClose} style={{ color: 'var(--text-muted)' }}>
            <X size={16} />
          </button>
        </div>

        <div className="flex flex-col gap-4 overflow-y-auto p-5">
          {/* Input area */}
          <div className="flex flex-col gap-3">
            <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Describe the tiles you want. The agent will generate plots, tables, and KPI cards
              from your macro reporting dataset.
            </div>

            <textarea
              value={narrative}
              onChange={(e) => setNarrative(e.target.value)}
              placeholder="e.g. Show GDP and unemployment trends by scenario, add a KPI for the latest Fed Funds rate under the adverse scenario, and a table comparing all variables at year-end."
              rows={3}
              className="w-full rounded-lg px-3 py-2 text-sm resize-none"
              style={{
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                color: 'var(--text-primary)', outline: 'none',
              }}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) generate() }}
            />

            <div className="flex items-center gap-2">
              {datasets.length > 0 && (
                <select
                  value={datasetId}
                  onChange={(e) => setDatasetId(e.target.value)}
                  className="flex-1 rounded-lg px-3 py-2 text-xs"
                  style={{
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--text-secondary)', maxWidth: 260,
                  }}
                >
                  <option value="">— sample macro data —</option>
                  {datasets.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              )}
              <button
                onClick={generate}
                disabled={loading || !narrative.trim()}
                className="px-4 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 ml-auto"
                style={{
                  background: loading || !narrative.trim() ? 'var(--bg-elevated)' : '#7C3AED',
                  color: loading || !narrative.trim() ? 'var(--text-muted)' : '#fff',
                  border: '1px solid var(--border)',
                  cursor: loading || !narrative.trim() ? 'not-allowed' : 'pointer',
                }}
              >
                {loading
                  ? <><Loader2 size={12} className="animate-spin" /> Designing…</>
                  : <><Sparkles size={12} /> Generate Tiles</>}
              </button>
            </div>
          </div>

          {error && (
            <div
              className="text-xs px-3 py-2 rounded-lg"
              style={{ background: '#FEF2F2', color: '#DC2626', border: '1px solid #FCA5A5' }}
            >
              {error}
            </div>
          )}

          {/* Results */}
          {result && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span className="font-semibold" style={{ color: 'var(--text-secondary)' }}>
                    {result.tiles.length} tile{result.tiles.length !== 1 ? 's' : ''} designed
                  </span>
                  {' · '}
                  {result.narrative_summary}
                </div>
                {!allAdded && result.tiles.length > 0 && (
                  <button
                    onClick={addAll}
                    className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1"
                    style={{ background: 'var(--accent)', color: '#fff' }}
                  >
                    <Plus size={11} /> Add All
                  </button>
                )}
                {allAdded && (
                  <div className="flex items-center gap-1 text-xs" style={{ color: '#059669' }}>
                    <CheckCircle2 size={13} /> All added
                  </div>
                )}
              </div>

              <div className="flex flex-col gap-3">
                {result.tiles.map((tile, idx) => {
                  const Icon = TYPE_ICON[tile.tile_type] || BarChart3
                  const color = TYPE_COLOR[tile.tile_type] || '#004977'
                  const isAdded = added.has(idx)
                  const isAdding = adding.has(idx)
                  const codeOpen = expandedCode.has(idx)

                  return (
                    <div
                      key={idx}
                      className="rounded-lg p-4"
                      style={{
                        background: 'var(--bg-elevated)',
                        border: `1px solid ${isAdded ? '#BBF7D0' : 'var(--border)'}`,
                      }}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex items-start gap-2 min-w-0">
                          <div
                            className="rounded p-1 mt-0.5 flex-shrink-0"
                            style={{ background: color + '20' }}
                          >
                            <Icon size={13} style={{ color }} />
                          </div>
                          <div className="min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                                {tile.name}
                              </span>
                              <span
                                className="text-[10px] px-1.5 py-0.5 rounded font-mono uppercase"
                                style={{ background: color + '15', color }}
                              >
                                {tile.tile_type}{tile.chart_type && tile.tile_type !== 'kpi' && tile.tile_type !== 'table' ? ` · ${tile.chart_type}` : ''}
                              </span>
                            </div>
                            {tile.description && (
                              <div className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
                                {tile.description}
                              </div>
                            )}
                            <div className="flex flex-wrap gap-x-3 mt-1">
                              {tile.x_field && (
                                <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                                  x: <code>{tile.x_field}</code>
                                </span>
                              )}
                              {(tile.y_fields || []).length > 0 && (
                                <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                                  y: <code>{(tile.y_fields || []).join(', ')}</code>
                                </span>
                              )}
                              {(tile.filters || []).length > 0 && (
                                <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                                  filters: {(tile.filters || []).map((f) => `${f.field}=${f.value}`).join(', ')}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>

                        <div className="flex items-center gap-2 flex-shrink-0">
                          {tile.python_snippet && (
                            <button
                              onClick={() => toggleCode(idx)}
                              className="text-[11px] flex items-center gap-1 px-2 py-1 rounded"
                              style={{
                                color: 'var(--text-muted)',
                                background: 'var(--bg-card)',
                                border: '1px solid var(--border)',
                              }}
                            >
                              <Code2 size={11} />
                              {codeOpen ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                            </button>
                          )}
                          <button
                            onClick={() => addTile(tile, idx)}
                            disabled={isAdded || isAdding}
                            className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1"
                            style={{
                              background: isAdded ? '#F0FDF4' : 'var(--accent)',
                              color: isAdded ? '#059669' : '#fff',
                              border: isAdded ? '1px solid #BBF7D0' : 'none',
                              cursor: isAdded ? 'default' : 'pointer',
                            }}
                          >
                            {isAdding
                              ? <Loader2 size={11} className="animate-spin" />
                              : isAdded
                              ? <><CheckCircle2 size={11} /> Added</>
                              : <><Plus size={11} /> Add</>}
                          </button>
                        </div>
                      </div>

                      {codeOpen && tile.python_snippet && (
                        <pre
                          className="mt-3 rounded-lg p-3 text-[11px] overflow-x-auto"
                          style={{
                            background: '#1E1E2E',
                            color: '#CDD6F4',
                            border: '1px solid var(--border)',
                            fontFamily: 'ui-monospace, monospace',
                          }}
                        >
                          {tile.python_snippet}
                        </pre>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          className="px-5 py-3 border-t flex items-center justify-between"
          style={{ borderColor: 'var(--border)' }}
        >
          <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            Powered by <span style={{ color: '#7C3AED' }}>reporting-tile-designer</span> skill ·
            {' '}Expected schema: scenario, snap_date, variable_name, variable_value, segment, origin
          </div>
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-xs"
            style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)' }}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
