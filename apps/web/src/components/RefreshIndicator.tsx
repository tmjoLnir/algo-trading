/**
 * Says how old the data is and when it next refreshes.
 *
 * With a 5-minute cadence the user cannot tell fresh data from four-minute-old
 * data by looking. Making the age explicit — and visibly warning once it
 * exceeds the interval — is the difference between a dashboard that is trusted
 * correctly and one that is trusted blindly.
 */

interface Props {
  ageSeconds: number | null
  isFetching: boolean
  onRefresh: () => void
  intervalSeconds: number
  stale?: boolean
}

export default function RefreshIndicator({
  ageSeconds,
  isFetching,
  onRefresh,
  intervalSeconds,
  stale,
}: Props) {
  const overdue = ageSeconds !== null && ageSeconds > intervalSeconds * 1.5
  const color = stale || overdue ? 'text-amber-400' : 'text-slate-400'

  return (
    <div className="flex items-center gap-3 text-xs">
      <span className={color}>
        {isFetching
          ? 'Refreshing…'
          : ageSeconds === null
            ? '—'
            : `Updated ${ageSeconds}s ago${overdue ? ' — data may be stale' : ''}`}
      </span>
      <span className="text-slate-600">auto-refresh every {intervalSeconds / 60} min</span>
      <button
        onClick={onRefresh}
        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800"
      >
        Refresh now
      </button>
    </div>
  )
}
