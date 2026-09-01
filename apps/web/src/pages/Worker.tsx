/**
 * The Worker tab — what this platform trades.
 *
 * Its own screen rather than a panel on Strategies, and the split is about what
 * each answers. Strategies is "what exists and what has run"; this is "what the
 * one live process is configured to do", which is a different question asked at
 * a different time — usually right before a restart, and usually by somebody
 * who has just read a reason on the dashboard saying nothing is trading.
 */

import WorkerConfigPanel from '@/components/WorkerConfigPanel'

export default function Worker() {
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">Worker configuration</h1>
        <p className="mt-1 text-xs text-slate-500">
          These settings live in the database, not in <code>.env</code>. They are read by the worker
          once, when it starts, and every save is recorded in the audit log with who made it.
        </p>
      </div>
      <WorkerConfigPanel />
    </div>
  )
}
