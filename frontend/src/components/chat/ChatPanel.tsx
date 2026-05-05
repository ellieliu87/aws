import { useEffect, useRef, useState } from 'react'
import {
  Send, Sparkles, X, Cpu, LineChart, ShieldCheck, Boxes, GitBranch,
  AlertTriangle, SlidersHorizontal, LifeBuoy, Filter, Trash2,
  Maximize2, Minimize2, ThumbsUp, ThumbsDown,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ResponsiveContainer, ScatterChart, Scatter, XAxis, YAxis,
  CartesianGrid, Tooltip as RechartsTooltip, ReferenceLine, Cell,
} from 'recharts'
import { useChatStore } from '@/store/chatStore'
import api from '@/lib/api'
import { cn } from '@/lib/utils'
import type { ChatAction } from '@/types'

const AGENT_ICON: Record<string, any> = {
  cpu: Cpu,
  'line-chart': LineChart,
  'shield-check': ShieldCheck,
  boxes: Boxes,
  'git-branch': GitBranch,
  'alert-triangle': AlertTriangle,
  'sliders-horizontal': SlidersHorizontal,
  'life-buoy': LifeBuoy,
}

interface ChatPanelProps {
  open: boolean
  onClose: () => void
}

const mdComponents: any = {
  h1: (p: any) => (
    <h1
      style={{
        color: 'var(--text-primary)', fontSize: 14, fontWeight: 700, marginTop: 12, marginBottom: 5,
        borderBottom: '1px solid var(--border)', paddingBottom: 4,
      }}
      {...p}
    />
  ),
  h2: (p: any) => (
    <h2 style={{ color: 'var(--text-primary)', fontSize: 13, fontWeight: 600, marginTop: 10, marginBottom: 4 }} {...p} />
  ),
  h3: (p: any) => (
    <h3 style={{ color: 'var(--accent)', fontSize: 12, fontWeight: 600, marginTop: 8, marginBottom: 3 }} {...p} />
  ),
  p: (p: any) => <p style={{ margin: '3px 0', lineHeight: 1.6, fontSize: 12 }} {...p} />,
  ul: (p: any) => <ul style={{ paddingLeft: 16, margin: '3px 0', fontSize: 12 }} {...p} />,
  ol: (p: any) => <ol style={{ paddingLeft: 16, margin: '3px 0', fontSize: 12 }} {...p} />,
  li: (p: any) => <li style={{ marginBottom: 2, lineHeight: 1.5, fontSize: 12 }} {...p} />,
  strong: (p: any) => {
    const content = String(p.children || '')
    if (/(WARN|EXCEED|BREACH|ALERT)/i.test(content))
      return (
        <strong
          style={{
            color: 'var(--error)', fontWeight: 700, background: 'rgba(220,38,38,0.08)',
            padding: '1px 3px', borderRadius: 3, fontSize: 12,
          }}
          {...p}
        />
      )
    if (/(WITHIN|PASS|OK)/i.test(content))
      return <strong style={{ color: 'var(--success)', fontWeight: 700, fontSize: 12 }} {...p} />
    if (/\$|\d+(\.\d+)?[BMK]|\d+ bps/.test(content))
      return (
        <strong
          style={{
            color: 'var(--accent)', fontWeight: 700,
            fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
          }}
          {...p}
        />
      )
    return <strong style={{ color: 'var(--text-primary)', fontWeight: 600, fontSize: 12 }} {...p} />
  },
  em: (p: any) => <em style={{ color: 'var(--text-muted)', fontStyle: 'italic', fontSize: 12 }} {...p} />,
  code: (p: any) => (
    <code
      style={{
        color: 'var(--accent)', background: 'rgba(0,73,119,0.06)',
        padding: '1px 4px', borderRadius: 3, fontSize: 11,
        fontFamily: 'JetBrains Mono, monospace',
      }}
      {...p}
    />
  ),
  table: (p: any) => (
    <table style={{ width: '100%', borderCollapse: 'collapse', margin: '6px 0', fontSize: 11 }} {...p} />
  ),
  th: (p: any) => (
    <th
      style={{
        border: '1px solid var(--border)', padding: '4px 6px',
        color: '#fff', background: 'var(--accent)', fontWeight: 600,
        fontSize: 10, textTransform: 'uppercase' as const, letterSpacing: '0.04em',
      }}
      {...p}
    />
  ),
  td: (p: any) => (
    <td
      style={{
        border: '1px solid var(--border)', padding: '3px 6px',
        fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
      }}
      {...p}
    />
  ),
  blockquote: (p: any) => (
    <blockquote
      style={{
        borderLeft: '3px solid var(--accent)',
        background: 'rgba(0,73,119,0.03)',
        paddingLeft: 10, paddingTop: 4, paddingBottom: 4, paddingRight: 6,
        margin: '6px 0', borderRadius: '0 4px 4px 0', fontSize: 12,
        color: 'var(--text-secondary)',
      }}
      {...p}
    />
  ),
  hr: () => <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: '8px 0' }} />,
}

