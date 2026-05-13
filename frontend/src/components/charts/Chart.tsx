import {
  Area, AreaChart, Bar, BarChart, Brush, CartesianGrid, Cell, Label, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { ChartSpec, PlotStyle } from '@/types'
import { smartTickFormat } from '@/lib/utils'

const DEFAULT_PALETTE = ['#004977', '#059669', '#D97706', '#DC2626', '#7C3AED', '#0891B2', '#00B8D9', '#A78BFA']

const tooltipStyle = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  fontSize: 11,
  fontFamily: 'JetBrains Mono, monospace',
  color: 'var(--text-primary)',
}

interface Props {
  spec: ChartSpec
  height?: number
  /** Show a brush for x-axis zoom on line / area / bar charts. Off by default. */
  brushable?: boolean
}

/** Resolve every style-affecting field with sensible fallbacks so the Chart
 *  is correct whether `spec.style` is missing, partial, or fully populated.
 *  This is the *only* place style is read — keep new style fields here. */
function resolveStyle(spec: ChartSpec) {
  const s: PlotStyle = spec.style || {}
  const palette = (s.palette && s.palette.length > 0) ? s.palette : DEFAULT_PALETTE
  const fontSize = (s.font_size && s.font_size > 0) ? s.font_size : 11
  const legendPos = s.legend_position && s.legend_position !== 'none'
    ? s.legend_position
    : (s.legend_position === 'none' ? null : 'auto') // 'auto' = let chart decide
  const axisTick = { fontSize, fill: 'var(--text-muted)' }
  return {
    palette,
    fontSize,
    legendPos,                    // 'top' | 'bottom' | 'left' | 'right' | 'auto' | null
    title: s.title || null,
    xAxisLabel: s.x_axis_label || null,
    yAxisLabel: s.y_axis_label || null,
    axisTick,
  }
}

function maybeLegend(legendPos: ReturnType<typeof resolveStyle>['legendPos'], multiSeries: boolean) {
  // 'auto' → show only when there's >1 series; null → never; named position → always.
  if (legendPos === null) return null
  if (legendPos === 'auto') {
    return multiSeries ? <Legend wrapperStyle={{ fontSize: 11 }} /> : null
  }
  return (
    <Legend
      wrapperStyle={{ fontSize: 11 }}
      verticalAlign={legendPos === 'top' || legendPos === 'bottom' ? legendPos : 'middle'}
      align={legendPos === 'left' || legendPos === 'right' ? legendPos : 'center'}
      layout={legendPos === 'left' || legendPos === 'right' ? 'vertical' : 'horizontal'}
    />
  )
}

/** Apply spec.style.sort_field / sort_desc before rendering — so the
 *  plot-tuner's `set_sort` shows up immediately without a refetch. */
function sortData(spec: ChartSpec): Record<string, any>[] {
  const s = spec.style
  if (!s?.sort_field) return spec.data
  const f = s.sort_field
  const desc = !!s.sort_desc
  return [...spec.data].sort((a, b) => {
    const av = a[f]
    const bv = b[f]
    if (av == null && bv == null) return 0
    if (av == null) return desc ? 1 : -1
    if (bv == null) return desc ? -1 : 1
    if (typeof av === 'number' && typeof bv === 'number') {
      return desc ? bv - av : av - bv
    }
    return desc ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv))
  })
}

