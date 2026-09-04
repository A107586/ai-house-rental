import { useState } from 'react'
import { api } from '../api'

/**
 * 找回密码（K-11 前端）。
 *
 * 两步在同一个界面里完成：申请 → 填验证码和新密码。
 * **不做成两个页面**，因为用户拿到验证码时是从邮箱切回来的，
 * 让他重新找一遍入口是多余的一步。
 *
 * 有两处措辞是刻意的：
 *
 * - 申请成功后显示的是「**如果该账号存在**…」。后端对存在与不存在的
 *   账号返回同一句话，前端也必须照样说——在这里补一句「该用户不存在」
 *   就把后端费劲藏起来的信息又漏了出去。
 * - 密码规则在提交前就校验一遍。让人去邮箱翻出验证码、填完才被告知
 *   「密码太短」，那趟邮箱就白跑了。
 */
export default function ForgotPasswordForm({ onDone, onBack }) {
  const [step, setStep] = useState('ask')
  const [username, setUsername] = useState('')
  const [token, setToken] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function ask(e) {
    e.preventDefault()
    if (busy) return
    if (!username.trim()) return setError('请输入用户名')
    setBusy(true)
    setError('')
    try {
      await api.forgotPassword(username.trim())
      setStep('reset')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function reset(e) {
    e.preventDefault()
    if (busy) return
    if (!token.trim()) return setError('请填写邮件里的验证码')
    // 先在本地校验一遍，别让人白跑一趟邮箱
    if (password.length < 6 || password.length > 64) {
      return setError('密码长度需为 6–64 位')
    }
    if (/\s/.test(password)) return setError('密码不能包含空格')

    setBusy(true)
    setError('')
    try {
      await api.resetPassword(token.trim(), password)
      onDone('密码已重置，请用新密码登录。')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h3>找回密码</h3>

      {step === 'ask' ? (
        <form onSubmit={ask}>
          <p className="desc">
            输入用户名，我们会把重置验证码发到该账号绑定的邮箱。
          </p>
          <div className="field">
            <input
              placeholder="用户名"
              maxLength={20}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
          </div>

          {error && <div className="reply failed">{error}</div>}

          <button className="btn block" type="submit" disabled={busy}>
            {busy ? '发送中…' : '发送重置验证码'}
          </button>
        </form>
      ) : (
        <form onSubmit={reset}>
          {/* 说法和后端保持一致：不确认这个账号是否存在 */}
          <div className="reply created">
            如果该账号存在且绑定了邮箱，验证码已经发出，请查收邮件。
            验证码 30 分钟内有效。
          </div>

          <div className="field">
            <input
              placeholder="邮件里的验证码"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoFocus
            />
          </div>
          <div className="field">
            <input
              type="password"
              placeholder="新密码（至少 6 位）"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && <div className="reply failed">{error}</div>}

          <button className="btn block" type="submit" disabled={busy}>
            {busy ? '提交中…' : '设置新密码'}
          </button>

          <div className="fld-hint">
            重置成功后，此前在所有设备上的登录都会失效，需要重新登录。
          </div>
        </form>
      )}

      <div className="dialog-foot">
        <button className="link" onClick={onBack}>返回登录</button>
      </div>
    </>
  )
}