// ── ```beta_scatter fenced-block renderer ───────────────────────────────
// The `beta-visualizer` agent emits a fenced ```beta_scatter block of the
// form {title, tolerance, points: [{product, historical_beta,
// projected_beta, ...}]}. We intercept that before passing the message
// body through ReactMarkdown so it renders as a Recharts scatter inline.
type BetaScatterSpec = {
  title?: string
  tolerance?: number
  points: {
    product: string
    historical_beta: number
    projected_beta: number
    r_squared?: number
    status?: string
  }[]
}

function BetaScatterChart({ spec }: { spec: BetaScatterSpec }) {
  const tol = spec.tolerance ?? 0.10
  const points = spec.points || []
  if (!points.length) return null
  const all = points.flatMap((p) => [p.historical_beta, p.projected_beta])
  const lo = Math.max(0, Math.min(...all) - 0.05)
  const hi = Math.min(1, Math.max(...all) + 0.05)
  const dotColor = (p: BetaScatterSpec['points'][number]) => {
    const gap = p.projected_beta - p.historical_beta
    if (Math.abs(gap) <= tol) return '#059669'   // ALIGNED — green
    return gap > 0 ? '#DC2626' : '#D97706'        // OVERSHOOT red / UNDERSHOOT amber
  }
  return (
    <div
      className="rounded-lg my-3"
      style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', padding: 12 }}
    >
      <div
        className="text-[12px] font-bold uppercase tracking-widest mb-2"
        style={{ color: 'var(--text-secondary)' }}
      >
        {spec.title || 'Projected vs historical beta'}
      </div>
      <ResponsiveContainer width="100%" height={260}>
        <ScatterChart margin={{ top: 12, right: 18, left: 0, bottom: 24 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
          <XAxis
            type="number" dataKey="historical_beta"
            domain={[lo, hi]}
            tick={{ fontSize: 10, fill: 'var(--text-muted)' }}
            label={{
              value: 'Historical β', position: 'insideBottom', offset: -8,
              style: { fontSize: 11, fill: 'var(--text-secondary)' },
            }}
          />
          <YAxis
            type="number" dataKey="projected_beta"
            domain={[lo, hi]}
            tick={{ fontSize: 10, fill: 'var(--text-muted)' }}
            label={{
              value: 'Projected β', angle: -90, position: 'insideLeft',
              style: { fontSize: 11, fill: 'var(--text-secondary)' },
            }}
          />
          <RechartsTooltip
            cursor={{ strokeDasharray: '3 3' }}
            contentStyle={{
              background: 'var(--bg-card)', border: '1px solid var(--border)',
              borderRadius: 8, fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
            }}
            formatter={(_v: any, _n: any, p: any) => {
              const r = p?.payload
              const gap = (r?.projected_beta - r?.historical_beta).toFixed(3)
              return [`${r?.product}: hist ${r?.historical_beta} / proj ${r?.projected_beta} (gap ${gap})`, '']
            }}
          />
          {/* 45° perfect-agreement line */}
          <ReferenceLine
            segment={[{ x: lo, y: lo }, { x: hi, y: hi }]}
            stroke="var(--text-muted)" strokeDasharray="4 4"
          />
          <Scatter data={points} fill="#0EA5E9">
            {points.map((p, i) => <Cell key={i} fill={dotColor(p)} />)}
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap gap-3 mt-2 text-[10px]" style={{ color: 'var(--text-muted)' }}>
        <span><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: '#059669', marginRight: 4 }} />Aligned (±{tol})</span>
        <span><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: '#DC2626', marginRight: 4 }} />Overshoot</span>
        <span><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: '#D97706', marginRight: 4 }} />Undershoot</span>
      </div>
    </div>
  )
}

