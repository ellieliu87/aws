import { useEffect, useMemo, useRef, useState } from 'react'
// react-grid-layout v1.5 — Responsive + WidthProvider auto-measures the
// container. v2 dropped WidthProvider and required passing width as a
// prop, which broke layout calculation entirely (cards overlapping).
import { Responsive, WidthProvider } from 'react-grid-layout'
const ResponsiveGridLayout = WidthProvider(Responsive)
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'

interface LayoutItem {
  i: string
  x: number
  y: number
  w: number
  h: number
  minW?: number
  minH?: number
  static?: boolean
}
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ArrowDown, ArrowUp, Lightbulb, Pin, Edit3, Check,
  BarChart3, Table as TableIcon, Plus, Type, RotateCcw, X, Move,
  Download, Upload, LayoutGrid, LayoutTemplate, Rows3, Sparkles,
} from 'lucide-react'
import api from '@/lib/api'
import Chart from '@/components/charts/Chart'
import InteractiveTable from '@/components/charts/InteractiveTable'
import { useChatStore } from '@/store/chatStore'
import type { ChartSpec, KpiPreview, PlotConfig, WorkspaceData } from '@/types'

// Always 12 cols regardless of viewport width. Dashboard tools (Grafana,
// QuickSight, Looker) don't reflow grids at smaller widths — they let
// users scroll. Reflowing into fewer columns mangles a layout we've
// carefully built for 12 cols (e.g. 4 KPIs at x=0,3,6,9 collapse into
// 3 rows when cols drops to 8).
const COLS = { lg: 12, md: 12, sm: 12, xs: 12, xxs: 12 }
const BREAKPOINTS = { lg: 1280, md: 992, sm: 768, xs: 480, xxs: 0 }
const ROW_HEIGHT = 36
const DRAG_HANDLE = '.cma-card-drag-handle'

