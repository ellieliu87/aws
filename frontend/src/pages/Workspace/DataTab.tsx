import { useEffect, useRef, useState } from 'react'
import {
  Database, FileText, Trash2, Upload, Plus, X, Search,
  Snowflake, Cloud, HardDrive, Globe, Eye, Sparkles, Loader2,
  Layers, ShieldCheck,
} from 'lucide-react'
import api from '@/lib/api'
import { useChatStore } from '@/store/chatStore'
import type {
  Dataset, DatasetPreview, DataSource, DataSourceTable,
} from '@/types'
import DataServicesSection from './DataServicesSection'

type SectionId = 'datasets' | 'data_services'

interface Props {
  functionId: string
  functionName: string
  onAskAgent: (q: string) => void
  onContextChange: (ctx: string | null) => void
}

export default function DataTab({ functionId, functionName, onAskAgent, onContextChange }: Props) {
  const [section, setSection] = useState<SectionId>('datasets')
  const [datasetCount, setDatasetCount] = useState<number>(0)

  // Lightweight count fetch so the toggle pill stays accurate
  useEffect(() => {
    api
      .get(`/api/datasets`, { params: { function_id: functionId } })
      .then((d) => setDatasetCount(d.data.length))
      .catch(() => {})
  }, [functionId, section])

  useEffect(() => {
    onContextChange(`${functionName} (Data tab · ${section}): ${datasetCount} datasets`)
    return () => onContextChange(null)
  }, [section, datasetCount, functionName, onContextChange])

  return (
    <div>
      {/* Section toggle */}
      <div
        className="inline-flex p-1 rounded-lg mb-5"
        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
      >
        {([
          { id: 'datasets',      label: 'Datasets',      icon: Database, count: datasetCount },
          { id: 'data_services', label: 'Data Services', icon: Layers,   count: null },
        ] as const).map(({ id, label, icon: Icon, count }) => {
          const active = section === id
          return (
            <button
              key={id}
              onClick={() => setSection(id as SectionId)}
              className="px-3.5 py-1.5 rounded-md text-xs font-semibold transition-colors flex items-center gap-1.5"
              style={{
                background: active ? 'var(--bg-card)' : 'transparent',
                color: active ? 'var(--accent)' : 'var(--text-secondary)',
                boxShadow: active ? '0 1px 4px rgba(0,0,0,0.06)' : 'none',
              }}
            >
              <Icon size={13} />
              {label}
              {count != null && (
                <span
                  className="rounded-full px-1.5 py-0.5"
                  style={{
                    fontSize: 10, fontWeight: 700,
                    background: active ? 'var(--accent-light)' : 'var(--bg-elevated)',
                    color: active ? 'var(--accent)' : 'var(--text-muted)',
                    marginLeft: 2,
                  }}
                >
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {section === 'datasets' && (
        <DatasetsSection
          functionId={functionId}
          functionName={functionName}
          onAskAgent={onAskAgent}
        />
      )}
      {section === 'data_services' && (
        <DataServicesSection
          functionId={functionId}
          functionName={functionName}
          onAskAgent={onAskAgent}
        />
      )}
    </div>
  )
}

const SOURCE_TYPE_META: Record<DataSource['type'], { icon: any; color: string }> = {
  snowflake:   { icon: Snowflake, color: '#29B5E8' },
  onelake:     { icon: Cloud,     color: '#0078D4' },
  postgres:    { icon: Database,  color: '#336791' },
  s3:          { icon: HardDrive, color: '#D97706' },
  rest_api:    { icon: Globe,     color: '#059669' },
  file_upload: { icon: FileText,  color: '#7C3AED' },
}

const FORMAT_LABEL: Record<string, string> = {
  csv: 'CSV', parquet: 'Parquet', xlsx: 'XLSX', xls: 'XLS', json: 'JSON',
}

interface SectionProps {
  functionId: string
  functionName: string
  onAskAgent: (q: string) => void
}

function DatasetsSection({ functionId, functionName, onAskAgent }: SectionProps) {
  const setEntity = useChatStore((s) => s.setEntity)
  const setOpen = useChatStore((s) => s.setOpen)
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [sources, setSources] = useState<DataSource[]>([])
  const [loading, setLoading] = useState(true)
  const [previewFor, setPreviewFor] = useState<Dataset | null>(null)
  const [bindOpen, setBindOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)

  const runQualityCheck = (d: Dataset) => {
    setEntity('dataset', d.id)
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `Run a data quality audit on "${d.name}".`
    }))
  }

  const explainDataset = (d: Dataset) => {
    setEntity('dataset', d.id)
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `Tell me about the "${d.name}" dataset — what's in it, how analysts typically use it.`
    }))
  }

  const load = () => {
    setLoading(true)
    Promise.all([
      api.get<Dataset[]>(`/api/datasets`, { params: { function_id: functionId } }),
      api.get<DataSource[]>(`/api/datasources`),
    ])
      .then(([d, s]) => { setDatasets(d.data); setSources(s.data) })
      .finally(() => setLoading(false))
  }

  useEffect(load, [functionId])

  const remove = async (id: string) => {
    if (!confirm('Delete this dataset?')) return
    await api.delete(`/api/datasets/${id}`)
    if (previewFor?.id === id) setPreviewFor(null)
    load()
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <div>
          <div
            className="font-display text-base font-semibold"
            style={{ color: 'var(--text-primary)' }}
          >
            Bound Datasets
          </div>
          <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {loading
              ? 'Loading…'
              : `${datasets.length} dataset${datasets.length === 1 ? '' : 's'} available to ${functionName}'s models, analytics, and reports.`}
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => onAskAgent(`What datasets do I have for ${functionName}? Suggest analyses I could run.`)}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
            }}
          >
            <Sparkles size={13} /> Ask Agent
          </button>
          <button
            onClick={() => setUploadOpen(true)}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
            }}
          >
            <Upload size={13} /> Upload File
          </button>
          <button
            onClick={() => setBindOpen(true)}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
            style={{ background: 'var(--accent)', color: '#fff' }}
          >
            <Plus size={13} /> Bind Table
          </button>
        </div>
      </div>

      {datasets.length === 0 && !loading && (
        <div
          className="panel text-center"
          style={{ padding: '40px 20px', borderStyle: 'dashed' }}
        >
          <Database size={28} style={{ color: 'var(--text-muted)', margin: '0 auto 12px' }} />
          <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
            No datasets yet
          </div>
          <div className="text-xs mt-1 mb-4" style={{ color: 'var(--text-muted)' }}>
            Bind a table from a connected source, or upload a CSV / Parquet / XLSX / JSON file.
          </div>
          <div className="flex gap-2 justify-center">
            <button
              onClick={() => setBindOpen(true)}
              className="px-3 py-2 rounded-lg text-xs font-semibold"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              Bind Table
            </button>
            <button
              onClick={() => setUploadOpen(true)}
              className="px-3 py-2 rounded-lg text-xs font-semibold"
              style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}
            >
              Upload File
            </button>
          </div>
        </div>
      )}

      <DatasetRoleSection
        title="Input data"
        sublabel="Datasets the workflow reads from — bound tables, uploads, and pack-attached sources."
        items={datasets.filter((d) => (d.dataset_role || 'input') === 'input')}
        sources={sources}
        onPreview={(d) => setPreviewFor(d)}
        onDelete={remove}
        onExplain={explainDataset}
        onQualityCheck={runQualityCheck}
      />

      <DatasetRoleSection
        title="Output data"
        sublabel="Tables the workflow writes results to — destinations, post-model snapshots."
        items={datasets.filter((d) => d.dataset_role === 'output')}
        sources={sources}
        onPreview={(d) => setPreviewFor(d)}
        onDelete={remove}
        onExplain={explainDataset}
        onQualityCheck={runQualityCheck}
      />

      {bindOpen && (
        <BindTableModal
          functionId={functionId}
          sources={sources}
          onClose={() => setBindOpen(false)}
          onCreated={() => { setBindOpen(false); load() }}
        />
      )}
      {uploadOpen && (
        <UploadFileModal
          functionId={functionId}
          onClose={() => setUploadOpen(false)}
          onCreated={() => { setUploadOpen(false); load() }}
        />
      )}
      {previewFor && (
        <PreviewPanel
          dataset={previewFor}
          onClose={() => setPreviewFor(null)}
        />
      )}
    </div>
  )
}

