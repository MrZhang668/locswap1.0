import { useState } from 'react'
import { api, setToken } from '../api'
import { Toast } from '../components/common'

export default function Login({ onDone }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('buyer@campus.edu')
  const [password, setPassword] = useState('demo1234')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const res = mode === 'login' ? await api.login(email, password) : await api.register(email, password)
      setToken(res.token)
      onDone(res.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div>
        <div className="logo">LocSwap</div>
        <div className="muted tiny" style={{ marginTop: 6, marginBottom: 26 }}>
          校园二手市场 · 一句话就能找到你要的东西
        </div>

        <div className="seg" style={{ margin: '0 0 18px' }}>
          <button className={mode === 'login' ? 'active' : ''} onClick={() => setMode('login')}>登录</button>
          <button className={mode === 'register' ? 'active' : ''} onClick={() => setMode('register')}>注册</button>
        </div>

        <Toast message={error} tone="err" />

        <form onSubmit={submit}>
          <div className="field">
            <label>邮箱</label>
            <input className="input" type="email" value={email} autoComplete="username"
                   onChange={(e) => setEmail(e.target.value)} placeholder="you@campus.edu" required />
          </div>
          <div className="field">
            <label>密码</label>
            <input className="input" type="password" value={password} autoComplete="current-password"
                   onChange={(e) => setPassword(e.target.value)} placeholder="至少 6 位" required minLength={6} />
          </div>
          <button className="btn block" disabled={busy}>
            {busy ? <span className="spin" /> : mode === 'login' ? '登录' : '注册并进入'}
          </button>
        </form>

        <div className="divider" style={{ marginTop: 22 }} />
        <div className="mini muted">
          演示账号（已预置，密码都是 demo1234）：<br />
          买家：buyer@campus.edu<br />
          卖家：seller001@campus.edu ~ seller120@campus.edu（完整数据集 seed_mock.py）<br />
          　　　seller1@campus.edu ~ seller3@campus.edu（小数据集 seed.py）
          <br /><br />
          演示级别的登录：密码哈希 + HMAC 签名令牌；没有找回密码，也没有验证码。
        </div>
      </div>
    </>
  )
}
