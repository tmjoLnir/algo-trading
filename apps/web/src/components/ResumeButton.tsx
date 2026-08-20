/**
 * Clearing one halt, from the banner that is announcing it.
 *
 * The deliberate half of the pair `KillSwitchButton` opens. Stopping is one
 * click and asks nothing; starting again asks for the account password, because
 * a session cookie proves somebody logged in this morning and not that anybody
 * is at the keyboard now (ADR 0009). Until this existed the asymmetry was
 * enforced by there being no screen at all, and clearing a halt meant shell
 * access to `scripts/halt.py` — which made the platform's most reversible
 * safety control the least reachable thing in it.
 *
 * **One control per halt, not one per banner.** A halt is keyed on
 * (scope, target) and is cleared by that same pair, so a single button could
 * only ever clear one of the halts on screen while appearing to clear the lot.
 * Each row carries its own and sends its own key.
 *
 * The password lives in component state for as long as the form is open and is
 * dropped the moment the resume succeeds. It is deliberately not lifted into a
 * store, a query cache or a URL — `/resume` takes it in the body for the same
 * reason, since a query string is written to nginx's access log verbatim.
 *
 * **A failed resume is shown, never swallowed**, exactly as a failed halt is —
 * but it means the opposite and the server's own words are what carry that. A
 * 503 here says the halt still stands and nothing is trading, which is the safe
 * direction; a reader left to guess from a red box would reasonably assume they
 * are now half resumed.
 */

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiPost } from '@/api/client'
import type { HaltView } from '@/api/types'

export default function ResumeButton({ halt }: { halt: HaltView }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState('')

  const resume = useMutation({
    mutationFn: () =>
      apiPost('/api/v1/risk/resume', {
        scope: halt.scope,
        target: halt.target,
        password,
      }),
    onSuccess: () => {
      // Dropped on success only. A wrong password is very much more likely to
      // be a typo than an intruder, and clearing the field on a 403 would make
      // the operator retype the whole thing to fix one character.
      setPassword('')
      setOpen(false)
      // The banner is rendered from this query, so the invalidation is what
      // makes the cleared row disappear — and what leaves the *other* rows in
      // place if any remain. `/resume` deliberately does not report what is
      // still halted; this is where that question gets answered.
      qc.invalidateQueries({ queryKey: ['dashboard', 'live'] })
    },
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded border border-rose-700 px-2 py-0.5 text-xs font-semibold text-rose-200 hover:bg-rose-900/60"
      >
        Resume…
      </button>
    )
  }

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        resume.mutate()
      }}
      className="flex flex-wrap items-center gap-2"
    >
      <label
        className="sr-only"
        htmlFor={`resume-password-${halt.scope}-${halt.target ?? 'global'}`}
      >
        Account password, to resume {halt.target ?? 'all'} trading
      </label>
      <input
        id={`resume-password-${halt.scope}-${halt.target ?? 'global'}`}
        type="password"
        autoComplete="current-password"
        autoFocus
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        placeholder="account password"
        className="w-44 rounded border border-rose-700 bg-rose-950 px-2 py-0.5 text-xs text-rose-100 placeholder:text-rose-400/60"
      />
      <button
        type="submit"
        disabled={resume.isPending || password.length === 0}
        className="rounded bg-rose-700 px-2 py-0.5 text-xs font-bold text-white hover:bg-rose-600 disabled:opacity-50"
      >
        {resume.isPending ? 'Resuming…' : 'RESUME'}
      </button>
      <button
        type="button"
        onClick={() => {
          setPassword('')
          setOpen(false)
          resume.reset()
        }}
        className="text-xs text-rose-300/80 hover:text-rose-200"
      >
        Cancel
      </button>

      {resume.error ? (
        // Announced rather than merely displayed, for the reason the halt
        // button's is: the form keeps its own appearance on failure, so a
        // reader not looking straight at this row has nothing else to tell them
        // trading did not restart.
        <p role="alert" className="w-full text-xs text-rose-300">
          {resume.error instanceof ApiError ? resume.error.detail : String(resume.error)}
        </p>
      ) : null}
    </form>
  )
}
