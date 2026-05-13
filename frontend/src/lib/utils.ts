import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Choose a reasonable number of decimals for a chart axis tick based on the
 * value's magnitude, so axes don't render things like "0.4621345" alongside
 * "0.5" when no explicit number_format is configured.
 *
 *   |v| ≥ 1e9      → compact "B" with 1 decimal
 *   |v| ≥ 1e6      → compact "M" with 1 decimal
 *   |v| ≥ 1e4      → compact "K" with 0 decimals (10K, 250K, …)
 *   |v| ≥ 100      → 0 decimals, comma-grouped
 *   |v| ≥ 10       → 1 decimal
 *   |v| ≥ 1        → 2 decimals
 *   |v| ≥ 0.01     → 3 decimals
 *   else (non-0)   → 2 significant figures (e.g. 0.0012)
 *
 * Trailing zeros are preserved on purpose: keeping every tick on the same axis
 * at the same decimal count is what makes the axis look consistent.
 */
export function smartTickFormat(v: any): string {
  if (v == null) return ''
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v)
  if (v === 0) return '0'
  const abs = Math.abs(v)
  if (abs >= 1e9) return (v / 1e9).toFixed(1) + 'B'
  if (abs >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (abs >= 1e4) return (v / 1e3).toFixed(0) + 'K'
  if (abs >= 100) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (abs >= 10)  return v.toFixed(1)
  if (abs >= 1)   return v.toFixed(2)
  if (abs >= 0.01) return v.toFixed(3)
  return v.toPrecision(2)
}