/** Splits message content into markdown segments + ```beta_scatter spec
 *  segments, rendering the latter as a Recharts ScatterChart inline. */
function ChatBody({ content }: { content: string }) {
  const segments: ({ kind: 'md'; text: string } | { kind: 'beta'; spec: BetaScatterSpec })[] = []
  const re = /```beta_scatter\n([\s\S]*?)```/g
  let cursor = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(content)) !== null) {
    if (m.index > cursor) segments.push({ kind: 'md', text: content.slice(cursor, m.index) })
    try {
      segments.push({ kind: 'beta', spec: JSON.parse(m[1]) as BetaScatterSpec })
    } catch {
      // Malformed JSON inside the fence — render the raw block as code.
      segments.push({ kind: 'md', text: m[0] })
    }
    cursor = m.index + m[0].length
  }
  if (cursor < content.length) segments.push({ kind: 'md', text: content.slice(cursor) })

  return (
    <>
      {segments.map((s, i) => s.kind === 'beta'
        ? <BetaScatterChart key={i} spec={s.spec} />
        : (
          <ReactMarkdown key={i} remarkPlugins={[remarkGfm]} components={mdComponents}>
            {s.text}
          </ReactMarkdown>
        )
      )}
    </>
  )
}

const QUICK_QUERIES = ['Brief me', 'Explain a metric', 'Risk alerts', 'Run a scenario', 'Draft report']

