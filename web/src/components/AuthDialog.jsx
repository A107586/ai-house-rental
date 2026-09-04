import { useState } from 'react'
import { api, setToken } from '../api'
import ForgotPasswordForm from './ForgotPasswordForm'

export default function AuthDialog({ onClose, onLoggedIn }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({
    username: '', password: '', real_name: '', phone: '', role: 'tenant',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // K-11：密码忘了原先是真的进不去，只能找管理员改
  const [notice, setNotice] = useState('')

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    if (busy) return

    const username = form.username.trim()
    const realName = form.real_name.trim()
    const phone = form.phone.trim()
    if (!username) return setError('请输入用户名')
    if (!form.password) return setError('请输入密码')
    if (mode === 'register') {
      if (!/^[A-Za-z0-9_]{3,20}$/.test(username)) {
        return setError('用户名需为 3–20 位字母、数字或下划线')
      }
      if (form.password.length < 6 || form.password.length > 64) {
        return setError('密码长度需为 6–64 位')
      }
      if (/\s/.test(form.password)) return setError('密码不能包含空格')
      if (realName && !/^[A-Za-z\u4e00-\u9fff· ]{2,20}$/.test(realName)) {
        return setError('姓名需为 2–20 位中英文字符')
      }
      if (phone && !/^1[3-9]\d{9}$/.test(phone)) {
        return setError('请输入正确的 11 位手机号')
      }
    }

    setBusy(true)
    setError('')
    try {
      const data =
        mode === 'login'
          ? await api.login(username, form.password)
          : await api.register({ ...form, username, real_name: realName, phone })
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
        {mode === 'forgot' ? (
          <ForgotPasswordForm
            onBack={() => { setMode('login'); setError('') }}
            onDone={(message) => {
              setMode('login')
              setError('')
              setNotice(message)
            }}
          />
        ) : (
        <>
        <h3>{mode === 'login' ? '登录' : '注册'}</h3>

        {notice && <div className="reply created">{notice}</div>}

        <form onSubmit={submit}>
          <div className="field">
            <input
              placeholder="用户名"
              maxLength={20}
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
              maxLength={64}
            />
          </div>

          {mode === 'register' && (
            <>
              <div className="field">
                <input
                  placeholder="姓名"
                  maxLength={20}
                  value={form.real_name}
                  onChange={update('real_name')}
                />
                <input
                  placeholder="手机号"
                  value={form.phone}
                  onChange={update('phone')}
                  inputMode="numeric"
                  maxLength={11}
                />
              </div>
              <div className="field">
                <select value={form.role} onChange={update('role')}>
                  <option value="tenant">我是租客</option>
                  <option value="landlord">我是房东</option>
                  <option value="agent">我是经纪人</option>
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
              <span className="foot-sep">·</span>
              <button className="link" onClick={() => { setMode('forgot'); setError('') }}>
                忘记密码
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
        </>
        )}
      </div>
    </>
  )
}
