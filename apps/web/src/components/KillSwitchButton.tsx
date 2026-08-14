/**
 * Emergency stop.
 *
 * Always visible, never behind a menu. Engaging asks for no confirmation —
 * hesitation is the expensive part. Resuming is the deliberate action, done
 * from the risk page, because restarting should require thought in a way
 * stopping must not.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiPost } from '@/api/client'

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
    <button
      onClick={() => halt.mutate()}
      disabled={halt.isPending}
      className="rounded bg-rose-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-rose-500 disabled:opacity-50"
    >
      {halt.isPending ? 'Halting…' : 'HALT TRADING'}
    </button>
  )
}