export default function ChatPanel({ open, onClose }: ChatPanelProps) {
  const messages = useChatStore((s) => s.messages)
  const isLoading = useChatStore((s) => s.isLoading)
  const pageContext = useChatStore((s) => s.pageContext)
  const functionId = useChatStore((s) => s.functionId)
  const tab = useChatStore((s) => s.tab)
  const entityKind = useChatStore((s) => s.entityKind)
  const entityId = useChatStore((s) => s.entityId)
  const payload = useChatStore((s) => s.payload)
  const addMessage = useChatStore((s) => s.addMessage)
  const setLoading = useChatStore((s) => s.setLoading)
  const clearMessages = useChatStore((s) => s.clearMessages)

  const [input, setInput] = useState('')
  const [panelWidth, setPanelWidth] = useState(400)
  const [maximized, setMaximized] = useState(false)
  // Sidebar takes 240px on the left (see Sidebar.tsx); maximizing claims
  // every pixel to the right of it without covering the navigation.
  const SIDEBAR_WIDTH = 240
  const effectiveWidth = maximized
    ? `calc(100vw - ${SIDEBAR_WIDTH}px)`
    : `${panelWidth}px`
  const isDragging = useRef(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [feedback, setFeedback] = useState<Record<string, 'up' | 'down'>>({})

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  // Maximize is a transient view preference, not a sticky setting — reset
  // it whenever the panel closes so reopening always starts at the
  // analyst's preferred default (resizable narrow side panel).
  useEffect(() => {
    if (!open) setMaximized(false)
  }, [open])

  // On every open, jump to the most recent message instead of the top.
  // Without this, reopening the panel scrolls to the very first message
  // in the persisted thread, which buries any answer the analyst is
  // most likely returning to read.
  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => {
      bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
    }, 30)
    return () => clearTimeout(t)
  }, [open])

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail && typeof detail === 'string') sendMessage(detail)
    }
    window.addEventListener('cma-chat', handler)
    return () => window.removeEventListener('cma-chat', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageContext, functionId])

  const handleMouseDown = (e: React.MouseEvent) => {
    if (maximized) return  // resize via the handle is meaningless when full-width
    e.preventDefault()
    isDragging.current = true
    const startX = e.clientX
    const startWidth = panelWidth
    const onMove = (ev: MouseEvent) => {
      if (!isDragging.current) return
      const delta = startX - ev.clientX
      setPanelWidth(Math.max(340, Math.min(720, startWidth + delta)))
    }
    const onUp = () => {
      isDragging.current = false
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const sendMessage = async (content: string) => {
    if (!content.trim()) return
    // Read the latest store snapshot at SEND time, not at the time the closure
    // was captured. Without this, the cma-chat event listener fires with stale
    // entity context from the previous render.
    const snap = useChatStore.getState()
    addMessage({ role: 'user', content, timestamp: new Date().toISOString() })
    setInput('')
    setLoading(true)
    try {
      const ctxBundle = snap.pageContext ? `[Context: ${snap.pageContext}]\n\n${content}` : content
      // Ship the last 10 prior turns so the agent can resolve back-references
      // like "the pie chart you suggested". `snap` was captured before the
      // user message was appended, so snap.messages already excludes it.
      const history = snap.messages
        .slice(-10)
        .map((m) => ({ role: m.role, content: m.content }))
      const res = await api.post('/api/chat/message', {
        message: ctxBundle,
        function_id: snap.functionId,
        agent_id: 'orchestrator',
        tab: snap.tab,
        entity_kind: snap.entityKind,
        entity_id: snap.entityId,
        payload: snap.payload,
        history,
      })
      addMessage({
        role: 'assistant',
        content: res.data.response || JSON.stringify(res.data),
        timestamp: new Date().toISOString(),
        agent_id: res.data.agent_id,
        agent_name: res.data.agent_name,
        agent_color: res.data.agent_color,
        agent_icon: res.data.agent_icon,
        actions: res.data.actions || [],
        trace: res.data.trace || [],
      } as any)
      // If the plot-tuner mutated a tile or analytic-def, broadcast a
      // refresh event so the open card re-fetches its spec/preview.
      const trace: any[] = res.data.trace || []
      const mutated = trace.some((s) => s.kind === 'tool_call' && [
        'apply_filter', 'set_sort', 'set_chart_type', 'set_axes',
        'set_axis_labels', 'set_style', 'apply_tile_filter',
      ].includes(s.tool_name))
      if (mutated) {
        if (snap.entityKind === 'tile' && snap.entityId) {
          window.dispatchEvent(new CustomEvent('cma-tile-updated', { detail: { plot_id: snap.entityId } }))
        }
        if (snap.entityKind === 'analytic_def' && snap.entityId) {
          window.dispatchEvent(new CustomEvent('cma-analytic-updated', { detail: { definition_id: snap.entityId } }))
        }
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      const status = err?.response?.status
      const detailStr = typeof detail === 'string'
        ? detail
        : detail
          ? JSON.stringify(detail)
          : (err?.message || 'unknown error')
      addMessage({
        role: 'assistant',
        content:
          `**Sorry — the agent call failed.**\n\n` +
          (status ? `\`HTTP ${status}\`\n\n` : '') +
          '```\n' + detailStr + '\n```',
        timestamp: new Date().toISOString(),
      })
    } finally {
      setLoading(false)
    }
  }

  const runAction = async (action: ChatAction) => {
    if (action.kind === 'apply_filter' && action.target && action.payload) {
      try {
        await api.post(`/api/plots/${action.target}/filters`, { append: action.payload })
        // Tell the world the tile changed so previews refresh
        window.dispatchEvent(new CustomEvent('cma-tile-updated', { detail: { plot_id: action.target } }))
        addMessage({
          role: 'assistant',
          content: `✓ Applied filter: \`${action.payload.field} ${action.payload.op} ${action.payload.value}\` to tile.`,
          timestamp: new Date().toISOString(),
          agent_id: 'tile_tuner',
          agent_name: 'Tile Tuner',
          agent_color: '#0F766E',
          agent_icon: 'sliders-horizontal',
        })
      } catch (e: any) {
        addMessage({
          role: 'assistant',
          content: `Could not apply filter: ${e?.response?.data?.detail || e.message}`,
          timestamp: new Date().toISOString(),
        })
      }
    } else if (action.kind === 'run_validation') {
      sendMessage('Validate the workflow.')
    } else if (action.kind === 'troubleshoot') {
      sendMessage('Help me troubleshoot this.')
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  if (!open) return null

  return (
    <>
      <div
        className="fixed inset-0 z-40"
        style={{ background: 'rgba(11,15,25,0.30)' }}
        onClick={onClose}
      />
      <div
        className="fixed right-0 top-0 bottom-0 z-50 flex flex-col"
        style={{
          width: effectiveWidth,
          background: 'var(--bg-card)',
          borderLeft: '1px solid var(--border)',
          boxShadow: '-8px 0 40px rgba(0,0,0,0.10)',
        }}
      >
        <div
          onMouseDown={handleMouseDown}
          className="absolute left-0 top-0 bottom-0 z-10"
          style={{ width: 5, cursor: 'col-resize' }}
          onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = 'var(--accent)')}
          onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = 'transparent')}
        />

        {/* Header — reflects the most recent responding agent. While a
            request is in flight we explicitly drop into a neutral
            "Thinking…" state instead of showing the previous agent's
            name, which previously read like the *new* request was
            being handled by the *old* agent. */}
        {(() => {
          const latest = [...messages].reverse().find((m) => m.role === 'assistant')
          const agentName = isLoading ? 'Thinking…' : (latest?.agent_name || 'CMA Agent')
          const agentColor = isLoading ? 'var(--text-secondary)' : (latest?.agent_color || 'var(--accent)')
          const HeaderIcon = isLoading
            ? Sparkles
            : ((latest?.agent_icon && AGENT_ICON[latest.agent_icon]) || Sparkles)
          const subline = isLoading
            ? 'routing…'
            : (latest?.agent_id || (functionId ? functionId.replace(/_/g, ' ') : 'self-serve'))
          return (
        <div
          className="flex items-center justify-between px-5 py-4"
          style={{
            background: `linear-gradient(135deg, ${agentColor}, color-mix(in srgb, ${agentColor} 70%, var(--teal)))`,
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-xl flex items-center justify-center"
              style={{ background: 'rgba(255,255,255,0.20)' }}
            >
              <HeaderIcon size={14} color="#fff" />
            </div>
            <div>
              <div className="font-display text-sm font-semibold" style={{ color: '#fff' }}>
                {agentName}
              </div>
              <div
                className="font-mono"
                style={{ fontSize: 10, color: 'rgba(255,255,255,0.75)' }}
              >
                {tab ? `${tab} · ` : ''}{subline}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => {
                if (messages.length === 0) return
                if (window.confirm('Clear the entire chat thread? This cannot be undone.')) {
                  clearMessages()
                }
              }}
              disabled={messages.length === 0}
              title="Clear chat"
              className="p-1.5 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-default"
              style={{ color: 'rgba(255,255,255,0.75)' }}
              onMouseEnter={(e) => {
                if (messages.length === 0) return
                const el = e.currentTarget as HTMLElement
                el.style.background = 'rgba(255,255,255,0.15)'
                el.style.color = '#fff'
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'transparent'
                el.style.color = 'rgba(255,255,255,0.75)'
              }}
            >
              <Trash2 size={14} />
            </button>
            <button
              onClick={() => setMaximized((m) => !m)}
              title={maximized ? 'Restore default size' : 'Maximize chat (full panel, sidebar stays visible)'}
              className="p-1.5 rounded-lg transition-colors"
              style={{ color: 'rgba(255,255,255,0.75)' }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'rgba(255,255,255,0.15)'
                el.style.color = '#fff'
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'transparent'
                el.style.color = 'rgba(255,255,255,0.75)'
              }}
            >
              {maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </button>
            <button
              onClick={onClose}
              title="Close"
              className="p-1.5 rounded-lg transition-colors"
              style={{ color: 'rgba(255,255,255,0.75)' }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'rgba(255,255,255,0.15)'
                el.style.color = '#fff'
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'transparent'
                el.style.color = 'rgba(255,255,255,0.75)'
              }}
            >
              <X size={15} />
            </button>
          </div>
        </div>
          )
        })()}

        {pageContext && (
          <div
            className="px-4 py-1.5 flex items-center gap-2"
            style={{
              background: 'rgba(0,73,119,0.05)',
              borderBottom: '1px solid var(--border-subtle)',
            }}
          >
            <div className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--accent)' }} />
            <span className="text-[10px] font-mono truncate" style={{ color: 'var(--accent)' }}>
              {pageContext.slice(0, 80)}
              {pageContext.length > 80 ? '…' : ''}
            </span>
          </div>
        )}

        <div
          className="flex gap-1.5 px-4 py-2.5 flex-wrap"
          style={{ borderBottom: '1px solid var(--border-subtle)' }}
        >
          {QUICK_QUERIES.map((q) => (
            <button
              key={q}
              onClick={() => sendMessage(q)}
              className="text-xs px-2.5 py-1 rounded-lg transition-colors font-medium"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border)',
                color: 'var(--text-secondary)',
              }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.borderColor = 'var(--accent)'
                el.style.color = 'var(--accent)'
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement
                el.style.borderColor = 'var(--border)'
                el.style.color = 'var(--text-secondary)'
              }}
            >
              {q}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          <div className="space-y-3">
            {messages.length === 0 && (
              <div className="text-center mt-12">
                <div className="text-4xl mb-3 opacity-20">✦</div>
                <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
                  Ask me to brief, explain, or run a scenario.
                </p>
              </div>
            )}
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={cn('flex animate-fade-in', msg.role === 'user' ? 'justify-end' : 'justify-start')}
              >
                <div
                  className="max-w-[88%] rounded-xl px-3 py-2 text-sm"
                  style={
                    msg.role === 'user'
                      ? {
                          background: 'var(--accent-light)',
                          color: 'var(--text-primary)',
                          border: '1px solid var(--accent-light)',
                        }
                      : {
                          background: 'var(--bg-elevated)',
                          border: '1px solid var(--border-subtle)',
                          color: 'var(--text-primary)',
                        }
                  }
                >
                  {msg.role === 'assistant' ? (
                    <>
                      {msg.agent_name && msg.agent_color && (
                        <div
                          className="flex items-center gap-1.5 mb-1.5 pb-1"
                          style={{
                            borderBottom: '1px solid var(--border-subtle)',
                          }}
                        >
                          {(() => {
                            const I = (msg.agent_icon && AGENT_ICON[msg.agent_icon]) || Sparkles
                            return (
                              <I size={10} style={{ color: msg.agent_color }} />
                            )
                          })()}
                          <span
                            style={{
                              fontSize: 10, fontWeight: 700, letterSpacing: '0.04em',
                              textTransform: 'uppercase', color: msg.agent_color,
                            }}
                          >
                            {msg.agent_name}
                          </span>
                        </div>
                      )}
                      <div
                        className="max-w-none"
                        style={{
                          fontSize: 12,
                          lineHeight: 1.6,
                          fontFamily: "'Instrument Sans', -apple-system, sans-serif",
                          color: 'var(--text-secondary)',
                        }}
                      >
                        <ChatBody content={msg.content} />
                      </div>
                      {/* "Steps taken" trace summary intentionally hidden —
                          the trace is still captured server-side and shipped
                          on the message for downstream eval, but the chat
                          panel keeps the response clean. */}
                      {msg.actions && msg.actions.length > 0 && (
                        <div className="flex flex-wrap gap-1.5 mt-2 pt-2" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                          {msg.actions.map((a, i) => (
                            <button
                              key={i}
                              onClick={() => runAction(a)}
                              disabled={a.kind === 'noop'}
                              className="text-[11px] px-2 py-1 rounded-md transition-colors flex items-center gap-1 disabled:opacity-50 disabled:cursor-default"
                              style={{
                                background: a.kind === 'apply_filter' ? 'rgba(15,118,110,0.10)' : 'var(--bg-card)',
                                border: `1px solid ${a.kind === 'apply_filter' ? '#0F766E' : 'var(--border)'}`,
                                color: a.kind === 'apply_filter' ? '#0F766E' : 'var(--text-secondary)',
                                fontWeight: 600,
                              }}
                            >
                              {a.kind === 'apply_filter' && <Filter size={10} />}
                              {a.label}
                            </button>
                          ))}
                        </div>
                      )}

                      {/* Thumbs up/down — captured for offline LLM eval. */}
                      <FeedbackRow
                        msgId={msg.id}
                        rating={feedback[msg.id]}
                        onRate={(rating) => {
                          // Optimistic local update; the user can still flip
                          // it. Backend logs every submission separately.
                          setFeedback((prev) => ({ ...prev, [msg.id]: rating }))
                          // Find the user message that triggered this reply
                          // for richer eval context: it's the most recent
                          // 'user' role above this message.
                          const idx = messages.findIndex((m) => m.id === msg.id)
                          let userMsg: string | undefined
                          for (let i = idx - 1; i >= 0; i--) {
                            if (messages[i].role === 'user') {
                              userMsg = messages[i].content
                              break
                            }
                          }
                          const snap = useChatStore.getState()
                          api.post('/api/chat/feedback', {
                            message_id: msg.id,
                            rating,
                            agent_id: msg.agent_id,
                            agent_name: msg.agent_name,
                            user_message: userMsg,
                            assistant_message: msg.content,
                            function_id: snap.functionId,
                            tab: snap.tab,
                            entity_kind: snap.entityKind,
                            entity_id: snap.entityId,
                          }).catch(() => {})
                        }}
                      />
                    </>
                  ) : (
                    <span style={{ fontSize: 13 }}>{msg.content}</span>
                  )}
                </div>
              </div>
            ))}
            {isLoading && (
              <div className="flex justify-start">
                <div
                  className="rounded-xl px-3 py-2 text-xs font-mono"
                  style={{
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border-subtle)',
                    color: 'var(--text-muted)',
                  }}
                >
                  <span className="animate-pulse">●●●</span>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </div>

        <div className="px-4 py-4" style={{ borderTop: '1px solid var(--border)' }}>
          <div className="flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about a metric, request a scenario, draft a memo…"
              disabled={isLoading}
              className="flex-1 text-sm rounded-xl px-3 py-2.5 transition-all"
              style={{
                background: 'var(--bg-elevated)',
                border: '1.5px solid var(--border)',
                color: 'var(--text-primary)',
              }}
              onFocus={(e) => {
                e.currentTarget.style.background = 'var(--bg-card)'
                e.currentTarget.style.borderColor = 'var(--accent)'
              }}
              onBlur={(e) => {
                e.currentTarget.style.background = 'var(--bg-elevated)'
                e.currentTarget.style.borderColor = 'var(--border)'
              }}
            />
            <button
              onClick={() => sendMessage(input)}
              disabled={isLoading || !input.trim()}
              className="px-3 py-2 rounded-xl transition-all disabled:opacity-40"
              style={{ background: 'var(--accent)', color: '#fff' }}
            >
              <Send size={14} />
            </button>
          </div>
        </div>
      </div>
    </>
  )
}

