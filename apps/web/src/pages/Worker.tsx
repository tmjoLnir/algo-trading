/**
 * The Config tab — what this platform trades, and what it will let itself risk.
 *
 * Its own screen rather than a panel on Strategies, and the split is about what
 * each answers. Strategies is "what exists and what has run"; this is "what the
 * one live process is configured to do", which is a different question asked at
 * a different time — usually right before a restart, and usually by somebody
 * who has just read a reason on the dashboard saying nothing is trading.
 *
 * **Called Config rather than Worker**, because the risk ceilings underneath the
 * worker settings are not the worker's. They bind every order this platform
 * places, including one an operator types into this dashboard while no worker is
 * running at all — so a tab named after the process would misdescribe half of
 * what is on it.
 */

import WorkerConfigPanel from '@/components/WorkerConfigPanel'

export default function Worker() {
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">Configuration</h1>
        <p className="mt-1 text-xs text-slate-500">
          What the worker trades, and the account-wide ceilings every order is measured against.
          These settings live in the database, not in <code>.env</code>, and every save is recorded
          in the audit log with who made it.
        </p>
      </div>
      <WorkerConfigPanel />
    </div>
  )
}
