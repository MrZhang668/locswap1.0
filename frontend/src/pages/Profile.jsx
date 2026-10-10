import { useState } from 'react'
import { api } from '../api'
import { Toast } from '../components/common'

/**
 * Profile: desktop account page.
 *
 * At rest it shows one thing — the account summary, centred on the page. Settings
 * is a toggle: pressing it turns the button black and slides the location /
 * pickup-range form in as a second column; pressing it again closes the form and
 * the summary returns to the centre. Most visits here are "check my location",
 * not "change my location", so the form stays out of the way until it's asked for.
 *
 * Same quiet treatment as Cart — no cards, no borders, no brand colour. The only
 * filled surfaces are the location select (it has to read as an input), Log out
 * (the one destructive action), and Settings while it is open.
 */
export default function Profile({ user, config, onUser, onLogout }) {
  const [location, setLocation] = useState(user.location_name ?? '')
  const [rangeEnabled, setRangeEnabled] = useState(user.pickup_range_enabled)
  const [rangeKm, setRangeKm] = useState(user.pickup_range_km ?? config?.default_pickup_range_km ?? 3)
  const [autoNotify, setAutoNotify] = useState(Boolean(user.auto_notify))
  const [tip, setTip] = useState({ text: '', tone: 'info' })
  const [busy, setBusy] = useState(false)
  // The settings form is opt-in: at rest the page is just the account card,
  // centred. Settings toggles the second column in and out.
  const [settingsOpen, setSettingsOpen] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      const updated = await api.updateMe({
        location_name: location || undefined,
        pickup_range_enabled: rangeEnabled,
        pickup_range_km: Number(rangeKm),
        auto_notify: autoNotify,
      })
      onUser(updated)
      setTip({ text: '已保存', tone: 'ok' })
    } catch (e) {
      setTip({ text: e.message, tone: 'err' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-scroll">
      <div className="container profile-page">
        <div className="page-head stack">
          <h1>个人资料</h1>
        </div>

        <div className={`profile-grid ${settingsOpen ? 'open' : ''}`}>
          {/* ---------- Left: account summary ---------- */}
          <div className="profile-side">
            <div className="avatar">{(user.email || '?')[0].toUpperCase()}</div>
            <div className="who">{user.email}</div>
            <div className="who-sub">账号 ID：{user.id} · 头像是占位图</div>

            <dl className="kv">
              <dt>当前位置</dt>
              <dd>{user.location_name || '未设置'}</dd>
              <dt>自提范围</dt>
              <dd>
                {user.pickup_range_enabled && user.pickup_range_km
                  ? `${Number(user.pickup_range_km).toFixed(1)} 公里以内`
                  : '不限'}
              </dd>
            </dl>

            <div className="quiet-label">系统</div>
            <button className={`quiet-btn block ${settingsOpen ? 'on' : ''}`}
                    aria-expanded={settingsOpen}
                    onClick={() => setSettingsOpen((v) => !v)}>
              设置
            </button>
            <button className="quiet-btn block danger" onClick={onLogout}>退出登录</button>
          </div>

          {/* ---------- Right: settings form, revealed by the Settings button ---------- */}
          {settingsOpen && (
          <div className="form-col">
            <Toast message={tip.text} tone={tip.tone} />

            <section className="fieldset">
              <div className="field-label">我的位置</div>
              <select className="input big" value={location} onChange={(e) => setLocation(e.target.value)}>
                <option value="">未设置</option>
                {(config?.locations ?? []).map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
              </select>
              <p className="hint">
                商品距离以这个位置为起点计算（haversine）；顶部导航和搜索地图上的绿点也用它。
              </p>
            </section>

            <section className="fieldset">
              <div className="field-label">自提范围</div>
              <label className="check-row">
                <span>只显示这个距离以内的商品</span>
                <input type="checkbox" checked={rangeEnabled} onChange={(e) => setRangeEnabled(e.target.checked)} />
              </label>
              {rangeEnabled && (
                <div className="range-row">
                  <div className="range-head">
                    <span>最远距离</span><span className="v">{Number(rangeKm).toFixed(1)} 公里</span>
                  </div>
                  <input type="range" min="0.3" max="10" step="0.1" value={rangeKm}
                         onChange={(e) => setRangeKm(e.target.value)} />
                </div>
              )}
              <p className="hint">
                开启后，它会成为检索管线里的一个硬性筛选条件。
              </p>
            </section>

            <section className="fieldset">
              <div className="field-label">通知</div>
              <label className="check-row">
                <span>我登记过的东西上架时通知我</span>
                <input type="checkbox" id="auto-notify" checked={autoNotify}
                       onChange={(e) => setAutoNotify(e.target.checked)} />
              </label>
              {/* Says exactly what it will and will not do. The distinction is
                  the whole design: requests you wrote on purpose count, things
                  you happened to search for do not — and saying so is how the
                  setting stays trustworthy once it starts sending. */}
              <p className="hint">
                只针对你自己保存过的求购，而且只在同类别下高度匹配时才会发。
                随手搜过一次的东西不算。默认关闭，每天有条数上限，随时可以在这里关掉。
              </p>
            </section>

            <button className="quiet-btn primary" onClick={save} disabled={busy}>
              {busy ? '保存中…' : '保存设置'}
            </button>

            {config && (
              <details className="debug-wrap center">
                <summary>运行时信息（检索索引 / 模型 / 配置）</summary>
                <pre className="debug">{JSON.stringify({
                  confirm_window_hours: config.confirm_window_hours,
                  default_pickup_range_km: config.default_pickup_range_km,
                  llm_available: config.llm_available,
                  llm_unavailable_reason: config.llm_unavailable_reason,
                  index_stats: config.index_stats,
                }, null, 2)}</pre>
              </details>
            )}
          </div>
          )}
        </div>
      </div>
    </div>
  )
}
