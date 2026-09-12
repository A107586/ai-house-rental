import { useState } from 'react'
import { api, setToken } from '../api'
import BecomeLandlord from './BecomeLandlord'

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

  // 免打扰（J-07）单独走一次即时保存，**不并进上面那个表单**。
  //
  // 那个表单的规则是「只提交填了内容的字段」（因为手机号显示的是掩码，
  // 留空表示不改）。一个布尔开关放进去，关掉时值是 false，
  // 会被 `v.trim()` 那一行当成空值直接丢掉——**开得了、关不掉**。
  async function toggleContact(next) {
    if (busy) return
    setBusy('contact')
    setMsg(null)
    try {
      const data = await api.updateProfile({ accept_contact: next })
      onUpdated(data.user)
      setMsg({ ok: true,
               text: next ? '已允许经纪人主动联系你'
                          : '已设为免打扰。经纪人不能再主动发起会话，'
                            + '但你的预约和带看通知照常送达。' })
    } catch (err) {
      setMsg({ ok: false, text: err.message })
    } finally {
      setBusy('')
    }
  }

  async function logoutEverywhere() {
    if (busy) return
    setBusy('logout')
    setMsg(null)
    try {
      await api.logoutAll()
      setMsg({ ok: true, text: '所有设备已登出，请重新登录' })
      // 当前这条令牌也在作废之列，留在页面上只会一路 401
      setTimeout(() => {
        setToken('')
        onLogout()
      }, 1200)
    } catch (err) {
      setMsg({ ok: false, text: err.message })
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
      setMsg({ ok: true, text: '密码已修改，其他设备已被登出，请重新登录' })
      setPwd({ old_password: '', new_password: '', confirm: '' })
      // 改密码会把此前签发的令牌全部作废（含当前这条），
      // 所以这里必须主动登出——继续留在页面上只会一路 401。
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

      <div className="section-title">免打扰</div>
      <label className="fld inline">
        <input
          type="checkbox"
          checked={user.accept_contact !== false}
          disabled={busy === 'contact'}
          onChange={(e) => toggleContact(e.target.checked)}
        />
        <span>允许经纪人主动联系我</span>
      </label>
      <p className="fld-hint">
        关掉之后，受托经纪人不能再主动发起会话。
        <b>你自己发起的咨询、以及预约与带看的通知不受影响</b>免打扰挡的是别人主动来，不是把你自己也关在外面。
      </p>

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

      <div className="section-title">登录设备</div>
      <p className="desc">
        手机丢了，或在别人电脑上登录过忘了退出，可以把所有设备一次性登出。<b>不需要改密码</b>只想下线的人，不该被迫换掉自己记得住的密码。
      </p>
      <button
        className="btn ghost block"
        disabled={busy === 'logout'}
        onClick={logoutEverywhere}
      >
        {busy === 'logout' ? '处理中…' : '退出所有设备'}
      </button>

      {/* 身份升级入口（K-13）。放在账号设置里而不是首页某个角落——
          「我也要出租房子」是一次关于账号本身的决定，
          和改密码、管设备是同一类事 */}
      <BecomeLandlord onUpgraded={onUpdated} />
    </>
  )
}