// ── Role section (Input data / Output data) ───────────────────────────────
function DatasetRoleSection({
  title, sublabel, items, sources,
  onPreview, onDelete, onExplain, onQualityCheck,
}: {
  title: string
  sublabel: string
  items: Dataset[]
  sources: DataSource[]
  onPreview: (d: Dataset) => void
  onDelete: (id: string) => void
  onExplain: (d: Dataset) => void
  onQualityCheck: (d: Dataset) => void
}) {
  return (
    <section className="mb-6">
      <div className="flex items-baseline gap-2 mb-2">
        <h2
          className="text-[12px] font-bold uppercase tracking-widest"
          style={{ color: 'var(--text-primary)' }}
        >
          {title}
        </h2>
        <span className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
          {items.length}
        </span>
        <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
          · {sublabel}
        </span>
      </div>
      {items.length === 0 ? (
        <div
          className="rounded-md text-center text-xs px-3 py-4"
          style={{
            background: 'var(--bg-elevated)',
            border: '1px dashed var(--border)',
            color: 'var(--text-muted)',
          }}
        >
          None yet.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {items.map((d) => (
            <DatasetCard
              key={d.id}
              dataset={d}
              sourceName={sources.find((s) => s.id === d.data_source_id)?.name}
              onPreview={() => onPreview(d)}
              onDelete={() => onDelete(d.id)}
              onExplain={() => onExplain(d)}
              onQualityCheck={() => onQualityCheck(d)}
            />
          ))}
        </div>
      )}
    </section>
  )
}