export default function Chart({ spec, height = 240, brushable = false }: Props) {
  const { type, x_key, y_keys } = spec
  const data = sortData(spec)
  const showBrush = brushable && data.length > 12 && (type === 'line' || type === 'area' || type === 'bar' || type === 'stacked_bar')
  const style = resolveStyle(spec)

  if (type === 'line') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: style.xAxisLabel ? 18 : 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
          <XAxis dataKey={x_key} tick={style.axisTick}>
            {style.xAxisLabel && <Label value={style.xAxisLabel} position="insideBottom" offset={-6} fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </XAxis>
          <YAxis tick={style.axisTick} tickFormatter={smartTickFormat}>
            {style.yAxisLabel && <Label value={style.yAxisLabel} angle={-90} position="insideLeft" fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </YAxis>
          <Tooltip contentStyle={tooltipStyle} />
          {maybeLegend(style.legendPos, y_keys.length > 1)}
          {y_keys.map((k, i) => (
            <Line key={k} type="monotone" dataKey={k} stroke={style.palette[i % style.palette.length]} strokeWidth={2} dot={{ r: 2.5 }} activeDot={{ r: 5 }} />
          ))}
          {showBrush && (
            <Brush dataKey={x_key} height={20} stroke="var(--accent)" travellerWidth={6} />
          )}
        </LineChart>
      </ResponsiveContainer>
    )
  }

  if (type === 'area') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: style.xAxisLabel ? 18 : 0 }}>
          <defs>
            {y_keys.map((k, i) => (
              <linearGradient key={k} id={`grad-${spec.id}-${k}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={style.palette[i % style.palette.length]} stopOpacity={0.45} />
                <stop offset="100%" stopColor={style.palette[i % style.palette.length]} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
          <XAxis dataKey={x_key} tick={style.axisTick}>
            {style.xAxisLabel && <Label value={style.xAxisLabel} position="insideBottom" offset={-6} fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </XAxis>
          <YAxis tick={style.axisTick} tickFormatter={smartTickFormat}>
            {style.yAxisLabel && <Label value={style.yAxisLabel} angle={-90} position="insideLeft" fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </YAxis>
          <Tooltip contentStyle={tooltipStyle} />
          {maybeLegend(style.legendPos, y_keys.length > 1)}
          {y_keys.map((k, i) => (
            <Area key={k} type="monotone" dataKey={k} stroke={style.palette[i % style.palette.length]} fill={`url(#grad-${spec.id}-${k})`} strokeWidth={2} />
          ))}
          {showBrush && (
            <Brush dataKey={x_key} height={20} stroke="var(--accent)" travellerWidth={6} />
          )}
        </AreaChart>
      </ResponsiveContainer>
    )
  }

  if (type === 'bar' || type === 'stacked_bar') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: style.xAxisLabel ? 18 : 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
          <XAxis dataKey={x_key} tick={style.axisTick}>
            {style.xAxisLabel && <Label value={style.xAxisLabel} position="insideBottom" offset={-6} fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </XAxis>
          <YAxis tick={style.axisTick} tickFormatter={smartTickFormat}>
            {style.yAxisLabel && <Label value={style.yAxisLabel} angle={-90} position="insideLeft" fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </YAxis>
          <Tooltip contentStyle={tooltipStyle} />
          {maybeLegend(style.legendPos, y_keys.length > 1)}
          {y_keys.map((k, i) => (
            <Bar
              key={k}
              dataKey={k}
              fill={style.palette[i % style.palette.length]}
              stackId={type === 'stacked_bar' ? 'a' : undefined}
              radius={[4, 4, 0, 0]}
            />
          ))}
          {showBrush && (
            <Brush dataKey={x_key} height={20} stroke="var(--accent)" travellerWidth={6} />
          )}
        </BarChart>
      </ResponsiveContainer>
    )
  }

  if (type === 'pie') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <PieChart>
          <Pie
            data={data}
            dataKey={y_keys[0]}
            nameKey={x_key}
            cx="50%"
            cy="50%"
            outerRadius={Math.min(80, height / 2 - 20)}
            innerRadius={Math.min(40, height / 4)}
            paddingAngle={2}
          >
            {data.map((_, i) => (
              <Cell key={i} fill={style.palette[i % style.palette.length]} />
            ))}
          </Pie>
          <Tooltip contentStyle={tooltipStyle} />
          {maybeLegend(style.legendPos, true)}
        </PieChart>
      </ResponsiveContainer>
    )
  }

  if (type === 'scatter') {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <ScatterChart margin={{ top: 8, right: 12, left: -8, bottom: style.xAxisLabel ? 18 : 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
          <XAxis dataKey={x_key} tick={style.axisTick} tickFormatter={smartTickFormat}>
            {style.xAxisLabel && <Label value={style.xAxisLabel} position="insideBottom" offset={-6} fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </XAxis>
          <YAxis dataKey={y_keys[0]} tick={style.axisTick} tickFormatter={smartTickFormat}>
            {style.yAxisLabel && <Label value={style.yAxisLabel} angle={-90} position="insideLeft" fill="var(--text-secondary)" fontSize={style.fontSize} />}
          </YAxis>
          <Tooltip contentStyle={tooltipStyle} />
          <Scatter data={data} fill={style.palette[0]} />
        </ScatterChart>
      </ResponsiveContainer>
    )
  }

  return null
}
