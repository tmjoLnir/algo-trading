import { Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Strategies from './pages/Strategies'
import Backtests from './pages/Backtests'
import Positions from './pages/Positions'
import Orders from './pages/Orders'
import Analytics from './pages/Analytics'
import Login from './pages/Login'
import RunModeBanner from './components/RunModeBanner'
import HaltBanner from './components/HaltBanner'
import { useLogout, useSession } from './api/session'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/strategies', label: 'Strategies' },
  { to: '/backtests', label: 'Backtests' },
  { to: '/positions', label: 'Positions' },
  { to: '/orders', label: 'Orders' },
  { to: '/analytics', label: 'Analytics' },
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
      {/* Both banners are above the nav on purpose: whether this is real money,
          and whether trading is halted, are the two facts a user must never
          have to scroll or click to discover. */}
      <RunModeBanner />
      <HaltBanner />

      <nav className="flex items-center gap-1 border-b border-slate-800 px-4">
        {NAV.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `px-4 py-3 text-sm ${isActive ? 'border-b-2 border-sky-400 text-sky-400' : 'text-slate-400 hover:text-slate-200'}`
            }
          >
            {label}
          </NavLink>
        ))}

        {/* Who you are signed in as, at the far end. Not decoration: every
            halt and every manual order is now recorded against this name, so
            it should be visible before you press the red button rather than
            discovered in the audit log afterwards. */}
        <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
          <span>{user}</span>
          {/* Stated rather than implied. A read-only session behaves almost
              identically today — the only acting control on this screen is the
              kill switch, which read-only sessions may deliberately still use —
              so without this badge the difference would be invisible until
              something was refused. */}
          {!mayAct ? (
            <span
              className="rounded border border-slate-700 px-1.5 py-0.5 text-slate-400"
              title="This session can see the book and halt trading, but cannot place, cancel or close anything."
            >
              read-only
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
            className="rounded border border-slate-700 px-2 py-1 text-slate-400 hover:text-slate-200 disabled:opacity-50"
          >
            Sign out
          </button>
        </div>
      </nav>

      <main className="p-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/analytics" element={<Analytics />} />
        </Routes>
      </main>
    </div>
  )
}