// ── Dataset card ───────────────────────────────────────────────────────────
function DatasetCard({
  dataset, sourceName, onPreview, onDelete, onExplain, onQualityCheck,
}: {
  dataset: Dataset
  sourceName?: string
  onPreview: () => void
  onDelete: () => void
  onExplain: () => void
  onQualityCheck: () => void
}) {
  const isUpload = dataset.source_kind === 'upload'
  const meta = isUpload ? SOURCE_TYPE_META.file_upload : SOURCE_TYPE_META.snowflake
  const Icon = meta.icon

  return (
    <div className="panel" style={{ padding: 14 }}>
      <div className="flex items-start gap-3 mb-2">
        <div
          className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
          style={{ background: `${meta.color}1A`, color: meta.color }}
        >
          <Icon size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-sm truncate" style={{ color: 'var(--text-primary)' }}>
            {dataset.name}
          </div>
          <div
            className="text-[11px] truncate font-mono"
            style={{ color: 'var(--text-muted)' }}
            title={isUpload ? dataset.file_path || '' : dataset.table_ref || ''}
          >
            {isUpload
              ? `${FORMAT_LABEL[dataset.file_format || ''] || dataset.file_format} · ${formatBytes(dataset.size_bytes)}`
              : `${sourceName || dataset.data_source_id} · ${dataset.table_ref}`}
          </div>
        </div>
      </div>
      {dataset.description && (
        <div className="text-xs mb-2" style={{ color: 'var(--text-secondary)' }}>
          {dataset.description}
        </div>
      )}

      <div className="flex flex-wrap gap-1 mb-2">
        <span className="pill" style={{ fontSize: 10 }}>
          {dataset.columns.length} cols
        </span>
        {dataset.row_count != null && (
          <span className="pill" style={{ fontSize: 10 }}>
            {dataset.row_count.toLocaleString()} rows
          </span>
        )}
        <span
          className="pill"
          style={{
            fontSize: 10,
            background: isUpload ? 'rgba(124,58,237,0.10)' : 'rgba(41,181,232,0.10)',
            color: isUpload ? '#7C3AED' : '#0078D4',
            borderColor: 'transparent',
          }}
        >
          {isUpload ? 'UPLOAD' : 'SQL TABLE'}
        </span>
      </div>

      <div
        className="flex items-center justify-between mt-2 pt-2"
        style={{ borderTop: '1px solid var(--border-subtle)' }}
      >
        <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
          {dataset.last_synced ? `Synced ${new Date(dataset.last_synced).toLocaleString()}` : ''}
        </div>
        <div className="flex gap-1">
          <button
            onClick={onQualityCheck}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: 'var(--text-muted)' }}
            title="Run data quality check"
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = '#059669')}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--text-muted)')}
          >
            <ShieldCheck size={13} />
          </button>
          <button
            onClick={onExplain}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: 'var(--text-muted)' }}
            title="Ask agent"
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--accent)')}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--text-muted)')}
          >
            <Sparkles size={13} />
          </button>
          <button
            onClick={onPreview}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: 'var(--text-muted)' }}
            title="Preview"
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--accent)')}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--text-muted)')}
          >
            <Eye size={13} />
          </button>
          <button
            onClick={onDelete}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: 'var(--text-muted)' }}
            title="Delete"
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--error)')}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--text-muted)')}
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Bind-from-table modal ──────────────────────────────────────────────────
function BindTableModal({
  functionId, sources, onClose, onCreated,
}: {
  functionId: string
  sources: DataSource[]
  onClose: () => void
  onCreated: () => void
}) {
  // Three source types are bindable from this modal: OneLake + Snowflake
  // (SQL-driven) and S3 (path-driven). Postgres / REST / file_upload
  // sources are filtered out — those have other registration paths.
  const ALLOWED_TYPES = new Set<DataSource['type']>(['onelake', 'snowflake', 's3'])
  const connected = sources.filter(
    (s) => s.status === 'connected' && ALLOWED_TYPES.has(s.type),
  )
  const [sourceId, setSourceId] = useState<string>(connected[0]?.id || '')
  const selectedSource = sources.find((s) => s.id === sourceId)
  const isS3 = selectedSource?.type === 's3'

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [datasetRole, setDatasetRole] = useState<'input' | 'output'>('input')
  const [sqlQuery, setSqlQuery] = useState('')
  const [s3Path, setS3Path] = useState('')
  const [agentPrompt, setAgentPrompt] = useState('')
  const [drafting, setDrafting] = useState(false)
  const [draftNote, setDraftNote] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Auto-fill the dataset name from the S3 path's basename so the user
  // doesn't have to retype it.
  useEffect(() => {
    if (isS3 && s3Path && !name) {
      const trimmed = s3Path.replace(/\/+$/, '')
      const tail = trimmed.split('/').pop() || trimmed
      if (tail) setName(tail)
    }
  }, [isS3, s3Path]) // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    if (!sourceId || !name) return
    if (isS3 && !s3Path.trim()) {
      setError('S3 sources require a bucket / key path.')
      return
    }
    if (!isS3 && !sqlQuery.trim()) {
      setError(`${selectedSource?.type === 'onelake' ? 'OneLake' : 'Snowflake'} sources require a SQL query.`)
      return
    }
    setSaving(true)
    setError(null)
    try {
      await api.post('/api/datasets/from-table', {
        function_id: functionId,
        name,
        description,
        data_source_id: sourceId,
        table_ref: isS3 ? s3Path.trim() : undefined,
        dataset_role: datasetRole,
        sql_query: isS3 ? undefined : sqlQuery.trim(),
      })
      onCreated()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to bind table')
    } finally {
      setSaving(false)
    }
  }

  const draftSql = async () => {
    if (!agentPrompt.trim()) return
    setDrafting(true)
    setDraftNote(null)
    try {
      const r = await api.post<{ sql_query: string; note?: string }>(
        '/api/datasets/draft-sql',
        {
          // No specific table_ref any more — the agent infers / uses
          // whatever the description names. The user edits before binding.
          table_ref: '',
          description: agentPrompt.trim(),
          columns: [],
        },
      )
      setSqlQuery(r.data.sql_query)
      setDraftNote(r.data.note || null)
    } catch (e: any) {
      setDraftNote(e?.response?.data?.detail || 'Could not draft SQL')
    } finally {
      setDrafting(false)
    }
  }

  return (
    <Modal title="Bind a Table as Dataset" onClose={onClose}>
      <Field label="Type">
        <RoleRadio value={datasetRole} onChange={setDatasetRole} />
      </Field>

      <Field label="Data Source" hint="OneLake or Snowflake → query with SQL. S3 → reference by URL path.">
        <select
          value={sourceId}
          onChange={(e) => setSourceId(e.target.value)}
          className="input"
        >
          {connected.length === 0 && (
            <option value="">No connected OneLake / Snowflake / S3 sources</option>
          )}
          {connected.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}  ({s.type})
            </option>
          ))}
        </select>
      </Field>

      {isS3 ? (
        // ── S3 path-driven binding ─────────────────────────────────────
        <Field
          label="S3 URL path"
          hint={`Full s3:// URL the workflow will read at run time. Wildcards / prefixes are fine.`}
        >
          <input
            value={s3Path}
            onChange={(e) => setS3Path(e.target.value)}
            className="input font-mono"
            placeholder="s3://cma-outputs/runs/2026-04/"
          />
        </Field>
      ) : (
        // ── OneLake / Snowflake SQL-driven binding ─────────────────────
        <>
          <Field
            label="SQL query"
            hint="The SELECT statement that defines this dataset. Runs against the connected data source."
          >
            <textarea
              rows={5}
              className="input font-mono"
              style={{ fontSize: 12, lineHeight: 1.55 }}
              value={sqlQuery}
              onChange={(e) => setSqlQuery(e.target.value)}
              placeholder={
                selectedSource?.type === 'onelake'
                  ? "SELECT * FROM Finance.cma.macro_scenario WHERE …"
                  : "SELECT * FROM CMA.PUBLIC.MACRO_SCENARIO WHERE …"
              }
            />
          </Field>

          {/* Agent box — describe what you want, get a SQL draft. */}
          <div
            className="rounded-md p-3"
            style={{
              background: 'var(--accent-light)',
              border: '1px solid var(--accent)',
            }}
          >
            <div className="flex items-baseline justify-between mb-1.5 gap-2 flex-wrap">
              <div
                className="text-[10px] font-bold uppercase tracking-widest flex items-center gap-1"
                style={{ color: 'var(--accent)' }}
              >
                <Sparkles size={11} /> Ask the agent
              </div>
              {draftNote && (
                <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                  {draftNote}
                </span>
              )}
            </div>
            <textarea
              rows={2}
              className="input"
              style={{ fontSize: 12, lineHeight: 1.45 }}
              value={agentPrompt}
              onChange={(e) => setAgentPrompt(e.target.value)}
              placeholder="e.g. read macro_scenario where as_of_date is in 2026 Q2 and severity = 'adverse', limit 100"
              disabled={drafting}
            />
            <div className="flex items-center justify-between gap-2 mt-2">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Drafts a SQL query from your description. You can edit before binding.
              </span>
              <button
                onClick={draftSql}
                disabled={!agentPrompt.trim() || drafting}
                className="px-3 py-1.5 rounded-md text-xs font-semibold flex items-center gap-1.5 disabled:opacity-50"
                style={{ background: 'var(--accent)', color: '#fff' }}
              >
                {drafting ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                {drafting ? 'Drafting…' : 'Generate SQL'}
              </button>
            </div>
          </div>
        </>
      )}

      <div className="grid grid-cols-2 gap-3">
        <Field label="Dataset name">
          <input value={name} onChange={(e) => setName(e.target.value)} className="input" />
        </Field>
        <Field label="Description (optional)">
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="input"
          />
        </Field>
      </div>

      {error && (
        <div className="text-xs px-3 py-2 rounded-md" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
          {error}
        </div>
      )}

      <ModalFooter
        onClose={onClose}
        onSubmit={submit}
        disabled={!sourceId || !name || saving || (isS3 ? !s3Path.trim() : !sqlQuery.trim())}
        submitLabel={saving ? 'Binding…' : 'Bind Dataset'}
      />
    </Modal>
  )
}

