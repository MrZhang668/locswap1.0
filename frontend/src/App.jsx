import { useEffect, useState } from 'react'
import { api, clearToken, getToken } from './api'
import { useAsync } from './components/common'
import Cart from './pages/Cart'
import Find from './pages/Find'
import Home from './pages/Home'
import Login from './pages/Login'
import Messages from './pages/Messages'
import Onboarding from './pages/Onboarding'
import Profile from './pages/Profile'
import Sell from './pages/Sell'

// `needsAccount` is the one place the rule lives, so the nav, the tab guard and
// the sign-in prompt cannot drift apart. The home board is open to visitors;
// every other tab asks for an account first.
const NAV = [
  { key: 'home', label: '首页' },
  { key: 'find', label: '搜索', needsAccount: '用搜索和地图找东西' },
  { key: 'cart', label: '购物车', needsAccount: '查看你预订的商品' },
  { key: 'sell', label: '我要卖', needsAccount: '发布商品' },
  { key: 'messages', label: '消息', needsAccount: '查看你的消息' },
]
const NEEDS_ACCOUNT = Object.fromEntries(
  NAV.filter((n) => n.needsAccount).map((n) => [n.key, n.needsAccount]),
)

export default function App() {
  const [user, setUser] = useState(null)
  const [booting, setBooting] = useState(true)
  const [tab, setTab] = useState('home')
  // Set from the home board's "View on the map", so Search opens pre-filtered to that category
  const [findCategory, setFindCategory] = useState(null)
  const [config, setConfig] = useState(null)
  // Holds the reason we are asking, e.g. "to reserve this item" — so the prompt
  // says why it appeared instead of being an unexplained wall.
  const [signInFor, setSignInFor] = useState(null)

  // Everything that needs an account funnels through here. A page asks for the
  // action; if there is nobody signed in it becomes a prompt with a reason
  // attached, and the action is simply not performed. Nothing is queued to run
  // "after login" — a reserve that fires on its own once you sign in is a
  // surprise, and this one takes someone else's place in a queue.
  const requireAccount = (reason, run) => {
    if (user) return run()
    setSignInFor(reason)
    return undefined
  }

  const goTab = (key) => {
    const reason = NEEDS_ACCOUNT[key]
    if (reason && !user) return setSignInFor(reason)
    return setTab(key)
  }

  // Cart + notifications live in App so several pages share the same data + badges
  const cart = useAsync(() => (user ? api.cart() : Promise.resolve([])), [user?.id])
  const notes = useAsync(() => (user ? api.notifications() : Promise.resolve([])), [user?.id])

  useEffect(() => {
    (async () => {
      try {
        setConfig(await api.config())
      } catch { /* don't block the login page when the backend is down */ }
      if (getToken()) {
        try {
          setUser(await api.me())
        } catch {
          clearToken()
        }
      }
      setBooting(false)
    })()
  }, [])

  // Item status can be changed by others (sold / expired), so poll the badge data
  useEffect(() => {
    if (!user) return undefined
    const t = setInterval(() => { cart.reload(); notes.reload() }, 30000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id])

  // No useCallback: reload closes over the current user; caching it would keep the pre-login one forever
  const refreshAll = () => { cart.reload(); notes.reload() }

  const logout = () => {
    clearToken()
    setUser(null)
    setTab('home')
  }

  if (booting) {
    return <div className="auth-screen"><div className="empty">正在启动…</div></div>
  }

  // No wall here any more. A visitor lands on the board, and the product gets to
  // make its case before it asks for anything.

  if (user && !user.onboarded) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <Onboarding config={config} onDone={(u) => setUser(u)} />
        </div>
      </div>
    )
  }

  const unread = (notes.data ?? []).filter((n) => !n.read).length
  const cartCount = (cart.data ?? []).length

  return (
    <div className="appshell">
      <nav className="mainnav" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark">L</span>
          LocSwap
        </div>
        {NAV.map((n) => {
          const badge = n.key === 'messages' ? unread : n.key === 'cart' ? cartCount : 0
          return (
            <button
              key={n.key}
              className={`navlink ${tab === n.key ? 'active' : ''}`}
              aria-current={tab === n.key ? 'page' : undefined}
              onClick={() => goTab(n.key)}
            >
              {n.label}
              {badge > 0 && <span className="dot">{badge}</span>}
            </button>
          )
        })}
        <div className="nav-right">
          <span className="nav-loc" title="当前位置（可在个人资料里修改）">
            <svg className="loc-ico" width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M12 2a7 7 0 0 0-7 7c0 5.25 7 13 7 13s7-7.75 7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z" />
            </svg>
            {user ? (user.location_name || '未设置位置') : '距离从校园中心起算'}
            {user && user.pickup_range_enabled && user.pickup_range_km
              ? ` · ${Number(user.pickup_range_km).toFixed(1)} 公里内`
              : ''}
          </span>
          {user ? (
            <button
              className={`navlink ${tab === 'profile' ? 'active' : ''}`}
              style={{ padding: '0 10px' }}
              aria-current={tab === 'profile' ? 'page' : undefined}
              onClick={() => setTab('profile')}
              title="个人资料"
            >
              <span className="nav-avatar">
                {(user.email || '?')[0].toUpperCase()}
              </span>
              <span className="zh">个人资料</span>
            </button>
          ) : (
            <button className="navlink" style={{ padding: '0 14px' }}
                    onClick={() => setSignInFor('使用你的账号')}>
              登录
            </button>
          )}
        </div>
      </nav>

      <main className="page">
        {tab === 'home' && (
          <Home
            config={config}
            onGo={(t) => { setFindCategory(null); goTab(t) }}
            onPickCategory={(c) => { setFindCategory(c); goTab('find') }}
            onChanged={refreshAll}
            requireAccount={requireAccount}
          />
        )}
        {tab === 'find' && (
          <Find user={user} config={config} onChanged={refreshAll}
                initialCategory={findCategory} requireAccount={requireAccount} />
        )}
        {tab === 'cart' && (
          <Cart rows={cart.data ?? []} loading={cart.loading} error={cart.error}
                reload={cart.reload} onChanged={refreshAll} />
        )}
        {tab === 'sell' && <Sell user={user} config={config} onChanged={refreshAll} />}
        {tab === 'messages' && (
          <Messages onChanged={refreshAll} notifications={notes.data ?? []} reloadNotifications={notes.reload} />
        )}
        {tab === 'profile' && user && (
          <Profile user={user} config={config} onUser={setUser} onLogout={logout} />
        )}
      </main>

      {signInFor && (
        <div className="mask" role="dialog" aria-modal="true"
             aria-label="登录" onClick={() => setSignInFor(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <p className="mini muted" style={{ marginBottom: 10 }}>
              登录后即可{signInFor}。不登录也可以继续浏览。
            </p>
            <Login onDone={(u) => { setUser(u); setSignInFor(null) }} />
            <button className="quiet-btn sm" style={{ marginTop: 12 }}
                    onClick={() => setSignInFor(null)}>
              继续逛逛
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