function FeedbackRow({
  msgId, rating, onRate,
}: {
  msgId: string
  rating: 'up' | 'down' | undefined
  onRate: (r: 'up' | 'down') => void
}) {
  const upActive = rating === 'up'
  const downActive = rating === 'down'
  return (
    <div
      className="flex items-center gap-1 mt-2 pt-2"
      style={{ borderTop: '1px solid var(--border-subtle)' }}
    >
      <span
        className="text-[10px] mr-1"
        style={{ color: 'var(--text-muted)', letterSpacing: '0.04em', textTransform: 'uppercase' }}
      >
        Helpful?
      </span>
      <button
        onClick={() => onRate('up')}
        title={upActive ? 'You marked this helpful' : 'Mark helpful'}
        className="p-1 rounded transition-colors"
        style={{
          background: upActive ? 'rgba(5,150,105,0.10)' : 'transparent',
          color: upActive ? 'var(--success)' : 'var(--text-muted)',
        }}
        onMouseEnter={(e) => {
          if (upActive) return
          ;(e.currentTarget as HTMLElement).style.color = 'var(--success)'
        }}
        onMouseLeave={(e) => {
          if (upActive) return
          ;(e.currentTarget as HTMLElement).style.color = 'var(--text-muted)'
        }}
      >
        <ThumbsUp size={11} fill={upActive ? 'currentColor' : 'none'} />
      </button>
      <button
        onClick={() => onRate('down')}
        title={downActive ? 'You marked this not helpful' : 'Mark not helpful'}
        className="p-1 rounded transition-colors"
        style={{
          background: downActive ? 'rgba(220,38,38,0.10)' : 'transparent',
          color: downActive ? 'var(--error)' : 'var(--text-muted)',
        }}
        onMouseEnter={(e) => {
          if (downActive) return
          ;(e.currentTarget as HTMLElement).style.color = 'var(--error)'
        }}
        onMouseLeave={(e) => {
          if (downActive) return
          ;(e.currentTarget as HTMLElement).style.color = 'var(--text-muted)'
        }}
      >
        <ThumbsDown size={11} fill={downActive ? 'currentColor' : 'none'} />
      </button>
      <span
        className="ml-auto text-[9px] font-mono"
        style={{ color: 'var(--text-muted)', opacity: 0.6 }}
        title={msgId}
      >
        {msgId.slice(-6)}
      </span>
    </div>
  )
}
