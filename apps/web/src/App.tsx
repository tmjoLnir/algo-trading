import { useEffect } from 'react'
import { Routes, Route, NavLink, useLocation } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Strategies from './pages/Strategies'
import Worker from './pages/Worker'
import Backtests from './pages/Backtests'
import Positions from './pages/Positions'
import Orders from './pages/Orders'
import Analytics from './pages/Analytics'
import Audit from './pages/Audit'
import Login from './pages/Login'
import RunModeBanner from './components/RunModeBanner'
import HaltBanner from './components/HaltBanner'
import LiveStream from './components/LiveStream'
import { useLogout, useSession } from './api/session'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/strategies', label: 'Strategies' },
  { to: '/backtests', label: 'Backtests' },
  { to: '/positions', label: 'Positions' },
  { to: '/orders', label: 'Orders' },
  { to: '/worker', label: 'Worker' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/audit', label: 'Audit' },
]

function Centred({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4 text-sm text-slate-400">
      {children}
    </div>
  )
}

export default function App() {
  const { user, mayAct, isAuthenticated, isPending, error } = useSession()
  const logout = useLogout()
  const { pathname } = useLocation()

  // Pinning the nav created this, so it is fixed here rather than left as
  // someone else's bug: a tab is now reachable from halfway down a long table,
  // and React Router does not reset the window's scroll offset across a route
  // change. Switching tabs from the bottom of Orders used to be impossible —
  // you had to scroll up to the nav to do it — and now lands you at the bottom
  // of Analytics, looking at whatever happens to be there. The scroll position
  // of the screen you left is not a fact about the screen you asked for.
  //
  // **Deleting this looks harmless and is not.** On a first visit the
  // destination renders its loading state, the document briefly gets shorter
  // than the offset, and the browser clamps the scroll to the top on its own —
  // so a route change tested once appears to reset correctly with no effect
  // here at all. The offset comes straight back on every *revisit*, where
  // TanStack Query has the destination cached and renders it full-height with
  // nothing to clamp against. Measured: a warm return to `/positions` from
  // 1400px down a 3903px dashboard stays at 1400 without this, on a page 3625px
  // tall that never collapsed.
  //
  // `documentElement.scrollTop` rather than `window.scrollTo`, which jsdom does
  // not implement and complains about on every route change under test.
  useEffect(() => {
    document.documentElement.scrollTop = 0
  }, [pathname])

  // Three states, not two. "Not logged in" and "cannot tell" are different, and
  // rendering the login form for the second would have the operator typing a
  // password at a server that is not in a position to check it — and reading
  // the failure as their mistake.
  if (isPending) return <Centred>Checking your session…</Centred>
  if (error) {
    return (
      <Centred>
        <div className="text-center">
          <p className="text-slate-300">Cannot reach the API.</p>
          <p className="mt-1 text-xs">
            This is not a sign-in problem — the server did not answer. It will retry on its own.
          </p>
        </div>
      </Centred>
    )
  }
  if (!isAuthenticated) return <Login />

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* Renders nothing; holds the live socket open for as long as somebody is
          signed in. Here rather than on the dashboard because `HaltBanner`
          below is on every screen, and a banner fed by a socket that exists on
          one route cannot interrupt anybody on the other screens. */}
      <LiveStream />

      {/* The two banners and the nav are pinned to the top of the viewport as
          one block, on every route.

          The banners were already above the nav because whether this is real
          money, and whether trading is halted, are the two facts a user must
          never have to scroll or click to discover — but "above" only held at
          the top of the page. Every screen here is a long table, and an
          operator reading the bottom of one had scrolled both banners away.
          The state that interrupts you is worth nothing if it is 900px above
          the thing you are about to act on.

          `bg-slate-950` is load-bearing, not decoration. The nav has no
          background of its own and both banners are alpha-blended
          (`bg-amber-500/90`, `bg-rose-950/80`), so a pinned bar without an
          opaque backdrop would have order rows sliding visibly through the
          words LIVE TRADING. Slate-950 is what they already composite against
          — the page — so nothing changes at rest.

          `z-40` because nothing else in this app sets a z-index at all: it
          beats every in-flow element and the chart tooltips, which recharts
          leaves at `auto`, and leaves `z-50` free for a modal if one ever
          arrives. */}
      <header className="sticky top-0 z-40 bg-slate-950">
        <RunModeBanner />
        <HaltBanner />

        {/* `overflow-x-auto` keeps a narrow window's overflow inside the tab
            strip. Eight tabs plus the account block are wider than a small
            laptop, and left to overflow the *page* they would give the document
            a horizontal scrollbar — which a sticky element does not resist,
            because stickiness is vertical only. The pinned header would slide
            sideways off the screen exactly when it is least affordable. */}
        <nav className="flex items-center gap-1 overflow-x-auto border-b border-slate-800 px-4">
          {NAV.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap px-4 py-3 text-sm ${isActive ? 'border-b-2 border-sky-400 text-sky-400' : 'text-slate-400 hover:text-slate-200'}`
              }
            >
              {label}
            </NavLink>
          ))}

          {/* Who you are signed in as, at the far end. Not decoration: every
              halt and every manual order is now recorded against this name, so
              it should be visible before you press the red button rather than
              discovered in the audit log afterwards. */}
          <div className="ml-auto flex shrink-0 items-center gap-3 pl-3 text-xs text-slate-500">
            <span>{user}</span>
            {/* Stated rather than implied. A read-only session behaves almost
                identically today — the only acting control on this screen is the
                kill switch, which read-only sessions may deliberately still use —
                so without this badge the difference would be invisible until
                something was refused. */}
            {!mayAct ? (
              <span
                className="whitespace-nowrap rounded border border-slate-700 px-1.5 py-0.5 text-slate-400"
                title="This session can see the book and halt trading, but cannot place, cancel or close anything."
              >
                read-only
              </span>
            ) : null}
            <button
              type="button"
              onClick={() => logout.mutate()}
              disabled={logout.isPending}
              className="whitespace-nowrap rounded border border-slate-700 px-2 py-1 text-slate-400 hover:text-slate-200 disabled:opacity-50"
            >
              Sign out
            </button>
          </div>
        </nav>
      </header>

      <main className="p-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/worker" element={<Worker />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/audit" element={<Audit />} />
        </Routes>
      </main>
    </div>
  )
}
