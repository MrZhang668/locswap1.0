import { useState } from 'react'
import { api } from '../api'
import { Toast } from '../components/common'

/** Mandatory first-login onboarding: pick a location (required) → set a pickup range (skippable). */
export default function Onboarding({ config, onDone }) {
  const [step, setStep] = useState(1)
  const [location, setLocation] = useState('')
  const [rangeEnabled, setRangeEnabled] = useState(false)
  const [rangeKm, setRangeKm] = useState(config?.default_pickup_range_km ?? 3)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const saveLocation = async () => {
    if (!location) return setError('请先选择一个位置')
    setBusy(true); setError('')
    try {
      await api.updateMe({ location_name: location })
      setStep(2)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const finish = async (skip) => {
    setBusy(true); setError('')
    try {
      const user = skip
        ? await api.me()
        : await api.updateMe({ pickup_range_enabled: rangeEnabled, pickup_range_km: Number(rangeKm) })
      onDone(user)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <div>
        <div className="logo" style={{ fontSize: 21 }}>
          {step === 1 ? '你在校园的哪个位置？' : '要限制自提范围吗？'}
        </div>
        <div className="muted tiny" style={{ marginTop: 6, marginBottom: 22 }}>
          {step === 1
            ? '用来计算商品离你有多远。之后可以随时在个人资料里改。'
            : '开启后，搜索只会返回这个距离以内的商品。这一步可以跳过。'}
        </div>

        <Toast message={error} tone="err" />

        {step === 1 ? (
          <>
            <div className="field">
              <label>我的位置</label>
              <select className="input" value={location} onChange={(e) => setLocation(e.target.value)}>
                <option value="">选择一个预设地点</option>
                {(config?.locations ?? []).map((l) => (
                  <option key={l.name} value={l.name}>{l.name}</option>
                ))}
              </select>
            </div>
            <button className="btn block" onClick={saveLocation} disabled={busy}>下一步</button>
          </>
        ) : (
          <>
            <div className="card">
              <label className="row between" style={{ cursor: 'pointer' }}>
                <span className="strong">开启自提范围限制</span>
                <input type="checkbox" checked={rangeEnabled}
                       onChange={(e) => setRangeEnabled(e.target.checked)} />
              </label>
              {rangeEnabled && (
                <div style={{ marginTop: 12 }}>
                  <div className="row between tiny muted">
                    <span>最远距离</span><span className="strong">{Number(rangeKm).toFixed(1)} 公里</span>
                  </div>
                  <input type="range" min="0.3" max="10" step="0.1" value={rangeKm}
                         style={{ width: '100%' }}
                         onChange={(e) => setRangeKm(e.target.value)} />
                </div>
              )}
            </div>
            <button className="btn block" onClick={() => finish(false)} disabled={busy}>完成，开始逛</button>
            <button className="btn plain block" style={{ marginTop: 10 }}
                    onClick={() => finish(true)} disabled={busy}>跳过</button>
          </>
        )}
      </div>
    </>
  )
}