interface Props {
  functionId: string
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

type CardKind = 'kpi' | 'chart' | 'table' | 'pinned' | 'insights' | 'text'

interface CardMeta {
  id: string                // stable identity key, e.g. `kpi:Net Yield`
  kind: CardKind
  defaultW: number
  defaultH: number
  minW: number
  minH: number
}

interface TextCard {
  id: string                // suffix only; full key is `text:<id>`
  body: string
}

interface HiddenSet {
  ids: string[]             // card ids the user removed in edit mode
}

interface ServerDefaultBundle {
  layout: LayoutItem[]
  hidden: HiddenSet
  text_cards: TextCard[]
  saved_by?: string | null
  saved_at?: string | null
}

// ── Templates ─────────────────────────────────────────────────────────────
// Each template builds a count-aware layout. Guiding rule: every row should
// fill the 12-col grid evenly, never leave an orphan card at half-width on
// the last row, and never leave a column gap. 4 KPIs → 4-up, 3 KPIs → 3-up,
// 5 KPIs → 3-up + 2-up (both rows full), 7 charts → 4-up + 3-up. Templates
// pass a `maxPerRow` ceiling so KPIs can pack 6 across while charts stay
// readable at 3 or 4.
type TemplateId = 'executive' | 'analyst' | 'single'

interface Template {
  id: TemplateId
  name: string
  description: string
  icon: any
  build: (cards: CardMeta[], startY: number) => LayoutItem[]
}

interface PlacedRow { items: LayoutItem[]; nextY: number }

/** Place items in a fixed-width grid (e.g. always 6 cols → 2-up, always
 *  4 cols → 3-up). Used for charts/tables/pinned/text where the template
 *  intentionally picks a per-row count. */
function placeInGrid(items: CardMeta[], y: number, w: number, h: number): PlacedRow {
  if (items.length === 0) return { items: [], nextY: y }
  const perRow = Math.max(1, Math.floor(12 / w))
  const placed = items.map((c, i) => ({
    i: c.id,
    x: (i % perRow) * w,
    y: y + Math.floor(i / perRow) * h,
    w, h,
    minH: c.minH,
  }))
  return { items: placed, nextY: y + Math.ceil(items.length / perRow) * h }
}

/** Place items so every row spans all 12 columns — no gaps, no orphans.
 *
 *  - n that divides 12 (1,2,3,4,6,12) → single row at w = 12/n.
 *  - n=5 → 3 on top (w=4) + 2 below (w=6).
 *  - n=7 → 4 on top (w=3) + 3 below (w=4).
 *  - n=8 → two rows of 4 (w=3).
 *  - n=9 → three rows of 3 (w=4).
 *  - n=10 → 4+3+3.
 *  - n=11 → 4+4+3.
 *  - everything else wraps at `maxPerRow` (cards may be wider than ideal,
 *    but rows always fill).
 *
 *  `maxPerRow` lets the caller cap density — KPIs use 6, executive charts
 *  use 3, analyst charts use 4.
 */
function spreadBalanced(
  items: CardMeta[],
  y: number,
  h: number,
  maxPerRow = 6,
): PlacedRow {
  if (items.length === 0) return { items: [], nextY: y }
  const n = items.length

  // Helper: stack two row-groups, each filling 12 cols.
  const stack = (top: CardMeta[], wTop: number, bot: CardMeta[], wBot: number): PlacedRow => {
    const a = placeInGrid(top, y, wTop, h)
    const b = placeInGrid(bot, a.nextY, wBot, h)
    return { items: [...a.items, ...b.items], nextY: b.nextY }
  }

  // Single-row exact fits, capped by maxPerRow.
  if (n === 1) return placeInGrid(items, y, 12, h)
  if (n === 2 && maxPerRow >= 2) return placeInGrid(items, y, 6, h)
  if (n === 3 && maxPerRow >= 3) return placeInGrid(items, y, 4, h)
  if (n === 4 && maxPerRow >= 4) return placeInGrid(items, y, 3, h)
  if (n === 6 && maxPerRow >= 6) return placeInGrid(items, y, 2, h)

  // Two-row exact fits.
  if (n === 5 && maxPerRow >= 3) return stack(items.slice(0, 3), 4, items.slice(3), 6)
  if (n === 7 && maxPerRow >= 4) return stack(items.slice(0, 4), 3, items.slice(4), 4)
  if (n === 8 && maxPerRow >= 4) return placeInGrid(items, y, 3, h)
  if (n === 9 && maxPerRow >= 3) return placeInGrid(items, y, 4, h)
  if (n === 10 && maxPerRow >= 4) {
    // 4+3+3 — three balanced rows.
    const a = placeInGrid(items.slice(0, 4), y, 3, h)
    const b = placeInGrid(items.slice(4, 7), a.nextY, 4, h)
    const c = placeInGrid(items.slice(7), b.nextY, 4, h)
    return { items: [...a.items, ...b.items, ...c.items], nextY: c.nextY }
  }
  if (n === 11 && maxPerRow >= 4) {
    // 4+4+3.
    const a = placeInGrid(items.slice(0, 4), y, 3, h)
    const b = placeInGrid(items.slice(4, 8), a.nextY, 3, h)
    const c = placeInGrid(items.slice(8), b.nextY, 4, h)
    return { items: [...a.items, ...b.items, ...c.items], nextY: c.nextY }
  }

  // Fallback for very large counts or low maxPerRow: wrap at maxPerRow.
  // The last row may be partial, but at this point density beats symmetry.
  const w = Math.max(1, Math.floor(12 / maxPerRow))
  return placeInGrid(items, y, w, h)
}

const TEMPLATES: Template[] = [
  {
    id: 'executive',
    name: 'Executive Brief',
    description: 'KPIs across the top, insights as a headline strip, large charts in balanced pairs, full-width tables. Generous whitespace for stakeholder readouts.',
    icon: LayoutTemplate,
    build: (cards, startY) => {
      const out: LayoutItem[] = []
      let y = startY
      const append = (r: PlacedRow) => { out.push(...r.items); y = r.nextY }

      // KPIs: short strip — h=3 (~108px) is enough for label + value + delta.
      // Up to 6 across, balanced layouts beyond that (5→3+2, 7→4+3).
      append(spreadBalanced(cards.filter((c) => c.kind === 'kpi'), y, 3, 6))
      append(placeInGrid(cards.filter((c) => c.kind === 'insights'), y, 12, 7))

      // Charts: capped at 3-up so each chart stays readable for stakeholders.
      // 1→full, 2→pair, 3→trio, 4→2x2, 5→3+2, 6→3+3, etc.
      append(spreadBalanced(cards.filter((c) => c.kind === 'chart'), y, 10, 3))

      // Tables: full-width stacked — multi-column tables read better that way.
      append(placeInGrid(cards.filter((c) => c.kind === 'table'), y, 12, 8))

      append(spreadBalanced(cards.filter((c) => c.kind === 'pinned'), y, 9, 3))
      append(spreadBalanced(cards.filter((c) => c.kind === 'text'), y, 4, 3))
      return out
    },
  },
  {
    id: 'analyst',
    name: 'Analyst Dashboard',
    description: 'Compact KPI row, denser chart grid for breadth, full-width tables for scanning rows, insights below. Information-dense for daily monitoring.',
    icon: LayoutGrid,
    build: (cards, startY) => {
      const out: LayoutItem[] = []
      let y = startY
      const append = (r: PlacedRow) => { out.push(...r.items); y = r.nextY }

      append(spreadBalanced(cards.filter((c) => c.kind === 'kpi'), y, 3, 6))

      // Charts: capped at 4-up for density. 1→full, 2→pair, 3→trio, 4→quad,
      // 5→3+2, 6→3+3, 7→4+3, 8→4+4 — every row fills the grid.
      append(spreadBalanced(cards.filter((c) => c.kind === 'chart'), y, 8, 4))

      append(placeInGrid(cards.filter((c) => c.kind === 'table'), y, 12, 7))
      append(placeInGrid(cards.filter((c) => c.kind === 'insights'), y, 12, 7))
      append(spreadBalanced(cards.filter((c) => c.kind === 'pinned'), y, 8, 4))
      append(spreadBalanced(cards.filter((c) => c.kind === 'text'), y, 4, 3))
      return out
    },
  },
  {
    id: 'single',
    name: 'Single Column',
    description: 'KPI row up top, every other card stacked at full width. Linear reading order — good for narrow screens or focused review.',
    icon: Rows3,
    build: (cards, startY) => {
      const out: LayoutItem[] = []
      let y = startY
      const append = (r: PlacedRow) => { out.push(...r.items); y = r.nextY }

      // KPIs still spread across the top — even single-column readers want
      // their headline numbers side-by-side.
      append(spreadBalanced(cards.filter((c) => c.kind === 'kpi'), y, 3, 6))
      append(placeInGrid(cards.filter((c) => c.kind === 'insights'), y, 12, 7))
      append(placeInGrid(cards.filter((c) => c.kind === 'chart'), y, 12, 9))
      append(placeInGrid(cards.filter((c) => c.kind === 'pinned'), y, 12, 9))
      append(placeInGrid(cards.filter((c) => c.kind === 'table'), y, 12, 8))
      append(placeInGrid(cards.filter((c) => c.kind === 'text'), y, 12, 4))
      return out
    },
  },
]

const DEFAULT_TEMPLATE: TemplateId = 'executive'

// ── localStorage helpers ──────────────────────────────────────────────────
// Layout key is versioned (v2) so positions saved by the earlier
// cursor-based auto-placement and the v1 template (which used a fixed
// per-kind width that didn't fit 4 KPIs in one row for the Analyst preset)
// are orphaned. Hidden + textcards keys are also bumped because card
// id schemes were unstable across earlier iterations. Template id and
// text-card content survive (templateKey unbumped, no card-id refs).
// All keys are versioned together so a single bump (when defaults or
// schemas change) gives every analyst a clean Overview without losing
// the localStorage entries from older builds in subtle, half-loaded ways.
// The text-cards key is bumped here so prior placeholder cards from the
// "click Add Text once and explore" flow don't carry over: a fresh
// dashboard now starts empty unless the user clicks Add Text.
const layoutKey   = (fn: string) => `cma:overview:layout:v6:${fn}`
const textCardsKey = (fn: string) => `cma:overview:textcards:v2:${fn}`
const hiddenKey   = (fn: string) => `cma:overview:hidden:v3:${fn}`
const templateKey = (fn: string) => `cma:overview:template:v2:${fn}`
const insightsSkillKey = (fn: string) => `cma:overview:insights_skill:${fn}`

const DEFAULT_INSIGHTS_SKILL = 'overview-insights'

function loadInsightsSkill(fn: string): string {
  try {
    const raw = localStorage.getItem(insightsSkillKey(fn))
    if (raw) return raw
  } catch {}
  return DEFAULT_INSIGHTS_SKILL
}
function saveInsightsSkill(fn: string, id: string) {
  try { localStorage.setItem(insightsSkillKey(fn), id) } catch {}
}

function loadLayout(fn: string): LayoutItem[] {
  try {
    const raw = localStorage.getItem(layoutKey(fn))
    if (raw) return JSON.parse(raw)
  } catch {}
  return []
}
function saveLayout(fn: string, l: LayoutItem[]) {
  try { localStorage.setItem(layoutKey(fn), JSON.stringify(l)) } catch {}
}
function loadTextCards(fn: string): TextCard[] {
  try {
    const raw = localStorage.getItem(textCardsKey(fn))
    if (raw) return JSON.parse(raw)
  } catch {}
  return []
}
function saveTextCards(fn: string, tc: TextCard[]) {
  try { localStorage.setItem(textCardsKey(fn), JSON.stringify(tc)) } catch {}
}
function loadHidden(fn: string): HiddenSet {
  try {
    const raw = localStorage.getItem(hiddenKey(fn))
    if (raw) return JSON.parse(raw)
  } catch {}
  return { ids: [] }
}
function saveHidden(fn: string, h: HiddenSet) {
  try { localStorage.setItem(hiddenKey(fn), JSON.stringify(h)) } catch {}
}
function loadTemplateId(fn: string): TemplateId {
  try {
    const raw = localStorage.getItem(templateKey(fn))
    if (raw === 'executive' || raw === 'analyst' || raw === 'single') return raw
  } catch {}
  return DEFAULT_TEMPLATE
}
function saveTemplateId(fn: string, id: TemplateId) {
  try { localStorage.setItem(templateKey(fn), id) } catch {}
}

export default function OverviewTab({ functionId, onAskAgent, onContextChange }: Props) {
  const setEntity = useChatStore((s) => s.setEntity)
  const [data, setData] = useState<WorkspaceData | null>(null)
  const [pinnedTiles, setPinnedTiles] = useState<PlotConfig[]>([])
  const [previews, setPreviews] = useState<Record<string, PreviewBundle>>({})
  const [error, setError] = useState<string | null>(null)

  const [editMode, setEditMode] = useState(false)
  const [layout, setLayout] = useState<LayoutItem[]>(() => loadLayout(functionId))
  const [textCards, setTextCards] = useState<TextCard[]>(() => loadTextCards(functionId))
  const [hidden, setHidden] = useState<HiddenSet>(() => loadHidden(functionId))
  const [templateId, setTemplateId] = useState<TemplateId>(() => loadTemplateId(functionId))
  const [insightsSkillId, setInsightsSkillId] = useState<string>(() => loadInsightsSkill(functionId))
  const [serverDefault, setServerDefault] = useState<ServerDefaultBundle | null>(null)
  const [savingDefault, setSavingDefault] = useState(false)

  const template = useMemo(
    () => TEMPLATES.find((t) => t.id === templateId) || TEMPLATES[0],
    [templateId],
  )

  // Reload all per-function persisted state when the function changes, and
  // fetch the function-wide default. If the local state is empty (i.e. the
  // user has not customized this function before), apply the server default
  // automatically so a fresh analyst lands on the saved dashboard.
  useEffect(() => {
    const localLayout = loadLayout(functionId)
    const localText = loadTextCards(functionId)
    const localHidden = loadHidden(functionId)
    setLayout(localLayout)
    setTextCards(localText)
    setHidden(localHidden)
    setTemplateId(loadTemplateId(functionId))
    setInsightsSkillId(loadInsightsSkill(functionId))
    setServerDefault(null)

    api.get<ServerDefaultBundle & { function_id: string }>(`/api/overview_layouts/${functionId}`)
      .then((r) => {
        const bundle: ServerDefaultBundle = {
          layout: r.data.layout || [],
          hidden: r.data.hidden || { ids: [] },
          text_cards: r.data.text_cards || [],
          saved_by: r.data.saved_by ?? null,
          saved_at: r.data.saved_at ?? null,
        }
        setServerDefault(bundle)
        const localEmpty =
          localLayout.length === 0
          && localText.length === 0
          && localHidden.ids.length === 0
        if (localEmpty && (bundle.layout.length > 0 || bundle.text_cards.length > 0)) {
          setLayout(bundle.layout)
          setTextCards(bundle.text_cards)
          setHidden(bundle.hidden)
        }
      })
      .catch(() => {
        // 404 (no default saved) is expected — silently ignore.
      })
  }, [functionId])

  // Persist on every change
  useEffect(() => { saveLayout(functionId, layout) }, [functionId, layout])
  useEffect(() => { saveTextCards(functionId, textCards) }, [functionId, textCards])
  useEffect(() => { saveHidden(functionId, hidden) }, [functionId, hidden])
  useEffect(() => { saveTemplateId(functionId, templateId) }, [functionId, templateId])
  useEffect(() => { saveInsightsSkill(functionId, insightsSkillId) }, [functionId, insightsSkillId])

  // Fetch workspace + pinned tiles
  useEffect(() => {
    if (!functionId) return
    setData(null); setError(null); setPinnedTiles([]); setPreviews({})
    api.get<WorkspaceData>(`/api/workspace/${functionId}`)
      .then((r) => setData(r.data))
      .catch((e) => setError(e?.response?.data?.detail || 'Failed to load workspace'))
    api.get<PlotConfig[]>(`/api/plots`, { params: { function_id: functionId, pinned: true } })
      .then((r) => setPinnedTiles(r.data))
      .catch(() => {})
  }, [functionId])

  // Render previews for pinned tiles
  useEffect(() => {
    pinnedTiles.forEach((p) => {
      api.get(`/api/plots/${p.id}/preview`).then((r) => {
        const spec: ChartSpec = {
          id: p.id, title: p.name, type: p.chart_type,
          data: r.data.preview_data, x_key: p.x_field, y_keys: p.y_fields,
          description: p.description || null,
          style: r.data.plot?.style || p.style || null,
        }
        const cols = (r.data.columns || []).map((c: any) => c.name) ||
          (r.data.preview_data[0] ? Object.keys(r.data.preview_data[0]) : [])
        setPreviews((prev) => ({
          ...prev,
          [p.id]: { spec, rows: r.data.preview_data, columns: cols, source: r.data.source, kpi: r.data.kpi },
        }))
      }).catch(() => {})
    })
  }, [pinnedTiles])

  // Page context for the chat panel — summary of pinned tiles only since
  // the Overview is now a pure pinned dashboard.
  useEffect(() => {
    if (!data) return
    const pinNote = pinnedTiles.length > 0
      ? `${pinnedTiles.length} pinned tile${pinnedTiles.length === 1 ? '' : 's'}`
      : 'no pinned tiles yet — pin from the Reporting tab'
    onContextChange(`${data.function_name} (Overview): ${pinNote}`)
    return () => onContextChange(null)
  }, [data, pinnedTiles.length, onContextChange])

  const unpin = async (id: string) => {
    await api.post(`/api/plots/${id}/pin`)
    setPinnedTiles((tiles) => tiles.filter((t) => t.id !== id))
  }

  // ── Card list ──────────────────────────────────────────────────────────
  // The Overview is a pure pinned-tile dashboard: nothing is hardcoded.
  // Users design KPI / plot / table tiles in the Reporting tab and pin
  // them here. Insights still come from the workspace endpoint as a
  // single read-only "Today's Insights" card; text cards are user-added.
  // The pinned card's kind reflects its tile_type so we can size KPIs
  // smaller (3×3) than plots/tables (6×9) by default.
  const cards: CardMeta[] = useMemo(() => {
    if (!data) return []
    const out: CardMeta[] = []
    pinnedTiles.forEach((p) => {
      if (p.tile_type === 'kpi') {
        out.push({
          id: `pinned:${p.id}`, kind: 'kpi',
          defaultW: 3, defaultH: 3, minW: 2, minH: 2,
        })
      } else if (p.tile_type === 'table') {
        out.push({
          id: `pinned:${p.id}`, kind: 'table',
          defaultW: 6, defaultH: 9, minW: 3, minH: 5,
        })
      } else {
        out.push({
          id: `pinned:${p.id}`, kind: 'chart',
          defaultW: 6, defaultH: 9, minW: 3, minH: 5,
        })
      }
    })
    // Insights card is always present — content is generated by the
    // configured insights skill (built-in `overview-insights` by default).
    out.push({
      id: 'insights', kind: 'insights',
      defaultW: 12, defaultH: 5, minW: 4, minH: 3,
    })
    textCards.forEach((tc) => out.push({
      id: `text:${tc.id}`, kind: 'text',
      defaultW: 6, defaultH: 4, minW: 3, minH: 2,
    }))
    return out
  }, [data, pinnedTiles, textCards])

  const visibleCards = useMemo(
    () => cards.filter((c) => !hidden.ids.includes(c.id)),
    [cards, hidden.ids],
  )

  // Saved positions take precedence; any card without a saved entry is
  // placed by the active template. Width is fixed by the template — the
  // grid disables east/west resize handles, and even if the user drags a
  // card, its width comes from whatever was saved (template- or user-set).
  const computedLayout: LayoutItem[] = useMemo(() => {
    const saved = new Map(layout.map((l) => [l.i, l]))
    let savedBottom = 0
    for (const l of layout) savedBottom = Math.max(savedBottom, l.y + l.h)

    const unsaved = visibleCards.filter((c) => !saved.has(c.id))
    const fresh = template.build(unsaved, savedBottom)
    const freshMap = new Map(fresh.map((l) => [l.i, l]))

    return visibleCards.map((c) => {
      const existing = saved.get(c.id) || freshMap.get(c.id)
      if (existing) return { ...existing, minH: c.minH }
      // Last-resort fallback (shouldn't be reachable): place full-width below.
      return { i: c.id, x: 0, y: savedBottom, w: 12, h: 4, minH: c.minH }
    })
  }, [visibleCards, layout, template])

  const onLayoutChange = (newLayout: LayoutItem[]) => {
    // RGL fires onLayoutChange both when the user drags/resizes AND on
    // mount / when the `layouts` prop changes (e.g. async data arrives).
    // The async fires used to lock in stale partial positions — if the
    // workspace endpoint resolved before pinned tiles, the insights card
    // was saved at y=0 and KPIs were forced beneath it on the next render.
    // Drag and resize only happen in edit mode (isDraggable / isResizable
    // are gated on editMode), so persisting only while editMode is true
    // keeps every fresh load template-driven and predictable.
    if (!editMode) return
    const known = new Map(newLayout.map((l) => [l.i, l]))
    const merged = layout.map((l) => known.get(l.i) || l)
    for (const l of newLayout) {
      if (!merged.find((m) => m.i === l.i)) merged.push(l)
    }
    setLayout(merged)
  }

  const addTextCard = () => {
    const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
    const newCardId = `text:${id}`
    const newW = 6
    const newH = 4
    setTextCards((tc) => [...tc, {
      id,
      body: '## New note\n\nClick this card to edit. Markdown supported.',
    }])
    // Drop the new card at the very top of the grid and shift every saved
    // position down by `newH`. Without this, the card lands at savedBottom
    // which is often well below the viewport — clicking Add Text felt like
    // nothing happened. Pushing existing cards down keeps the rest of the
    // layout intact, just translated.
    setLayout((prev) => {
      const shifted = prev.map((l) => ({ ...l, y: l.y + newH }))
      return [{ i: newCardId, x: 0, y: 0, w: newW, h: newH, minH: 2 }, ...shifted]
    })
    setEditMode(true)
    // Scroll to top so the new card is centered in the viewport.
    setTimeout(() => window.scrollTo({ top: 0, behavior: 'smooth' }), 50)
  }
  const updateTextCard = (id: string, body: string) => {
    setTextCards((tc) => tc.map((c) => (c.id === id ? { ...c, body } : c)))
  }
  const removeTextCard = (id: string) => {
    setTextCards((tc) => tc.filter((c) => c.id !== id))
  }

  const hideCard = (cardId: string) => {
    setHidden((h) => ({ ids: Array.from(new Set([...h.ids, cardId])) }))
  }
  const removeCard = (cardId: string) => {
    if (cardId.startsWith('text:')) {
      removeTextCard(cardId.slice(5))
    } else if (cardId.startsWith('pinned:')) {
      // Pinned cards have a real backend effect — keep the existing unpin flow.
      const plotId = cardId.slice(7)
      unpin(plotId)
    } else {
      hideCard(cardId)
    }
  }

  const resetLayout = () => {
    if (!confirm(`Reset the Overview layout for this function? Card positions, sizes, and any hidden cards will be restored using the "${template.name}" template. Text-box content is kept.`)) return
    setLayout([])
    setHidden({ ids: [] })
  }

  const chooseTemplate = (id: TemplateId) => {
    if (id === templateId) return
    const next = TEMPLATES.find((t) => t.id === id)
    if (!next) return
    if (layout.length > 0 || hidden.ids.length > 0) {
      const ok = confirm(
        `Switch to the "${next.name}" template?\n\n${next.description}\n\nThis resets card positions and brings any hidden cards back. Text-box content is kept.`
      )
      if (!ok) return
    }
    setTemplateId(id)
    setLayout([])
    setHidden({ ids: [] })
  }

  // ── Export / Import ───────────────────────────────────────────────────
  // Bundle is scoped to one function: positions reference card ids that
  // include function-specific KPI labels, chart ids, and pinned-tile ids,
  // so importing into a different function silently skips most cards.
  // We warn the user but still let them try.
  const fileInputRef = useRef<HTMLInputElement>(null)

  const exportLayout = () => {
    const bundle = {
      version: 1,
      kind: 'cma-overview-layout',
      function_id: functionId,
      function_name: data?.function_name || functionId,
      exported_at: new Date().toISOString(),
      layout,
      hidden,
      text_cards: textCards,
    }
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 10)
    a.href = url
    a.download = `cma-overview-${functionId}-${stamp}.json`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  const triggerImport = () => fileInputRef.current?.click()

  const saveAsFunctionDefault = async () => {
    if (!confirm(
      `Save the current Overview layout as the default for "${data?.function_name || functionId}"?\n\n`
      + `Anyone who has not customized their own Overview for this function will see this layout the next time they open the page.`
    )) return
    setSavingDefault(true)
    try {
      const body = {
        layout,
        hidden,
        text_cards: textCards,
        saved_at: new Date().toISOString(),
      }
      const r = await api.put<ServerDefaultBundle & { function_id: string }>(
        `/api/overview_layouts/${functionId}`, body,
      )
      setServerDefault({
        layout: r.data.layout || [],
        hidden: r.data.hidden || { ids: [] },
        text_cards: r.data.text_cards || [],
        saved_by: r.data.saved_by ?? null,
        saved_at: r.data.saved_at ?? null,
      })
    } catch (e: any) {
      alert(e?.response?.data?.detail || 'Could not save the function default.')
    } finally {
      setSavingDefault(false)
    }
  }

  const loadFunctionDefault = () => {
    if (!serverDefault) return
    if (!confirm(
      `Replace your current Overview with the saved function default? Your local positions, hidden cards, and text boxes will be overwritten.`
    )) return
    setLayout(serverDefault.layout)
    setHidden(serverDefault.hidden)
    setTextCards(serverDefault.text_cards)
  }

  const clearFunctionDefault = async () => {
    if (!serverDefault) return
    if (!confirm(
      `Clear the saved default layout for "${data?.function_name || functionId}"?\n\n`
      + `Future analysts will land on the auto-generated layout until someone saves a new default. This does not change your current view.`
    )) return
    try {
      await api.delete(`/api/overview_layouts/${functionId}`)
      setServerDefault(null)
    } catch (e: any) {
      alert(e?.response?.data?.detail || 'Could not clear the function default.')
    }
  }

  const handleImportFile = async (file: File) => {
    let bundle: any
    try {
      const text = await file.text()
      bundle = JSON.parse(text)
    } catch {
      alert('Could not read file — not valid JSON.')
      return
    }
    if (bundle?.kind !== 'cma-overview-layout' || !Array.isArray(bundle?.layout)) {
      alert('That file is not a CMA Overview layout export.')
      return
    }
    if (bundle.function_id && bundle.function_id !== functionId) {
      const ok = confirm(
        `This layout was exported from "${bundle.function_name || bundle.function_id}", but you are on "${data?.function_name || functionId}".\n\n`
        + `Card IDs are function-specific, so most cards will not match and will be ignored. Text boxes will still come through.\n\n`
        + `Import anyway?`
      )
      if (!ok) return
    }
    const hasExisting = layout.length > 0 || textCards.length > 0 || hidden.ids.length > 0
    if (hasExisting) {
      const ok = confirm(
        'Importing replaces the current layout, hidden cards, and text boxes for this function. Continue?'
      )
      if (!ok) return
    }
    setLayout(Array.isArray(bundle.layout) ? bundle.layout : [])
    setHidden(
      bundle.hidden && Array.isArray(bundle.hidden.ids)
        ? { ids: bundle.hidden.ids.filter((x: any) => typeof x === 'string') }
        : { ids: [] }
    )
    setTextCards(
      Array.isArray(bundle.text_cards)
        ? bundle.text_cards
            .filter((t: any) => t && typeof t.id === 'string' && typeof t.body === 'string')
            .map((t: any) => ({ id: t.id, body: t.body }))
        : []
    )
  }

  if (error) {
    return (
      <div className="panel" style={{ background: 'var(--error-bg)', borderColor: 'var(--error)' }}>
        <div style={{ color: 'var(--error)', fontWeight: 600 }}>{error}</div>
      </div>
    )
  }
  if (!data) return <div style={{ color: 'var(--text-muted)' }}>Loading workspace…</div>

  const hiddenCount = hidden.ids.length

  return (
    <div>
      {/* Hidden file input for import */}
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) handleImportFile(f)
          if (fileInputRef.current) fileInputRef.current.value = ''
        }}
      />

      {/* Toolbar */}
      <div className="flex justify-end mb-3 gap-2 flex-wrap">
        <button
          onClick={addTextCard}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            color: 'var(--text-secondary)',
          }}
          title="Add a markdown commentary card"
        >
          <Type size={12} /> Add Text
        </button>
        <button
          onClick={exportLayout}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            color: 'var(--text-secondary)',
          }}
          title="Download this Overview's layout (positions, hidden cards, text boxes) as a JSON file"
        >
          <Download size={12} /> Export
        </button>
        <button
          onClick={triggerImport}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            color: 'var(--text-secondary)',
          }}
          title="Load an Overview layout JSON exported from this or another machine"
        >
          <Upload size={12} /> Import
        </button>
        {editMode && (
          <>
            <div
              className="flex items-center rounded-lg p-0.5 gap-0.5"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border)',
              }}
            >
              {TEMPLATES.map((t) => {
                const active = templateId === t.id
                const Icon = t.icon
                return (
                  <button
                    key={t.id}
                    onClick={() => chooseTemplate(t.id)}
                    className="px-2 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 transition-colors"
                    style={{
                      background: active ? 'var(--bg-card)' : 'transparent',
                      color: active ? 'var(--accent)' : 'var(--text-secondary)',
                      boxShadow: active ? '0 1px 4px rgba(0,0,0,0.05)' : 'none',
                    }}
                    title={t.description}
                  >
                    <Icon size={11} /> {t.name}
                  </button>
                )
              })}
            </div>
            <button
              onClick={resetLayout}
              className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
              style={{
                background: 'var(--bg-card)',
                border: '1px solid var(--border)',
                color: 'var(--text-secondary)',
              }}
              title={`Restore the "${template.name}" template and bring back hidden cards`}
            >
              <RotateCcw size={12} /> Reset Layout
            </button>
            {serverDefault && (
              <button
                onClick={loadFunctionDefault}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
                style={{
                  background: 'var(--bg-card)',
                  border: '1px solid var(--border)',
                  color: 'var(--text-secondary)',
                }}
                title={`Replace your view with the function default${
                  serverDefault.saved_by ? ` (last saved by ${serverDefault.saved_by})` : ''
                }`}
              >
                <Download size={12} /> Load Function Default
              </button>
            )}
            <button
              onClick={saveAsFunctionDefault}
              disabled={savingDefault}
              className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors disabled:opacity-50"
              style={{
                background: 'var(--accent-light)',
                border: '1px solid var(--accent)',
                color: 'var(--accent)',
              }}
              title="Make this layout the default for everyone who has not customized their own Overview for this function"
            >
              <Pin size={12} /> {savingDefault ? 'Saving…' : 'Save as Function Default'}
            </button>
            {serverDefault && (
              <button
                onClick={clearFunctionDefault}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-colors"
                style={{
                  background: 'var(--bg-card)',
                  border: '1px solid var(--error)',
                  color: 'var(--error)',
                }}
                title="Remove the saved function default; new analysts will see the auto-generated layout"
              >
                <X size={12} /> Clear Function Default
              </button>
            )}
          </>
        )}
        <button
          onClick={() => setEditMode((m) => !m)}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-all"
          style={{
            background: editMode ? 'var(--accent)' : 'var(--bg-card)',
            border: `1px solid ${editMode ? 'var(--accent)' : 'var(--border)'}`,
            color: editMode ? '#fff' : 'var(--text-secondary)',
          }}
          title={editMode ? 'Exit layout edit mode' : 'Drag, resize, and remove cards'}
        >
          {editMode ? <Check size={12} /> : <Edit3 size={12} />}
          {editMode ? 'Done' : 'Edit Layout'}
          {!editMode && hiddenCount > 0 && (
            <span
              className="rounded-full px-1.5 py-0.5"
              style={{
                fontSize: 9, fontWeight: 700, lineHeight: 1,
                background: 'var(--accent)', color: '#fff',
              }}
            >
              {hiddenCount}
            </span>
          )}
        </button>
      </div>

      {visibleCards.length === 0 && (
        <div className="panel text-center" style={{ padding: '40px 20px', borderStyle: 'dashed' }}>
          <Edit3 size={20} style={{ color: 'var(--text-muted)', margin: '0 auto 8px' }} />
          <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
            All cards hidden
          </div>
          <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
            Click <strong>Edit Layout</strong> → <strong>Reset Layout</strong> to bring them back, or pin tiles from the Reporting tab.
          </div>
        </div>
      )}

      {visibleCards.length > 0 && (
        <ResponsiveGridLayout
          className="layout"
          layouts={{ lg: computedLayout, md: computedLayout, sm: computedLayout, xs: computedLayout, xxs: computedLayout }}
          breakpoints={BREAKPOINTS}
          cols={COLS}
          rowHeight={ROW_HEIGHT}
          margin={[12, 12]}
          containerPadding={[0, 0]}
          compactType="vertical"
          // Width is fixed by the active template — only the south-edge
          // handle is shown so users can grow/shrink height but not width.
          resizeHandles={['s']}
          isDraggable={editMode}
          isResizable={editMode}
          draggableHandle={DRAG_HANDLE}
          onLayoutChange={onLayoutChange as any}
          // Resize visibly via the bottom-right handle the lib draws by default.
          // We hide the handle in static mode via the editMode flag.
        >
          {visibleCards.map((c) => (
            <div key={c.id} data-card-kind={c.kind}>
              <CardShell
                kind={c.kind}
                editMode={editMode}
                onRemove={() => removeCard(c.id)}
              >
                {renderCardBody(c, {
                  functionId,
                  data,
                  pinnedTiles,
                  previews,
                  textCards,
                  setEntity,
                  onAskAgent,
                  editMode,
                  updateTextCard,
                  insightsSkillId,
                  setInsightsSkillId,
                })}
              </CardShell>
            </div>
          ))}
        </ResponsiveGridLayout>
      )}

      {/* Tiny bit of CSS so RGL transitions feel native and the drag handle
          doesn't fight with the underlying chart pointer events. */}
      <style>{`
        .layout {
          position: relative;
        }
        .react-grid-item.react-grid-placeholder {
          background: var(--accent) !important;
          opacity: 0.18 !important;
          border-radius: 12px !important;
        }
        .react-grid-item > .react-resizable-handle {
          opacity: ${editMode ? 0.55 : 0};
          transition: opacity 120ms;
        }
        .react-grid-item:hover > .react-resizable-handle {
          opacity: ${editMode ? 1 : 0};
        }
        .cma-card-drag-handle {
          cursor: ${editMode ? 'grab' : 'default'};
        }
        .cma-card-drag-handle:active {
          cursor: ${editMode ? 'grabbing' : 'default'};
        }
        ${editMode ? `
          .react-grid-item {
            outline: 1px dashed var(--border);
            outline-offset: -2px;
            border-radius: 12px;
          }
          .react-grid-item.react-draggable-dragging {
            outline: 2px solid var(--accent);
            box-shadow: 0 12px 32px rgba(0,0,0,0.18);
          }
        ` : ''}
      `}</style>
    </div>
  )
}

