import { Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Strategies from './pages/Strategies'
import Backtests from './pages/Backtests'
import Positions from './pages/Positions'
import Orders from './pages/Orders'
import Analytics from './pages/Analytics'
import RunModeBanner from './components/RunModeBanner'
import HaltBanner from './components/HaltBanner'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/strategies', label: 'Strategies' },
  { to: '/backtests', label: 'Backtests' },
  { to: '/positions', label: 'Positions' },
  { to: '/orders', label: 'Orders' },
  { to: '/analytics', label: 'Analytics' },
]

export default function App() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* Both banners are above the nav on purpose: whether this is real money,
          and whether trading is halted, are the two facts a user must never
          have to scroll or click to discover. */}
      <RunModeBanner />
      <HaltBanner />

      <nav className="flex gap-1 border-b border-slate-800 px-4">
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
