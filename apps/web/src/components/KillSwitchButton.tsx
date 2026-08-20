/**
 * Emergency stop.
 *
 * Always visible, never behind a menu. Engaging asks for no confirmation —
 * hesitation is the expensive part. Resuming is the deliberate action and is
 * deliberately not here: `POST /risk/resume` demands the account password again
 * (ADR 0009), so it asks for one from `ResumeButton`, on the halt it is
 * clearing, inside the banner a halted reader is already looking at.
 * `scripts/halt.py clear --by <name>` remains the path that works when the API
 * does not.
 *
 * **A failed halt is shown, not swallowed.** This is the one control in the app
 * that acts on the book, and the failure it can hit is the one an operator is
 * least equipped to guess at: the API answers 503 when the switch could not be
 * written, and that state is neither "stopped" nor "still trading" — the switch
 * fails closed, so nothing trades while the store is unreachable, but nothing
 * was recorded either, so trading resumes on its own when it recovers. The
 * server's own `detail` is rendered verbatim because it is the only thing that
 * explains that, and a button that quietly returned to its resting state would
 * read as "I stopped it".
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiPost } from '@/api/client'

export default function KillSwitchButton({ halted }: { halted: boolean }) {
  const qc = useQueryClient()
  const halt = useMutation({
    mutationFn: () => apiPost('/api/v1/risk/halt', { scope: 'global', reason: 'manual' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['dashboard', 'live'] }),
  })

  if (halted) {
    return (
      <span className="rounded bg-rose-900 px-3 py-1.5 text-xs font-bold text-rose-200">
        TRADING HALTED
      </span>
    )
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={() => halt.mutate()}
        disabled={halt.isPending}
        className="rounded bg-rose-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-rose-500 disabled:opacity-50"
      >
        {halt.isPending ? 'Halting…' : 'HALT TRADING'}
      </button>

      {halt.error ? (
        // `role="alert"` so this is announced rather than merely displayed: the
        // button keeps its own label on failure, so a reader who is not looking
        // directly at this corner has nothing else to tell them the stop did
        // not take.
        <p role="alert" className="max-w-md text-right text-xs text-rose-300">
          {halt.error instanceof ApiError ? halt.error.detail : String(halt.error)}
        </p>
      ) : null}
    </div>
  )
}
