import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Upload, Trash2, FileText, FileSpreadsheet, FileCode2, FileImage,
  FileBox, BookOpen, Search, Filter, Sparkles, Eye, X, Wand2, Loader2,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import api from '@/lib/api'
import { useChatStore } from '@/store/chatStore'

type DocumentInfo = {
  id: string
  name: string
  size_bytes: number
  extension: string
  scope: string | null
  uploaded_at: string
}

type DocsRoot = {
  root: string
  exists: boolean
  extensions: string[]
  env_var: string
  env_value: string | null
}

const EXT_META: Record<string, { color: string; icon: any }> = {
  '.md':    { color: '#0EA5E9', icon: FileText },
  '.txt':   { color: '#64748B', icon: FileText },
  '.pdf':   { color: '#DC2626', icon: FileBox },
  '.docx':  { color: '#2563EB', icon: FileText },
  '.pptx':  { color: '#EA580C', icon: FileImage },
  '.py':    { color: '#7C3AED', icon: FileCode2 },
  '.csv':   { color: '#059669', icon: FileSpreadsheet },
  '.xlsx':  { color: '#16A34A', icon: FileSpreadsheet },
  '.xls':   { color: '#16A34A', icon: FileSpreadsheet },
  '.json':  { color: '#D97706', icon: FileCode2 },
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatDate(iso: string): string {
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

export default function KnowledgeBaseTab() {
  const [docs, setDocs] = useState<DocumentInfo[]>([])
  const [root, setRoot] = useState<DocsRoot | null>(null)
  const [loading, setLoading] = useState(true)
  const [scopeFilter, setScopeFilter] = useState<string>('__all__')
  const [searchQuery, setSearchQuery] = useState('')
  const [uploadScope, setUploadScope] = useState('')
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const [previewDoc, setPreviewDoc] = useState<DocumentInfo | null>(null)
  const [extractOpen, setExtractOpen] = useState(false)
  const setOpen = useChatStore((s) => s.setOpen)
  const setEntity = useChatStore((s) => s.setEntity)
  const setPageContext = useChatStore((s) => s.setPageContext)

  const load = async () => {
    setLoading(true)
    try {
      const [d, r] = await Promise.all([
        api.get<DocumentInfo[]>('/api/documents'),
        api.get<DocsRoot>('/api/documents/root'),
      ])
      setDocs(d.data)
      setRoot(r.data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // Hide playbook-uploaded files from the KB view. They live under
  // `playbook/<id>/` because the playbook editor uploads attachments
  // into the same docs root, but they're scoped artefacts the analyst
  // shouldn't have to delete from here. The agent's `rag_search` still
  // sees them when scoped to the right playbook.
  const isPlaybookScoped = (d: DocumentInfo) =>
    !!d.scope && d.scope.toLowerCase().startsWith('playbook')
  const visibleDocs = useMemo(() => docs.filter((d) => !isPlaybookScoped(d)), [docs])

  const scopes = useMemo(() => {
    const s = new Set<string>()
    for (const d of visibleDocs) if (d.scope) s.add(d.scope)
    return Array.from(s).sort()
  }, [visibleDocs])

  const filtered = useMemo(() => {
    return visibleDocs.filter((d) => {
      if (scopeFilter === '__all__') {
        // pass
      } else if (scopeFilter === '__root__') {
        if (d.scope) return false
      } else if (d.scope !== scopeFilter) {
        return false
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase()
        if (!d.name.toLowerCase().includes(q) && !(d.scope || '').toLowerCase().includes(q)) {
          return false
        }
      }
      return true
    })
  }, [visibleDocs, scopeFilter, searchQuery])

  const totalSize = useMemo(() => visibleDocs.reduce((sum, d) => sum + d.size_bytes, 0), [visibleDocs])

  // Click "Explain" on a card → bind the chat panel to the doc with
  // entity_kind='document' and dispatch a doc-explainer prompt.
  // Important: we do NOT bind entity_kind='dataset' here — the
  // orchestrator routes 'dataset' to the data-quality / dataset
  // explainer skill, which can't actually read whitepaper markdown.
  // The 'document' kind tells the orchestrator to route to
  // methodology-researcher (which carries `rag_search` in its
  // toolkit and can read .md / .pdf / .docx whitepapers).
  const onExplain = (d: DocumentInfo) => {
    setEntity('document', d.id)
    setPageContext(`Explain knowledge-base document "${d.name}".`)
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail:
        `I'm looking at the knowledge-base document **${d.name}** ` +
        `(file id: \`${d.id}\`${d.scope ? `, scope: \`${d.scope}\`` : ''}). ` +
        `Use \`rag_search\` to find and read this file from the docs root, ` +
        `then explain in 4-6 bullets what the document covers — what the ` +
        `model / methodology projects, key formulas, calibrated parameters, ` +
        `stress overlay (if any), and caveats. ` +
        `Quote the file's actual content; don't paraphrase from training data. ` +
        `Do NOT route this as a dataset / data-quality question — this is a ` +
        `document explanation request.`,
    }))
  }

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length === 0) return
    setUploading(true)
    try {
      for (const f of files) {
        const fd = new FormData()
        fd.append('file', f)
        if (uploadScope.trim()) fd.append('scope', uploadScope.trim())
        try {
          await api.post('/api/documents/upload', fd, {
            headers: { 'Content-Type': 'multipart/form-data' },
          })
        } catch (err: any) {
          alert(`Upload failed for ${f.name}: ${err?.response?.data?.detail || err.message}`)
        }
      }
      if (fileRef.current) fileRef.current.value = ''
      await load()
    } finally {
      setUploading(false)
    }
  }

  const onDelete = async (id: string, name: string) => {
    if (!confirm(`Delete ${name}?`)) return
    await api.delete(`/api/documents/${encodeURIComponent(id)}`)
    load()
  }

  return (
    <div>
      {/* ─ Header strip ─────────────────────────────────────────────────── */}
      <div
        className="rounded-xl p-4 mb-5 flex flex-wrap items-center gap-4"
        style={{
          background: 'linear-gradient(135deg, rgba(14,165,233,0.06), rgba(124,58,237,0.04))',
          border: '1px solid var(--border)',
        }}
      >
        <div
          className="w-11 h-11 rounded-lg flex items-center justify-center shrink-0"
          style={{ background: 'rgba(14,165,233,0.12)', color: '#0EA5E9' }}
        >
          <BookOpen size={20} />
        </div>
        <div className="flex-1 min-w-[260px]">
          <div className="font-display text-base font-semibold mb-0.5">Knowledge Base</div>
          <div className="text-[12px] leading-snug" style={{ color: 'var(--text-secondary)' }}>
            Upload model whitepapers, reports, decks, and code that the{' '}
            <span className="font-mono text-[11px]" style={{ color: 'var(--accent)' }}>rag_search</span>{' '}
            tool can query for domain-specific context. Group related files by scope (e.g.{' '}
            <span className="font-mono text-[11px]">retail_deposit</span>) so agents can pre-filter their searches.
          </div>
        </div>
        <div className="flex flex-col items-end gap-0.5 text-[11px]" style={{ color: 'var(--text-muted)' }}>
          <div><strong style={{ color: 'var(--text-primary)' }}>{docs.length}</strong> file{docs.length === 1 ? '' : 's'}</div>
          <div>{formatBytes(totalSize)} stored</div>
        </div>
      </div>

      {/* ─ Action bar ──────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 min-w-[220px] max-w-[420px]">
          <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-muted)' }} />
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search filenames or scopes…"
            className="w-full pl-9 pr-3 py-2 rounded-lg text-[13px]"
            style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
          />
        </div>
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-[12px]"
             style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
          <Filter size={12} style={{ color: 'var(--text-muted)' }} />
          <select
            value={scopeFilter}
            onChange={(e) => setScopeFilter(e.target.value)}
            className="bg-transparent outline-none"
            style={{ color: 'var(--text-primary)' }}
          >
            <option value="__all__">All scopes</option>
            <option value="__root__">No scope (root)</option>
            {scopes.map((s) => (<option key={s} value={s}>{s}</option>))}
          </select>
        </div>

        <input
          value={uploadScope}
          onChange={(e) => setUploadScope(e.target.value)}
          placeholder="Upload scope (optional, e.g. retail_deposit)"
          className="px-3 py-2 rounded-lg text-[12px] flex-1 min-w-[200px] max-w-[300px]"
          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
        />
        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          accept={root?.extensions.join(',') || '.md,.txt,.pdf,.docx,.pptx,.py,.csv,.xlsx,.xls,.json'}
          onChange={onUpload}
        />
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          className="px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 disabled:opacity-50"
          style={{ background: 'var(--accent)', color: '#fff' }}
        >
          <Upload size={13} /> {uploading ? 'Uploading…' : 'Upload Files'}
        </button>
        <button
          onClick={() => setExtractOpen(true)}
          className="px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
          style={{
            background: 'linear-gradient(135deg, rgba(124,58,237,0.12), rgba(14,165,233,0.10))',
            border: '1px solid rgba(124,58,237,0.35)',
            color: '#7C3AED',
          }}
          title="Upload a PDF or Word file — agent extracts a structured methodology .md"
        >
          <Wand2 size={13} /> Extract whitepaper
        </button>
      </div>

      {/* ─ Storage path hint ──────────────────────────────────────────── */}
      {root && (
        <div
          className="text-[11px] mb-4 px-3 py-2 rounded-md font-mono"
          style={{ background: 'var(--bg-elevated)', color: 'var(--text-muted)' }}
        >
          <span className="opacity-60">Storage:</span> {root.root}
          {!root.exists && <span style={{ color: 'var(--warning)' }}> (will be created on first upload)</span>}
          {root.env_value && <span className="opacity-60"> · via ${root.env_var}</span>}
        </div>
      )}

      {/* ─ File grid ──────────────────────────────────────────────────── */}
      {loading ? (
        <div className="text-sm text-center py-12" style={{ color: 'var(--text-muted)' }}>Loading…</div>
      ) : filtered.length === 0 ? (
        <div
          className="text-center py-16 rounded-xl"
          style={{ border: '1px dashed var(--border)', background: 'var(--bg-elevated)' }}
        >
          <BookOpen size={28} className="mx-auto mb-3" style={{ color: 'var(--text-muted)' }} />
          <div className="text-sm font-semibold mb-1">
            {docs.length === 0 ? 'No documents yet' : 'No documents match this filter'}
          </div>
          <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            {docs.length === 0
              ? 'Upload whitepapers, reports, decks, or code so agents can ground their answers.'
              : 'Try clearing the search or scope filter.'}
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {filtered.map((d) => {
            const meta = EXT_META[d.extension] || { color: '#64748B', icon: FileText }
            const Icon = meta.icon
            return (
              <div key={d.id} className="panel" style={{ display: 'flex', flexDirection: 'column' }}>
                <div className="flex items-start gap-3 mb-2">
                  <div
                    className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                    style={{ background: `${meta.color}1A`, color: meta.color }}
                  >
                    <Icon size={16} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold text-[13px] truncate" title={d.name}>{d.name}</div>
                    <div className="flex items-center gap-2 mt-0.5">
                      {d.scope && (
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded-md font-mono"
                          style={{ background: 'rgba(14,165,233,0.12)', color: '#0EA5E9' }}
                        >
                          {d.scope}
                        </span>
                      )}
                      <span
                        className="text-[10px] uppercase tracking-wider font-semibold"
                        style={{ color: 'var(--text-muted)' }}
                      >
                        {d.extension.replace('.', '')}
                      </span>
                    </div>
                  </div>
                </div>
                <div
                  className="flex items-center justify-between mt-auto pt-3"
                  style={{ borderTop: '1px solid var(--border-subtle)' }}
                >
                  <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                    {formatBytes(d.size_bytes)} · {formatDate(d.uploaded_at)}
                  </div>
                  {/* Action icons match the model-registry card style:
                      Sparkles → ask the agent to explain this doc;
                      Eye      → open a side panel showing the rendered
                                 markdown / extracted text. Both are
                                 hover-affordances rather than dominant
                                 buttons so the card stays scannable. */}
                  <div className="flex items-center gap-0.5">
                    <button
                      onClick={() => onExplain(d)}
                      className="p-1.5 rounded-md transition-colors"
                      style={{ color: 'var(--text-muted)' }}
                      title="Explain with agent"
                    >
                      <Sparkles size={13} />
                    </button>
                    <button
                      onClick={() => setPreviewDoc(d)}
                      className="p-1.5 rounded-md transition-colors"
                      style={{ color: 'var(--text-muted)' }}
                      title="Preview"
                    >
                      <Eye size={13} />
                    </button>
                    <button
                      onClick={() => onDelete(d.id, d.name)}
                      className="p-1.5 rounded-md transition-colors"
                      style={{ color: 'var(--text-muted)' }}
                      title="Delete"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* ─ Preview side panel ─────────────────────────────────────────── */}
      {previewDoc && (
        <PreviewPanel doc={previewDoc} onClose={() => setPreviewDoc(null)} />
      )}

      {/* ─ Extract-from-PDF/Word modal ────────────────────────────────── */}
      {extractOpen && (
        <ExtractModal
          onClose={() => setExtractOpen(false)}
          onExtracted={() => { setExtractOpen(false); load() }}
        />
      )}
    </div>
  )
}

// ── Preview side panel ─────────────────────────────────────────────────
function PreviewPanel({ doc, onClose }: { doc: DocumentInfo; onClose: () => void }) {
  const [text, setText] = useState<string>('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.get<{ text: string }>(`/api/documents/content/${encodeURIComponent(doc.id)}`)
      .then((r) => { if (!cancelled) setText(r.data.text || '') })
      .catch(() => { if (!cancelled) setText('(failed to load preview)') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [doc.id])

  const isMarkdown = doc.extension === '.md'
  return (
    <div
      className="fixed inset-0 z-40 flex items-stretch justify-end"
      style={{ background: 'rgba(11,15,25,0.45)' }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="z-50 flex flex-col"
        style={{
          width: 'min(720px, 92vw)',
          background: 'var(--bg-card)',
          borderLeft: '1px solid var(--border)',
          boxShadow: '-24px 0 72px rgba(0,0,0,0.32)',
        }}
      >
        <div className="flex items-center justify-between px-5 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold truncate" title={doc.name}>{doc.name}</div>
            <div className="text-[10px] font-mono mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {doc.scope ? `${doc.scope}/` : ''}{doc.name} · {formatBytes(doc.size_bytes)}
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'var(--text-muted)' }}>
            <X size={16} />
          </button>
        </div>
        <div
          className="flex-1 overflow-y-auto px-6 py-5"
          style={{ background: '#FFFFFF', fontFamily: "'Source Serif Pro', Georgia, serif", fontSize: 14, lineHeight: 1.7 }}
        >
          {loading ? (
            <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--text-muted)' }}>
              <Loader2 size={12} className="animate-spin" /> Loading…
            </div>
          ) : isMarkdown ? (
            <div className="cma-md cma-md-report">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
            </div>
          ) : (
            <pre className="text-[12px] whitespace-pre-wrap font-mono">{text}</pre>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Extract whitepaper modal ───────────────────────────────────────────
const DEFAULT_FOCUS_AREAS = `- Methodology: the model's exact formulas, calibrated parameters, and key assumptions
- Stress overlay: any behavioral changes under regulatory stress (BHC Stress / Fed SA)
- Suite linkages: upstream inputs the model consumes and downstream models / decisions it feeds
- Caveats: known limitations, data constraints, and out-of-scope behaviors

Preserve concrete numbers and formulas verbatim — don't paraphrase them away.`

function ExtractModal({ onClose, onExtracted }: { onClose: () => void; onExtracted: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [scope, setScope] = useState('whitepapers')
  const [focusAreas, setFocusAreas] = useState(DEFAULT_FOCUS_AREAS)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const addFiles = (incoming: FileList | null) => {
    if (!incoming) return
    const added = Array.from(incoming)
    setFiles((prev) => {
      const names = new Set(prev.map((f) => f.name))
      return [...prev, ...added.filter((f) => !names.has(f.name))]
    })
    if (fileRef.current) fileRef.current.value = ''
  }

  const removeFile = (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx))

  const submit = async () => {
    if (files.length === 0) { setError('Pick at least one .pdf or .docx file.'); return }
    setBusy(true); setError(null)
    const total = files.length
    let lastError: string | null = null
    for (let i = 0; i < total; i++) {
      setProgress({ current: i + 1, total })
      try {
        const fd = new FormData()
        fd.append('file', files[i])
        if (scope.trim()) fd.append('scope', scope.trim())
        if (focusAreas.trim()) fd.append('focus_areas', focusAreas.trim())
        await api.post('/api/documents/extract-whitepaper', fd, {
          headers: { 'Content-Type': 'multipart/form-data' },
          timeout: 120000,
        })
      } catch (e: any) {
        lastError = `"${files[i].name}": ${e?.response?.data?.detail || e.message || 'Extraction failed'}`
      }
    }
    setBusy(false)
    setProgress(null)
    if (lastError) {
      setError(lastError)
    } else {
      onExtracted()
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center p-6"
      style={{ background: 'rgba(11,15,25,0.45)' }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="z-50 rounded-xl flex flex-col"
        style={{
          width: 'min(560px, 100%)',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          boxShadow: '0 24px 72px rgba(0,0,0,0.32)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-3"
          style={{
            borderBottom: '1px solid var(--border)',
            background: 'linear-gradient(135deg, rgba(124,58,237,0.10), rgba(14,165,233,0.06))',
          }}
        >
          <div className="flex items-center gap-2">
            <Wand2 size={14} style={{ color: '#7C3AED' }} />
            <div className="font-display text-[14px] font-semibold">Extract whitepaper</div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'var(--text-muted)' }}>
            <X size={16} />
          </button>
        </div>
        <div className="px-5 py-4 space-y-3">
          <div className="text-[12px]" style={{ color: 'var(--text-secondary)', lineHeight: 1.55 }}>
            Upload one or more PDFs or Word docs. The agent reads each file, extracts model methodology, and saves a structured markdown whitepaper — frontmatter, methodology, stress overlay, suite linkages, caveats.
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-widest mb-1" style={{ color: 'var(--text-muted)' }}>Source files (.pdf or .docx)</div>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx"
              multiple
              onChange={(e) => addFiles(e.target.files)}
              className="w-full px-3 py-2 rounded-lg text-[12px]"
              style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
            />
            {files.length > 0 && (
              <div className="mt-2 space-y-1">
                {files.map((f, i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between px-2 py-1 rounded-md text-[11px]"
                    style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
                  >
                    <span className="truncate" style={{ color: 'var(--text-secondary)' }}>{f.name}</span>
                    <button
                      type="button"
                      onClick={() => removeFile(i)}
                      className="ml-2 shrink-0"
                      style={{ color: 'var(--text-muted)' }}
                      disabled={busy}
                    >
                      <X size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-widest mb-1" style={{ color: 'var(--text-muted)' }}>Scope (folder)</div>
            <input
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              placeholder="e.g. retail_deposit"
              className="w-full px-3 py-2 rounded-lg text-[12px]"
              style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
            />
          </div>
          <div>
            <div className="flex items-center justify-between mb-1">
              <div className="text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
                What to extract
              </div>
              <button
                type="button"
                onClick={() => setFocusAreas(DEFAULT_FOCUS_AREAS)}
                className="text-[10px]"
                style={{ color: 'var(--text-muted)' }}
                title="Reset to default focus areas"
              >
                ↺ reset to default
              </button>
            </div>
            <textarea
              value={focusAreas}
              onChange={(e) => setFocusAreas(e.target.value)}
              rows={6}
              className="w-full px-3 py-2 rounded-lg text-[12px] resize-y"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border)',
                fontFamily: 'inherit',
                lineHeight: 1.5,
              }}
              placeholder="What sections / topics should the agent extract?"
            />
            <div className="text-[10px] mt-1" style={{ color: 'var(--text-muted)' }}>
              The agent ALWAYS produces frontmatter + the standard whitepaper structure; this box adds extra focus areas (or trims the defaults). Edit freely.
            </div>
          </div>
          {error && (
            <div
              className="text-[11px] px-2 py-1.5 rounded-md"
              style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
            >
              {error}
            </div>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
          <button onClick={onClose} className="px-3 py-2 text-xs rounded-lg" style={{ color: 'var(--text-muted)' }}>
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || files.length === 0}
            className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 disabled:opacity-50"
            style={{ background: '#7C3AED', color: '#fff' }}
          >
            {busy && progress
              ? <><Loader2 size={11} className="animate-spin" /> Extracting {progress.current} of {progress.total}…</>
              : <><Wand2 size={11} /> Extract & save{files.length > 1 ? ` (${files.length})` : ''}</>}
          </button>
        </div>
      </div>
    </div>
  )
}
