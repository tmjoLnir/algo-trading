/**
 * The refusals query.
 *
 * Deliberately unfiltered by default. The first question this screen answers is
 * "is *anything* being refused", and narrowing to a strategy or a rule before a
 * reader has seen the whole picture would answer a question they have not asked
 * yet — the server does the same, and its `rejections()` filters in SQL so the
 * newest refusals are found however long ago they happened.
 *
 * Not polled. Refusals accumulate on the worker's evaluation interval, and the
 * Dashboard's signal feed is the screen for watching something move; this one is
 * read when somebody is asking why nothing is happening.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { RejectionsResponse } from '@/api/types'

export function useRejections(limit = 50) {
  return useQuery<RejectionsResponse>({
    queryKey: ['risk', 'rejections', limit],
    queryFn: () => apiGet<RejectionsResponse>(`/api/v1/risk/rejections?limit=${limit}`),
    retry: false,
  })
}