// ── Card shell — header strip is the drag handle in edit mode ──────────────
function CardShell({
  kind, editMode, onRemove, children,
}: {
  kind: CardKind
  editMode: boolean
  onRemove: () => void
  children: React.ReactNode
}) {
  return (
    <div
      className="panel h-full flex flex-col overflow-hidden"
      style={{ padding: 0 }}
    >
      {/* Drag strip — only takes vertical space in edit mode so static view
          stays clean. */}
      {editMode && (
        <div
          className={`${DRAG_HANDLE.slice(1)} flex items-center justify-between px-2`}
          style={{
            height: 22,
            background: 'var(--bg-elevated)',
            borderBottom: '1px solid var(--border-subtle)',
            color: 'var(--text-muted)',
            flexShrink: 0,
          }}
        >
          <div className="flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-widest">
            <Move size={10} />
            {kind}
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); onRemove() }}
            onMouseDown={(e) => e.stopPropagation()}
            className="p-0.5 rounded hover:bg-red-50"
            title={kind === 'pinned' ? 'Unpin' : kind === 'text' ? 'Delete' : 'Hide'}
            style={{ color: 'var(--error)' }}
          >
            <X size={11} />
          </button>
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto" style={{ padding: 14 }}>
        {children}
      </div>
    </div>
  )
}

