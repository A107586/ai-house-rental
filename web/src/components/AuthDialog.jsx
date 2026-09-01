import { useState } from 'react'
import { api, setToken } from '../api'

export default function AuthDialog({ onClose, onLoggedIn }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({
    username: '', password: '', real_name: '', phone: '', role: 'tenant',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    if (busy) return

    setBusy(true)
    setError('')
    try {
      const data =
        mode === 'login'
          ? await api.login(form.username, form.password)
          : await api.register(form)
      setToken(data.token)
      onLoggedIn(data.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="dialog">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <h3>{mode === 'login' ? '登录' : '注册'}</h3>

        <form onSubmit={submit}>
          <div className="field">
            <input
              placeholder="用户名"
              value={form.username}
              onChange={update('username')}
              autoFocus
            />
          </div>
          <div className="field">
            <input
              type="password"
              placeholder={mode === 'login' ? '密码' : '密码（至少 6 位）'}
              value={form.password}
              onChange={update('password')}
            />
          </div>

          {mode === 'register' && (
            <>
              <div className="field">
                <input
                  placeholder="姓名"
                  value={form.real_name}
                  onChange={update('real_name')}
                />
                <input
                  placeholder="手机号"
                  value={form.phone}
                  onChange={update('phone')}
                />
              </div>
              <div className="field">
                <select value={form.role} onChange={update('role')}>
                  <option value="tenant">我是租客</option>
                  <option value="landlord">我是房东</option>
                </select>
              </div>
            </>
          )}

          {error && <div className="reply failed">{error}</div>}

          <button className="btn block" type="submit" disabled={busy}>
            {busy ? '处理中…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>

        <div className="dialog-foot">
          {mode === 'login' ? (
            <>
              还没有账号？
              <button className="link" onClick={() => { setMode('register'); setError('') }}>
                注册一个
              </button>
            </>
          ) : (
            <>
              已有账号？
              <button className="link" onClick={() => { setMode('login'); setError('') }}>
                去登录
              </button>
            </>
          )}
        </div>

        {mode === 'login' && (
          <div className="demo-accounts">
            演示账号：<code>tenant1 / test123</code>（租客）·{' '}
            <code>landlord1 / test123</code>（房东）·{' '}
            <code>admin / admin123</code>（管理员）
          </div>
        )}
      </div>
    </>
  )
}
