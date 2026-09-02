import { useState } from 'react'
import { api, setToken } from '../api'

export default function ProfileForm({ user, onUpdated, onLogout }) {
  const [profile, setProfile] = useState({
    real_name: user.real_name || '',
    phone: '',
    id_card: '',
    email: '',
  })
  const [pwd, setPwd] = useState({ old_password: '', new_password: '', confirm: '' })
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState('')

  async function saveProfile(e) {
    e.preventDefault()
    if (busy) return
    setBusy('profile')
    setMsg(null)
    try {
      // 只提交填了内容的字段。手机号展示是脱敏的，留空表示不修改，
      // 否则会把 139****5678 这种掩码串当成真号码存进去。
      const payload = {}
      Object.entries(profile).forEach(([k, v]) => {
        if (v.trim()) payload[k] = v.trim()
      })
      if (Object.keys(payload).length === 0) {
        setMsg({ ok: false, text: '没有需要保存的修改' })
        return
      }
      const data = await api.updateProfile(payload)
      onUpdated(data.user)
      setProfile({ real_name: data.user.real_name || '', phone: '',
                   email: '', id_card: '' })
      setMsg({ ok: true, text: '资料已保存' })
    } catch (err) {
      setMsg({ ok: false, text: err.message })
    } finally {
      setBusy('')
    }
  }

  async function changePassword(e) {
    e.preventDefault()
    if (busy) return
    if (pwd.new_password !== pwd.confirm) {
      setMsg({ ok: false, text: '两次输入的新密码不一致' })
      return
    }
    setBusy('pwd')
    setMsg(null)
    try {
      await api.changePassword(pwd.old_password, pwd.new_password)
      setMsg({ ok: true, text: '密码已修改，请重新登录' })
      setPwd({ old_password: '', new_password: '', confirm: '' })
      // 令牌是无状态的，改密后旧令牌技术上仍有效。
      // 主动登出，避免用户误以为旧会话已经失效。
      setTimeout(() => {
        setToken('')
        onLogout()
      }, 1200)
    } catch (err) {
      setMsg({ ok: false, text: err.message })
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      {msg && (
        <div className={`reply ${msg.ok ? 'created' : 'failed'}`}>{msg.text}</div>
      )}

      <div className="section-title">基本资料</div>
      <form onSubmit={saveProfile}>
        <label className="fld">
          <span>姓名</span>
          <input
            value={profile.real_name}
            onChange={(e) => setProfile({ ...profile, real_name: e.target.value })}
          />
        </label>
        <label className="fld">
          <span>手机号（当前 {user.phone || '未填写'}）</span>
          <input
            value={profile.phone}
            onChange={(e) => setProfile({ ...profile, phone: e.target.value })}
            placeholder="留空表示不修改"
          />
        </label>
        <label className="fld">
          <span>
            身份证号（当前 {user.id_card || '未实名'}）
          </span>
          <input
            value={profile.id_card}
            onChange={(e) => setProfile({ ...profile, id_card: e.target.value })}
            placeholder="签约需要实名，留空表示不修改"
            maxLength={18}
          />
          <span className="fld-hint">
            系统<b>只保存脱敏后的号码与一个不可逆指纹</b>，完整号码不会入库
            ——它的用途只有展示与查重，两者都不需要还原原文。
          </span>
        </label>
        <label className="fld">
          <span>邮箱</span>
          <input
            value={profile.email}
            onChange={(e) => setProfile({ ...profile, email: e.target.value })}
            placeholder="留空表示不修改"
          />
        </label>
        <button className="btn block" type="submit" disabled={busy === 'profile'}>
          {busy === 'profile' ? '保存中…' : '保存资料'}
        </button>
      </form>

      <div className="section-title">修改密码</div>
      <form onSubmit={changePassword}>
        <label className="fld">
          <span>原密码</span>
          <input
            type="password"
            value={pwd.old_password}
            onChange={(e) => setPwd({ ...pwd, old_password: e.target.value })}
          />
        </label>
        <label className="fld">
          <span>新密码（至少 6 位）</span>
          <input
            type="password"
            value={pwd.new_password}
            onChange={(e) => setPwd({ ...pwd, new_password: e.target.value })}
          />
        </label>
        <label className="fld">
          <span>确认新密码</span>
          <input
            type="password"
            value={pwd.confirm}
            onChange={(e) => setPwd({ ...pwd, confirm: e.target.value })}
          />
        </label>
        <button
          className="btn block"
          type="submit"
          disabled={busy === 'pwd' || !pwd.old_password || !pwd.new_password}
        >
          {busy === 'pwd' ? '提交中…' : '修改密码'}
        </button>
      </form>
    </>
  )
}
