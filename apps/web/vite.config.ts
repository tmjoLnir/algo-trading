/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: { port: 5173, host: true },
  test: {
    // jsdom rather than the default node environment: the dashboard's rules —
    // a P&L that shows a sign as well as a colour, a stale price that is greyed
    // out, an unknown figure that renders as a dash rather than as zero — are
    // properties of what is on the screen. Asserting them against a returned
    // object rather than against rendered output would test the code that
    // builds the props and not the thing a person reads.
    environment: 'jsdom',
    // `globals` stays off. Every test imports `describe`/`it`/`expect` from
    // `vitest` explicitly, which is what `src/api/client.test.ts` already did
    // and what keeps the test files honest under `tsc --noEmit` without adding
    // ambient types to tsconfig.
    globals: false,
    restoreMocks: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