// ── Card body renderer ─────────────────────────────────────────────────────
function renderCardBody(
  c: CardMeta,
  ctx: {
    functionId: string
    data: WorkspaceData
    pinnedTiles: PlotConfig[]
    previews: Record<string, PreviewBundle>
    textCards: TextCard[]
    setEntity: (kind: any, id: string | null) => void
    onAskAgent: (q: string) => void
    editMode: boolean
    updateTextCard: (id: string, body: string) => void
    insightsSkillId: string
    setInsightsSkillId: (id: string) => void
  },
): React.ReactNode {
  // KPI / chart / table cards all wrap a pinned tile. The card.id encodes
  // the plot id as `pinned:<id>`; we look up the tile + preview and render
  // by tile_type.
  if (c.kind === 'kpi' || c.kind === 'chart' || c.kind === 'table') {
    const id = c.id.slice(7)  // strip "pinned:"
    const tile = ctx.pinnedTiles.find((p) => p.id === id)
    if (!tile) return null
    const preview = ctx.previews[tile.id]

    if (tile.tile_type === 'kpi') {
      const k = preview?.kpi
      return (
        <button
          onClick={(e) => {
            // Don't open the chat while the user is rearranging cards —
            // the click was almost certainly meant for the drag/select.
            if (ctx.editMode) {
              e.preventDefault()
              return
            }
            ctx.setEntity('tile', tile.id)
            ctx.onAskAgent(`Explain the "${tile.name}" KPI.`)
          }}
          onMouseDown={(e) => { if (ctx.editMode) e.stopPropagation() }}
          className="text-left w-full h-full flex flex-col justify-center"
          style={{ cursor: ctx.editMode ? 'default' : 'pointer' }}
        >
          <div className="metric-label">{tile.name}</div>
          <div
            className="font-mono mt-1"
            style={{
              fontSize: 28, fontWeight: 700,
              color: 'var(--text-primary)',
              letterSpacing: '-0.02em', lineHeight: 1.1,
            }}
          >
            {k?.display ?? '—'}
          </div>
          {k?.sublabel && (
            <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
              {k.sublabel}
            </div>
          )}
        </button>
      )
    }

    const isTable = tile.tile_type === 'table'
    return (
      <div className="h-full flex flex-col">
        <div className="flex items-start justify-between mb-2 gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <div
              className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
              style={{
                background: isTable ? 'rgba(15,118,110,0.10)' : 'var(--accent-light)',
                color: isTable ? '#0F766E' : 'var(--accent)',
              }}
            >
              {isTable ? <TableIcon size={14} /> : <BarChart3 size={14} />}
            </div>
            <div className="min-w-0">
              <div className="font-display text-base font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
                {tile.name}
              </div>
              <div className="text-[11px] truncate" style={{ color: 'var(--text-muted)' }}>
                {isTable ? 'Table' : tile.chart_type}
                {preview?.source === 'live' && (
                  <span className="pill ml-2" style={{ fontSize: 9, background: 'var(--success-bg)', color: 'var(--success)', borderColor: 'transparent' }}>
                    LIVE
                  </span>
                )}
              </div>
            </div>
          </div>
          {!ctx.editMode && (
            <button
              onClick={() => ctx.onAskAgent(`Explain the pinned ${isTable ? 'table' : 'chart'} "${tile.name}".`)}
              className="text-xs px-2 py-1 rounded-md shrink-0"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border)',
                color: 'var(--text-secondary)',
              }}
              title="Ask agent"
            >
              Explain
            </button>
          )}
        </div>
        <div className="flex-1 min-h-0">
          {!preview ? (
            <div className="flex items-center justify-center text-xs h-full" style={{ color: 'var(--text-muted)' }}>
              Loading preview…
            </div>
          ) : isTable ? (
            <InteractiveTable
              rows={preview.rows}
              columns={(tile.table_columns && tile.table_columns.length > 0)
                ? tile.table_columns
                : preview.columns}
              defaultSort={tile.table_default_sort || null}
              defaultSortDesc={!!tile.table_default_sort_desc}
              height={220}
            />
          ) : (
            <Chart spec={preview.spec} brushable />
          )}
        </div>
      </div>
    )
  }

  if (c.kind === 'insights') {
    return (
      <InsightsCard
        functionId={ctx.functionId}
        skillId={ctx.insightsSkillId}
        onSkillChange={ctx.setInsightsSkillId}
        onAskAgent={ctx.onAskAgent}
        editMode={ctx.editMode}
      />
    )
  }

  if (c.kind === 'text') {
    const id = c.id.slice(5)  // strip "text:"
    const tc = ctx.textCards.find((x) => x.id === id)
    if (!tc) return null
    return (
      <TextCardBody
        body={tc.body}
        editMode={ctx.editMode}
        onChange={(body) => ctx.updateTextCard(id, body)}
      />
    )
  }

  return null
}

