/**
 * The audit trail, on a screen.
 *
 * The roadmap item is "audit log surfaced in UI", and the second half is the
 * point: `audit_log` has been in the schema since the first migration with
 * nothing writing it and nowhere to read it. A record nobody can see is a record
 * nobody checks, and one nobody checks is not doing the job it exists for.
 *
 * What is on the screen today is authentication and refusals — signing in and
 * out, failed attempts, rate-limit lockouts, and writes refused to a read-only
 * session. That is not the whole of what the table's docstring anticipates; the
 * order-flow and kill-switch events land with their handlers, every one of which
 * is still a stub (ADR 0010).
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, apiGet } from '@/api/client'
import type { AuditEntryView, AuditPage } from '@/api/types'

/**
 * Actions worth noticing, and how loudly.
 *
 * Colour is never the only signal (docs/DASHBOARD.md) — every row states its
 * action in words, and the tint is an accent on text that already says it.
 */
const TONE: Record<string, string> = {
  login: 'text-slate-300',
  logout: 'text-slate-400',
  login_failed: 'text-amber-400',
  rate_limited: 'text-rose-400',
  forbidden: 'text-rose-400',
}

const FILTERS = [
  { value: '', label: 'Everything' },
  { value: 'login', label: 'Sign-ins' },
  { value: 'login_failed', label: 'Failed sign-ins' },
  { value: 'rate_limited', label: 'Rate-limited' },
  { value: 'forbidden', label: 'Refused actions' },
  { value: 'logout', label: 'Sign-outs' },
]

function Row({ entry }: { entry: AuditEntryView }) {
  const detail = Object.entries(entry.detail ?? {})
  return (
    <tr className="border-t border-slate-800 align-top">
      <td className="whitespace-nowrap px-3 py-2 text-slate-400 tabular-nums">
        {new Date(entry.at).toLocaleString()}
      </td>
      <td className="px-3 py-2 text-slate-300">{entry.actor}</td>
      <td
        className={`whitespace-nowrap px-3 py-2 font-medium ${TONE[entry.action] ?? 'text-slate-300'}`}
      >
        {entry.action}
      </td>
      {/* An absent target renders as a dash, never as blank or as a zero-like
          placeholder — plenty of actions genuinely have no object, and "signing
          out is not done to anything" should read as that rather than as
          missing data (docs/DASHBOARD.md). */}
      <td className="px-3 py-2 text-slate-400">{entry.target ?? '—'}</td>
      <td className="px-3 py-2 text-slate-500">
        {detail.length === 0
          ? '—'
          : detail.map(([key, value]) => (
              <span key={key} className="mr-3 whitespace-nowrap">
                {key}=<span className="text-slate-400">{String(value)}</span>
              </span>
            ))}
      </td>
    </tr>
  )
}

export default function Audit() {
  const [action, setAction] = useState('')
  const [pages, setPages] = useState<number[]>([])

  const beforeId = pages.at(-1)
  const query = useQuery({
    queryKey: ['audit', action, beforeId ?? null],
    queryFn: () => {
      const params = new URLSearchParams({ limit: '100' })
      if (action) params.set('action', action)
      if (beforeId) params.set('before_id', String(beforeId))
      return apiGet<AuditPage>(`/api/v1/audit?${params}`)
    },
  })

  // 503 means the record could not be read, which is not the same as the record
  // being empty. Saying "no entries" when the database is unreachable would tell
  // the reader nothing happened — during exactly the incident they opened this
  // page to investigate.
  const unreachable = query.error instanceof ApiError && query.error.status === 503

  // `entries` is optional in the generated schema because the server model
  // defaults it. Resolved once here rather than with a `?.` at each use, so the
  // "empty" branch below is comparing a list rather than a maybe-list.
  const entries = query.data?.entries ?? []

  return (
    <div>
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-sm font-semibold text-slate-200">Audit trail</h1>
        <label className="sr-only" htmlFor="action-filter">
          Filter by action
        </label>
        <select
          id="action-filter"
          value={action}
          onChange={(event) => {
            setAction(event.target.value)
            setPages([])
          }}
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
        >
          {FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {pages.length > 0 ? (
          <button
            type="button"
            onClick={() => setPages([])}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
          >
            Back to newest
          </button>
        ) : null}
      </div>

      {unreachable ? (
        <p
          role="alert"
          className="rounded border border-amber-700/60 bg-amber-950/40 p-3 text-sm text-amber-300"
        >
          The audit trail could not be read. This is not the same as it being empty — nothing can be
          concluded from this screen until the database is reachable again.
        </p>
      ) : query.isPending ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : query.error ? (
        <p role="alert" className="text-sm text-rose-400">
          Could not load the audit trail.
        </p>
      ) : entries.length === 0 ? (
        <p className="text-sm text-slate-500">
          {action ? 'Nothing recorded of that kind yet.' : 'Nothing recorded yet.'}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto rounded border border-slate-800">
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">When</th>
                  <th className="px-3 py-2 font-medium">Who</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 font-medium">Target</th>
                  <th className="px-3 py-2 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <Row key={entry.id} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>

          {query.data?.next_before_id ? (
            <button
              type="button"
              onClick={() => setPages([...pages, query.data.next_before_id!])}
              className="mt-3 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:text-slate-100"
            >
              Older
            </button>
          ) : null}
        </>
      )}
    </div>
  )
}