// ── Upload-file modal ──────────────────────────────────────────────────────
function UploadFileModal({
  functionId, onClose, onCreated,
}: {
  functionId: string
  onClose: () => void
  onCreated: () => void
}) {
  const [files, setFiles] = useState<File[]>([])
  const [names, setNames] = useState<Record<number, string>>({})
  const [description, setDescription] = useState('')
  const [datasetRole, setDatasetRole] = useState<'input' | 'output'>('input')
  const [saving, setSaving] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [errors, setErrors] = useState<{ file: string; message: string }[]>([])
  const inputRef = useRef<HTMLInputElement>(null)

  const addFiles = (incoming: FileList | null) => {
    if (!incoming || incoming.length === 0) return
    const next = Array.from(incoming)
    setFiles((prev) => {
      // de-dupe by name+size
      const seen = new Set(prev.map((f) => `${f.name}-${f.size}`))
      const filtered = next.filter((f) => !seen.has(`${f.name}-${f.size}`))
      return [...prev, ...filtered]
    })
  }

  const removeAt = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
    setNames((prev) => {
      const out: Record<number, string> = {}
      Object.entries(prev).forEach(([k, v]) => {
        const ki = Number(k)
        if (ki < idx) out[ki] = v
        else if (ki > idx) out[ki - 1] = v
      })
      return out
    })
  }

  const setNameAt = (idx: number, value: string) => {
    setNames((prev) => ({ ...prev, [idx]: value }))
  }

  const totalBytes = files.reduce((s, f) => s + f.size, 0)

  const submit = async () => {
    if (files.length === 0) return
    setSaving(true)
    setErrors([])
    setProgress({ done: 0, total: files.length })
    const failures: { file: string; message: string }[] = []
    for (let i = 0; i < files.length; i++) {
      const file = files[i]
      const fd = new FormData()
      fd.append('function_id', functionId)
      fd.append('file', file)
      const stem = file.name.split('.').slice(0, -1).join('.') || file.name
      const finalName = (names[i] && names[i].trim()) || stem
      fd.append('name', finalName)
      if (description) fd.append('description', description)
      fd.append('dataset_role', datasetRole)
      try {
        await api.post('/api/datasets/upload', fd, {
          headers: { 'Content-Type': 'multipart/form-data' },
        })
      } catch (e: any) {
        failures.push({
          file: file.name,
          message: e?.response?.data?.detail || 'Upload failed',
        })
      }
      setProgress({ done: i + 1, total: files.length })
    }
    setErrors(failures)
    setSaving(false)
    if (failures.length === 0) onCreated()
  }

  return (
    <Modal title="Upload Dataset Files" onClose={onClose}>
      <Field label="Type">
        <RoleRadio value={datasetRole} onChange={setDatasetRole} />
      </Field>
      <Field label="Files (CSV / Parquet / XLSX / JSON, max 50 MB each — multi-select supported)">
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".csv,.parquet,.xlsx,.xls,.json"
          onChange={(e) => {
            addFiles(e.target.files)
            if (inputRef.current) inputRef.current.value = ''
          }}
          className="hidden"
        />
        <button
          onClick={() => inputRef.current?.click()}
          className="w-full rounded-lg py-5 text-sm transition-colors"
          style={{
            background: 'var(--bg-elevated)',
            border: `1.5px dashed ${files.length > 0 ? 'var(--accent)' : 'var(--border)'}`,
            color: files.length > 0 ? 'var(--accent)' : 'var(--text-muted)',
          }}
        >
          {files.length > 0 ? (
            <div>
              <div className="font-semibold">
                {files.length} file{files.length === 1 ? '' : 's'} selected · {formatBytes(totalBytes)} total
              </div>
              <div className="text-[11px] mt-0.5" style={{ color: 'var(--text-muted)' }}>
                Click to add more
              </div>
            </div>
          ) : (
            <>
              <Upload size={20} style={{ display: 'inline-block', marginRight: 6 }} />
              Click to choose files (you can pick multiple)
            </>
          )}
        </button>
      </Field>

      {files.length > 0 && (
        <div
          className="rounded-lg overflow-hidden"
          style={{ border: '1px solid var(--border)' }}
        >
          {files.map((f, i) => {
            const stem = f.name.split('.').slice(0, -1).join('.') || f.name
            return (
              <div
                key={`${f.name}-${i}`}
                className="flex items-center gap-2 px-3 py-2"
                style={{
                  background: i % 2 === 0 ? 'var(--bg-card)' : 'var(--bg-elevated)',
                  borderBottom: i < files.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                }}
              >
                <FileText size={13} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
                <div
                  className="text-[12px] font-mono truncate min-w-0 flex-1"
                  style={{ color: 'var(--text-secondary)' }}
                  title={f.name}
                >
                  {f.name}
                </div>
                <input
                  className="input"
                  style={{ width: 200, padding: '4px 8px', fontSize: 12 }}
                  placeholder={stem}
                  value={names[i] ?? ''}
                  onChange={(e) => setNameAt(i, e.target.value)}
                  title="Override the dataset name (defaults to filename stem)"
                  disabled={saving}
                />
                <span
                  className="text-[10px] font-mono shrink-0"
                  style={{ color: 'var(--text-muted)', width: 70, textAlign: 'right' }}
                >
                  {formatBytes(f.size)}
                </span>
                <button
                  onClick={() => removeAt(i)}
                  disabled={saving}
                  className="p-1 rounded shrink-0"
                  style={{ color: 'var(--text-muted)' }}
                  title="Remove from list"
                >
                  <X size={12} />
                </button>
              </div>
            )
          })}
        </div>
      )}

      <Field label="Description (optional — applied to all uploaded datasets)">
        <input
          className="input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={saving}
        />
      </Field>

      {progress && (
        <div
          className="rounded-md px-3 py-2 text-xs flex items-center gap-2"
          style={{
            background: 'var(--accent-light)',
            color: 'var(--accent)',
          }}
        >
          <span style={{ fontWeight: 600 }}>
            {saving ? 'Uploading…' : 'Done'}
          </span>
          <span className="font-mono">
            {progress.done} / {progress.total}
          </span>
          <div
            className="flex-1 h-1.5 rounded-full"
            style={{ background: 'var(--bg-elevated)' }}
          >
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${(progress.done / progress.total) * 100}%`,
                background: 'var(--accent)',
              }}
            />
          </div>
        </div>
      )}

      {errors.length > 0 && (
        <div className="text-xs px-3 py-2 rounded-md" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
          <div className="font-semibold mb-1">{errors.length} upload{errors.length === 1 ? '' : 's'} failed:</div>
          <ul className="space-y-0.5 font-mono">
            {errors.map((e, i) => (
              <li key={i}>· <strong>{e.file}</strong> — {e.message}</li>
            ))}
          </ul>
        </div>
      )}

      <ModalFooter
        onClose={onClose}
        onSubmit={submit}
        disabled={files.length === 0 || saving}
        submitLabel={
          saving
            ? `Uploading ${progress?.done ?? 0}/${progress?.total ?? files.length}…`
            : files.length === 0
              ? 'Upload & Bind'
              : `Upload & Bind ${files.length} File${files.length === 1 ? '' : 's'}`
        }
      />
    </Modal>
  )
}

// ── Preview side panel ────────────────────────────────────────────────────
function PreviewPanel({
  dataset, onClose,
}: {
  dataset: Dataset
  onClose: () => void
}) {
  const [preview, setPreview] = useState<DatasetPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Wiring the dataset entity into the chat store before dispatching the
  // event is what makes the router pick `data-explainer` instead of falling
  // through to the generic orchestrator. Without it, "Ask Agent" sent a
  // bare message and either bounced or got a generic answer.
  const setEntity = useChatStore((s) => s.setEntity)
  const setOpen = useChatStore((s) => s.setOpen)
  const setPayload = useChatStore((s) => s.setPayload)

  useEffect(() => {
    setPreview(null)
    setError(null)
    api
      .get<DatasetPreview>(`/api/datasets/${dataset.id}/preview`, { params: { n: 25 } })
      .then((r) => setPreview(r.data))
      .catch((e) => setError(e?.response?.data?.detail || 'Could not load preview'))
  }, [dataset.id])

  const askDataExplainer = () => {
    setEntity('dataset', dataset.id)
    // Pass the exact rows the analyst is looking at into [Context] so the
    // agent narrates THIS view rather than re-fetching a possibly
    // different slice via get_dataset_preview. The backend stringifies the
    // payload and trims to its cap; rows that don't fit are dropped, which
    // is fine — the agent still gets the schema + as many rows as fit.
    if (preview) {
      setPayload({
        dataset_name: dataset.name,
        source_kind: dataset.source_kind,
        total_rows: preview.total_rows ?? null,
        columns: preview.columns,
        sample_rows: preview.sample_rows,
        sample_rows_shown: preview.sample_rows.length,
      })
    } else {
      setPayload(null)
    }
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `Explain this dataset ("${dataset.name}"). Use the columns and sample_rows in [Context]'s payload (that's exactly what I'm looking at). Walk me through what's in it, what each column means, and call out any data-quality concerns you can see from those rows.`,
    }))
  }

  return (
    <>
      <div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11,15,25,0.45)' }}
        onClick={onClose}
      />
      <div
        className="fixed top-0 right-0 bottom-0 z-50 flex flex-col"
        style={{
          width: 'min(880px, 96vw)',
          background: 'var(--bg-card)',
          borderLeft: '1px solid var(--border)',
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
          <div>
            <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>
              {dataset.name}
            </div>
            <div className="font-mono" style={{ fontSize: 11, color: 'rgba(255,255,255,0.75)' }}>
              {dataset.source_kind === 'upload'
                ? `Upload · ${(dataset.file_format || '').toUpperCase()} · ${formatBytes(dataset.size_bytes)}`
                : `${dataset.data_source_id} · ${dataset.table_ref}`}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg"
            style={{ color: 'rgba(255,255,255,0.85)' }}
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {error && (
            <div
              className="text-xs px-3 py-2 rounded-md"
              style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
            >
              {error}
            </div>
          )}
          {!preview && !error && (
            <div
              className="flex items-center gap-2 text-xs"
              style={{ color: 'var(--text-muted)' }}
            >
              <Loader2 size={12} className="animate-spin" /> Loading preview…
            </div>
          )}
          {preview && (
            <>
              <div>
                <div className="section-title">Schema ({preview.columns.length} columns)</div>
                <div
                  className="rounded-lg p-2 font-mono grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-0.5"
                  style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', fontSize: 11 }}
                >
                  {preview.columns.map((c) => (
                    <div key={c.name} className="flex justify-between">
                      <span style={{ color: 'var(--text-primary)' }}>{c.name}</span>
                      <span style={{ color: 'var(--text-muted)' }}>{c.dtype}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="section-title" style={{ marginBottom: 0 }}>
                    Sample Rows ({preview.sample_rows.length}
                    {preview.total_rows != null ? ` of ${preview.total_rows.toLocaleString()}` : ''})
                  </div>
                  <button
                    onClick={askDataExplainer}
                    className="text-[11px] flex items-center gap-1"
                    style={{ color: 'var(--accent)', fontWeight: 600 }}
                    title="Explain this dataset — columns, contents, and quality concerns"
                  >
                    <Sparkles size={11} /> Ask Agent
                  </button>
                </div>
                <div
                  className="overflow-auto rounded-lg"
                  style={{ border: '1px solid var(--border)', maxHeight: 480 }}
                >
                  <table className="w-full text-xs font-mono">
                    <thead style={{ background: 'var(--bg-elevated)', position: 'sticky', top: 0 }}>
                      <tr>
                        {preview.columns.map((c) => (
                          <th
                            key={c.name}
                            className="text-left py-2 px-3 whitespace-nowrap"
                            style={{
                              fontSize: 10, fontWeight: 700, letterSpacing: '0.05em',
                              textTransform: 'uppercase', color: 'var(--text-secondary)',
                              borderBottom: '1px solid var(--border)',
                            }}
                          >
                            {c.name}
                            <div
                              style={{ fontSize: 9, fontWeight: 400, textTransform: 'none', color: 'var(--text-muted)' }}
                            >
                              {c.dtype}
                            </div>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.sample_rows.map((row, i) => (
                        <tr key={i}>
                          {preview.columns.map((c) => (
                            <td
                              key={c.name}
                              className="py-1.5 px-3 whitespace-nowrap"
                              style={{ borderBottom: '1px solid var(--border-subtle)' }}
                            >
                              {formatCell(row[c.name])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </>
  )
}

// ── Shared modal chrome ───────────────────────────────────────────────────
function Modal({
  title, onClose, children,
}: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: 'rgba(11,15,25,0.45)' }} onClick={onClose} />
      <div
        className="fixed top-1/2 left-1/2 z-50 flex flex-col"
        style={{
          width: 'min(640px, 96vw)',
          maxHeight: '90vh',
          transform: 'translate(-50%, -50%)',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: 12,
          boxShadow: '0 24px 64px rgba(0,0,0,0.20)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-3.5"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--teal))',
            borderTopLeftRadius: 11,
            borderTopRightRadius: 11,
          }}
        >
          <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>
            {title}
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'rgba(255,255,255,0.85)' }}>
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">{children}</div>
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

function ModalFooter({
  onClose, onSubmit, disabled, submitLabel,
}: {
  onClose: () => void
  onSubmit: () => void
  disabled?: boolean
  submitLabel: string
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <button onClick={onClose} className="px-3 py-2 text-xs rounded-lg" style={{ color: 'var(--text-muted)' }}>
        Cancel
      </button>
      <button
        onClick={onSubmit}
        disabled={disabled}
        className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
        style={{ background: 'var(--accent)', color: '#fff' }}
      >
        {submitLabel}
      </button>
    </div>
  )
}

function RoleRadio({
  value, onChange,
}: { value: 'input' | 'output'; onChange: (v: 'input' | 'output') => void }) {
  // Two-button segmented control. Used by both Upload File + Bind Table
  // modals so the analyst declares whether they're registering an input
  // (read by the workflow) or an output (written by the workflow).
  const opt = (
    val: 'input' | 'output',
    label: string,
    sublabel: string,
    color: string,
  ) => {
    const active = value === val
    return (
      <button
        type="button"
        key={val}
        onClick={() => onChange(val)}
        className="flex-1 rounded-md text-left px-3 py-2 transition-colors"
        style={{
          background: active ? `${color}1A` : 'var(--bg-elevated)',
          border: `1px solid ${active ? color : 'var(--border)'}`,
          color: active ? color : 'var(--text-primary)',
        }}
      >
        <div className="text-[12px] font-semibold">{label}</div>
        <div
          className="text-[10px] mt-0.5"
          style={{ color: active ? color : 'var(--text-muted)' }}
        >
          {sublabel}
        </div>
      </button>
    )
  }
  return (
    <div className="flex gap-2">
      {opt('input',  'Input data',  'Workflow reads from this',          '#0078D4')}
      {opt('output', 'Output data', 'Workflow writes results here', '#059669')}
    </div>
  )
}

function Field({
  label, hint, children,
}: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span
        className="block text-[11px] font-semibold uppercase tracking-widest mb-1"
        style={{ color: 'var(--text-secondary)' }}
      >
        {label}
      </span>
      {children}
      {hint && (
        <span
          className="block text-[10px] mt-1"
          style={{ color: 'var(--text-muted)', lineHeight: 1.45 }}
        >
          {hint}
        </span>
      )}
    </label>
  )
}

// ── tiny formatters ───────────────────────────────────────────────────────
function formatBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(1)} GB`
}

function formatCell(v: any): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') {
    return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4)
  }
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
