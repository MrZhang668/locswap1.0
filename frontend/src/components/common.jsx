import { useEffect, useState } from 'react'

/** Countdown: ticks down locally from confirm_deadline's remaining seconds — same constant window the backend sends. */
export function Countdown({ seconds, prefix = '待确认', bare = false }) {
  const [left, setLeft] = useState(seconds ?? 0)

  useEffect(() => setLeft(seconds ?? 0), [seconds])
  useEffect(() => {
    if (left <= 0) return undefined
    const t = setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000)
    return () => clearInterval(t)
  }, [left > 0])

  if (seconds === null || seconds === undefined) return null
  if (left <= 0) {
    return bare
      ? <span className="bare-status warn">已超时，等待系统处理</span>
      : <span className="pill warn">已超时，等待系统处理</span>
  }

  const h = Math.floor(left / 3600)
  const m = Math.floor((left % 3600) / 60)
  const s = left % 60
  const text = h > 0 ? `${h} 小时 ${m} 分` : m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
  if (bare) return <span className={`bare-status ${h < 3 ? 'warn' : ''}`}>{prefix} {text}</span>
  return <span className={`pill ${h < 3 ? 'warn' : 'grey'}`}>{prefix} {text}</span>
}

export function Modal({ title, children, onClose, actions }) {
  // Desktop accessibility: Esc closes; the mask centers on the browser viewport (see .mask's position: fixed)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return (
    <div className="mask" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <div className="tiny muted" style={{ marginBottom: 14 }}>{children}</div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>{actions}</div>
      </div>
    </div>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Toast({ message, tone = 'info' }) {
  if (!message) return null
  return <div className={`notice ${tone}`} style={{ margin: '0 0 12px' }}>{message}</div>
}

const STATUS_TEXT = {
  selling: ['在售', 'ok'],
  reserved: ['已被预订', 'warn'],
  sold_out: ['已成交', 'grey'],
  expired: ['已过期', 'grey'],
}

export function StatusPill({ status, bare = false }) {
  const [text, tone] = STATUS_TEXT[status] || [status, 'grey']
  if (bare) return <span className={`bare-status ${tone}`}>{text}</span>
  return <span className={`pill ${tone}`}>{text}</span>
}

export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: '' })
  const reload = async () => {
    setState((s) => ({ ...s, loading: true }))
    try {
      const data = await fn()
      setState({ loading: false, data, error: '' })
    } catch (e) {
      setState({ loading: false, data: null, error: e.message })
    }
  }
  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { ...state, reload, setData: (data) => setState((s) => ({ ...s, data })) }
}
