/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Where the dev server forwards /api, /ws and the health probes. Defaults to a
// local `make dev-api`; docker-compose sets it to the api service, because
// inside the web container `localhost` is the web container.
const devProxyTarget = process.env.ATP_DEV_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    port: 5173,
    host: true,
    // The dev server proxies the API onto its own origin, which is how
    // production serves it too (infra/docker/web.nginx.conf). The point is not
    // convenience — it is that both environments resolve the API the same way.
    // With the dev server talking cross-origin to :8000 and production talking
    // same-origin, every CORS and mixed-content problem is invisible until
    // deploy, and `import.meta.env.VITE_API_BASE_URL` has to be right in two
    // places that are never exercised together.
    proxy: {
      '/api': { target: devProxyTarget, changeOrigin: true },
      '/healthz': { target: devProxyTarget, changeOrigin: true },
      '/readyz': { target: devProxyTarget, changeOrigin: true },
      '/ws': { target: devProxyTarget, ws: true, changeOrigin: true },
    },
  },
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