// ── Insights card — agent-generated brief over the function's pinned tiles ─
interface InsightSkill {
  id: string
  name: string
  description: string
  icon: string
  color: string
  source: string
  pack_id: string | null
}

function InsightsCard({
  functionId, skillId, onSkillChange, onAskAgent, editMode,
}: {
  functionId: string
  skillId: string
  onSkillChange: (id: string) => void
  onAskAgent: (q: string) => void
  editMode: boolean
}) {
  const [skills, setSkills] = useState<InsightSkill[]>([])
  const [markdown, setMarkdown] = useState('')
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const pickerRef = useRef<HTMLDivElement>(null)

  // Load available skills for the picker
  useEffect(() => {
    api.get<InsightSkill[]>(`/api/workspace/${functionId}/insights/skills`)
      .then((r) => setSkills(r.data || []))
      .catch(() => {})
  }, [functionId])

  const generate = async () => {
    setLoading(true); setError(null)
    try {
      const r = await api.post<{ markdown: string; generated_at: string; skill_name: string }>(
        `/api/workspace/${functionId}/insights`,
        { skill_id: skillId },
      )
      setMarkdown(r.data.markdown || '')
      setGeneratedAt(r.data.generated_at || null)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : (e?.message || 'Failed to generate insights'))
      setMarkdown('')
    } finally {
      setLoading(false)
    }
  }

  // Auto-generate on mount + when function or skill changes
  useEffect(() => { generate() /* eslint-disable-next-line */ }, [functionId, skillId])

  // Close picker on outside click
  useEffect(() => {
    if (!pickerOpen) return
    const handler = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false)
    }
    const t = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => { clearTimeout(t); document.removeEventListener('mousedown', handler) }
  }, [pickerOpen])

  const activeSkill = skills.find((s) => s.id === skillId)
  const activeName = activeSkill?.name || skillId.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  const activeColor = activeSkill?.color || 'var(--warning)'

  const generatedRel = (() => {
    if (!generatedAt) return null
    const dt = new Date(generatedAt)
    const secs = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000))
    if (secs < 60) return `${secs}s ago`
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
    return dt.toLocaleString()
  })()

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 mb-2">
        <Lightbulb size={14} style={{ color: activeColor }} />
        <div className="font-display text-base font-semibold" style={{ color: 'var(--text-primary)' }}>
          Today's Insights
        </div>
        <div className="ml-auto flex items-center gap-1 relative" ref={pickerRef}>
          {generatedAt && !loading && (
            <span
              className="text-[10px] font-mono"
              style={{ color: 'var(--text-muted)' }}
              title={`Generated ${new Date(generatedAt).toLocaleString()}`}
            >
              {generatedRel}
            </span>
          )}
          <button
            onClick={() => setPickerOpen((o) => !o)}
            onMouseDown={(e) => e.stopPropagation()}
            className="px-2 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 transition-colors"
            style={{
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border)',
              color: activeColor,
            }}
            title="Choose which agent generates these insights"
          >
            <Sparkles size={11} /> {activeName}
          </button>
          <button
            onClick={generate}
            onMouseDown={(e) => e.stopPropagation()}
            disabled={loading}
            className="p-1 rounded-md transition-colors disabled:opacity-50"
            style={{ color: 'var(--text-muted)' }}
            title="Refresh insights"
          >
            <RotateCcw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
          {pickerOpen && (
            <div
              className="absolute right-0 top-full mt-1 panel z-30"
              style={{
                width: 280, padding: 0,
                boxShadow: '0 12px 32px rgba(0,0,0,0.12)',
                maxHeight: 360, overflowY: 'auto',
              }}
            >
              <div
                className="px-3 py-2 text-[10px] font-semibold uppercase tracking-widest"
                style={{
                  color: 'var(--text-secondary)',
                  borderBottom: '1px solid var(--border-subtle)',
                  background: 'var(--bg-elevated)',
                  position: 'sticky', top: 0,
                }}
              >
                Insights agent
              </div>
              {skills.length === 0 ? (
                <div className="px-3 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                  No skills loaded.
                </div>
              ) : skills.map((s) => {
                const active = s.id === skillId
                return (
                  <button
                    key={s.id}
                    onClick={() => {
                      onSkillChange(s.id)
                      setPickerOpen(false)
                    }}
                    onMouseDown={(e) => e.stopPropagation()}
                    className="w-full text-left px-3 py-2 transition-colors"
                    style={{
                      background: active ? 'var(--accent-light)' : 'transparent',
                      borderBottom: '1px solid var(--border-subtle)',
                    }}
                    onMouseEnter={(e) => {
                      if (!active) (e.currentTarget as HTMLElement).style.background = 'var(--bg-elevated)'
                    }}
                    onMouseLeave={(e) => {
                      if (!active) (e.currentTarget as HTMLElement).style.background = 'transparent'
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <div
                        className="w-2 h-2 rounded-full shrink-0"
                        style={{ background: s.color || 'var(--accent)' }}
                      />
                      <div
                        className="text-sm font-semibold truncate"
                        style={{ color: active ? 'var(--accent)' : 'var(--text-primary)' }}
                      >
                        {s.name}
                      </div>
                      <div
                        className="ml-auto text-[9px] font-mono px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          background: 'var(--bg-elevated)',
                          color: 'var(--text-muted)',
                          textTransform: 'uppercase',
                          letterSpacing: '0.05em',
                        }}
                      >
                        {s.source}
                      </div>
                    </div>
                    <div className="text-[11px] mt-0.5 line-clamp-2" style={{ color: 'var(--text-muted)' }}>
                      {s.description}
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-auto">
        {loading && !markdown ? (
          <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Generating insights from <strong>{activeName}</strong>…
          </div>
        ) : error ? (
          <div
            className="text-xs px-3 py-2 rounded-md"
            style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
          >
            {error}
            <button
              onClick={generate}
              className="ml-2 underline font-semibold"
              style={{ color: 'var(--error)' }}
            >
              retry
            </button>
          </div>
        ) : markdown ? (
          <div
            className="markdown-body"
            style={{
              fontSize: 12.5, lineHeight: 1.6,
              color: 'var(--text-secondary)',
              opacity: loading ? 0.5 : 1,
            }}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={insightsMd(editMode ? null : onAskAgent)}>
              {markdown}
            </ReactMarkdown>
          </div>
        ) : (
          <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
            No insights yet. Click the refresh icon to generate, or pick a different agent.
          </div>
        )}
      </div>
    </div>
  )
}

const insightsMd = (onAskAgent: ((q: string) => void) | null): any => ({
  ul: (p: any) => <ul style={{ paddingLeft: 18, marginBottom: 6 }} {...p} />,
  ol: (p: any) => <ol style={{ paddingLeft: 18, marginBottom: 6 }} {...p} />,
  li: (p: any) => {
    // No "discuss" affordance when the user is rearranging the dashboard.
    if (!onAskAgent) {
      return <li style={{ marginBottom: 4 }} {...p} />
    }
    const text = String(
      Array.isArray(p.children) ? p.children.map((c: any) =>
        typeof c === 'string' ? c : (c?.props?.children ?? '')
      ).join(' ') : (p.children || '')
    )
    return (
      <li
        className="group flex gap-2 items-start"
        style={{ marginBottom: 4 }}
        {...p}
      >
        <span style={{ flex: 1 }}>{p.children}</span>
        <button
          onClick={() => onAskAgent(`Tell me more about: ${text.slice(0, 200)}`)}
          className="opacity-0 group-hover:opacity-100 text-[10px] shrink-0 transition-opacity"
          style={{ color: 'var(--accent)', fontWeight: 600 }}
        >
          discuss →
        </button>
      </li>
    )
  },
  p: (p: any) => <p style={{ marginBottom: 6 }} {...p} />,
  strong: (p: any) => {
    const content = String(p.children || '')
    if (/(WATCH|BREACH|NEAR LIMIT|ALERT|RISK)/i.test(content)) {
      return (
        <strong style={{
          color: 'var(--error)', fontWeight: 700,
          background: 'rgba(220,38,38,0.08)',
          padding: '0 4px', borderRadius: 3,
        }} {...p} />
      )
    }
    return <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }} {...p} />
  },
  code: (p: any) => (
    <code
      style={{
        color: 'var(--accent)',
        background: 'var(--accent-light)',
        padding: '1px 4px', borderRadius: 3,
        fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
      }}
      {...p}
    />
  ),
  em: (p: any) => <em style={{ color: 'var(--text-muted)' }} {...p} />,
})

function TextCardBody({
  body, editMode, onChange,
}: {
  body: string
  editMode: boolean
  onChange: (body: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)

  // When edit mode turns off, exit any in-place editor.
  useEffect(() => { if (!editMode) setEditing(false) }, [editMode])

  if (editing && editMode) {
    return (
      <textarea
        ref={taRef}
        defaultValue={body}
        autoFocus
        onMouseDown={(e) => e.stopPropagation()}
        onBlur={(e) => {
          onChange(e.currentTarget.value)
          setEditing(false)
        }}
        className="w-full h-full resize-none font-mono text-xs"
        style={{
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: 8,
          padding: 10,
          color: 'var(--text-primary)',
          outline: 'none',
        }}
        placeholder="Markdown supported — e.g. ## Heading, **bold**, - bullet"
      />
    )
  }

  return (
    <div
      onClick={() => editMode && setEditing(true)}
      onMouseDown={(e) => editMode && e.stopPropagation()}
      style={{
        cursor: editMode ? 'text' : 'default',
        fontSize: 13,
        lineHeight: 1.6,
        color: 'var(--text-secondary)',
        height: '100%',
        overflow: 'auto',
      }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={textCardMd}>
        {body || '_(empty — toggle Edit Layout and click to write)_'}
      </ReactMarkdown>
    </div>
  )
}

const textCardMd: any = {
  h1: (p: any) => <h1 style={{ fontSize: 16, fontWeight: 700, marginBottom: 6, color: 'var(--text-primary)' }} {...p} />,
  h2: (p: any) => <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4, color: 'var(--text-primary)' }} {...p} />,
  h3: (p: any) => <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 3, color: 'var(--accent)' }} {...p} />,
  p: (p: any) => <p style={{ marginBottom: 6 }} {...p} />,
  ul: (p: any) => <ul style={{ paddingLeft: 18, marginBottom: 6 }} {...p} />,
  ol: (p: any) => <ol style={{ paddingLeft: 18, marginBottom: 6 }} {...p} />,
  li: (p: any) => <li style={{ marginBottom: 2 }} {...p} />,
  strong: (p: any) => <strong style={{ color: 'var(--text-primary)' }} {...p} />,
  code: (p: any) => (
    <code
      style={{
        color: 'var(--accent)',
        background: 'var(--accent-light)',
        padding: '1px 4px', borderRadius: 3,
        fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
      }}
      {...p}
    />
  ),
}

function cellColor(cell: any): string {
  const s = String(cell || '').toUpperCase()
  if (s.includes('OK') || s.includes('PASS')) return 'var(--success)'
  if (s.includes('WATCH') || s.includes('WARN')) return 'var(--warning)'
  if (s.includes('BREACH') || s.includes('FAIL')) return 'var(--error)'
  return 'var(--text-primary)'
}
