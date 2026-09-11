import { useState } from 'react'
import { api, setToken } from '../api'

/**
 * 登录 / 注册 / 找回密码，独立整页（G1）。
 *
 * 原先是一个 460px 的弹窗。弹窗的问题不在于难看，在于**它把
 * 「你是谁」当成了一次打断**：用户点收藏、点预约，被一个盖在页面上的
 * 框拦住，填完还得再点一次原来那个按钮。
 *
 * 改成整页之后：
 *   - 地址栏有 `#/auth/login`，能分享、能收藏、后退键可用；
 *   - 左边有空间讲清楚「注册能得到什么」——弹窗里塞不下这些；
 *   - 注册表单有五个字段加一个身份选择，弹窗里要滚动才填得完。
 *
 * 三种模式共用一个组件而不是三个页面：它们共享同一套输入框样式、
 * 同一套错误处理、同一个「成功后去哪」的逻辑。拆三份只会让
 * 「改一处忘了另外两处」变成常态。
 */

const ROLES = [
  { key: 'tenant', label: '我要租房', desc: '找房、预约看房、签约缴费' },
  { key: 'landlord', label: '我要出租', desc: '发布房源、收租、处理报修' },
  { key: 'agent', label: '我是经纪人', desc: '接受委托、带看、赚取佣金' },
]

/** 注册后需要审核的角色。见 D1。 */
const NEEDS_REVIEW = new Set(['landlord', 'agent'])

const SELLING_POINTS = [
  ['看得见的条件', '整句需求自动转成筛选条件，页面上可直接修改'],
  ['从签到退有据可查', '合同生效即排出整期账单，每笔缴费都有流水与凭证'],
  ['房源经过核验', '产权凭证由管理员人工审核，未核验的房源会明确标注'],
]

export default function AuthPage({ mode = 'login', onDone, onSwitch, onBack }) {
  const [form, setForm] = useState({
    username: '', password: '', confirm: '',
    real_name: '', phone: '', role: 'tenant',
    // B1：重置用的一次性验证码。它不是密码，所以不做 6–64 位那套校验
    code: '',
  })
  const [errs, setErrs] = useState({})
  const [top, setTop] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState('')

  const set = (k) => (e) => {
    setForm((f) => ({ ...f, [k]: e.target.value }))
    // 改哪一项就清哪一项的错，别让用户对着一个已经改好的字段
    // 还看着红字
    setErrs((p) => (p[k] ? { ...p, [k]: '' } : p))
  }

  /** 逐项校验。**一次把全部问题标出来**，不是报完第一个就停——
   *  一个一个报会让用户来回提交五次。 */
  function validate() {
    const e = {}
    if (mode !== 'reset') {
      const u = form.username.trim()
      if (!u) e.username = '请输入用户名'
      else if (!/^[A-Za-z0-9_]{3,20}$/.test(u)) e.username = '需 3–20 位字母、数字或下划线'
    }

    if (!form.password) e.password = '请输入密码'
    else if (form.password.length < 6 || form.password.length > 64) e.password = '密码需 6–64 位'
    else if (/\s/.test(form.password)) e.password = '密码不能包含空格'

    if (mode === 'reset') {
      if (!form.code.trim()) e.code = '请粘贴邮件里的验证码'
      if (form.confirm !== form.password) e.confirm = '两次输入的密码不一致'
    }

    if (mode === 'register') {
      if (form.confirm !== form.password) e.confirm = '两次输入的密码不一致'
      const n = form.real_name.trim()
      if (!n) e.real_name = '请输入真实姓名'
      else if (!/^[A-Za-z一-鿿· ]{2,20}$/.test(n)) e.real_name = '需 2–20 位中英文'
      if (!/^\d{11}$/.test(form.phone.trim())) e.phone = '请输入 11 位手机号'
    }
    setErrs(e)
    return Object.keys(e).length === 0
  }

  async function submit(ev) {
    ev.preventDefault()
    setTop('')
    if (mode !== 'forgot' && !validate()) return
    setBusy(true)
    try {
      if (mode === 'login') {
        const d = await api.login(form.username.trim(), form.password)
        setToken(d.token)
        onDone?.(d.user)
      } else if (mode === 'register') {
        const d = await api.register({
          username: form.username.trim(),
          password: form.password,
          real_name: form.real_name.trim(),
          phone: form.phone.trim(),
          role: form.role,
        })
        setToken(d.token)
        // D1：房东与经纪人需要审核。注册照常成功、也照常登录，
        // 但在获批之前只有租客的权限——**这件事必须当场说清楚**，
        // 不然用户会以为发布房源的入口坏了。
        if (NEEDS_REVIEW.has(form.role)) {
          setDone(form.role === 'landlord'
            ? '注册成功。房东身份需管理员审核，通过后即可发布房源；审核期间可先用租客身份找房。'
            : '注册成功。经纪人身份需要管理员审核，通过后即可接受委托。')
          setTimeout(() => onDone?.(d.user), 2600)
        } else {
          onDone?.(d.user)
        }
      } else if (mode === 'reset') {
        // B1：真正把密码改掉的那一步。此前只有「申请」没有「完成」，
        // 用户收到验证码之后**没有任何地方可以用它**
        await api.resetPassword(form.code.trim(), form.password)
        setDone('密码已重置，正在回到登录页…')
        setTimeout(() => onSwitch('login'), 1800)
      } else {
        await api.forgotPassword(form.username.trim())
        // 不论账号存不存在都给同一句话——区别对待等于给了别人
        // 一个枚举账号的接口
        setDone('如果该账号存在且绑定了邮箱，重置验证码已经发出，请查收邮件。'
                + '收到后点下面的「我有验证码」继续。')
      }
    } catch (err) {
      setTop(err.message || '操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const title = { login: '登录', register: '注册', forgot: '找回密码',
                  reset: '设置新密码' }[mode]

  return (
    <div className="auth-page">
      {/* 左半边讲「为什么要有账号」。弹窗里塞不下这些，
          于是注册就成了一件纯粹的手续。 */}
      <aside className="auth-aside">
        {/* E3：返回改成一个圆圈里的 ‹。
            原来是「‹ 返回首页」的描边胶囊，压在左上角的背景图上，
            四个字加一个框，比它旁边的站名还显眼——**返回是退路，
            不该抢主视觉**。 */}
        <button className="auth-back" onClick={onBack}
                aria-label="返回首页" title="返回首页">‹</button>
        <div className="auth-brand">大连<span>智租</span></div>
        <p className="auth-slogan">基于 AI 智能体的房屋租赁平台</p>
        <ul className="auth-points">
          {SELLING_POINTS.map(([k, v]) => (
            <li key={k}>
              <b>{k}</b>
              <span>{v}</span>
            </li>
          ))}
        </ul>

        {/* E3：左半边原来只填了上半屏，下面空掉一大片。
            补一条脚注把这一列**撑到底**——顺便把「这是课程作品」
            这句必须说的话放在了注册按钮看得见的地方，
            而不是等用户填完信息才在页脚发现。 */}
        <p className="auth-foot-note">
          大连理工大学 实习实训课程设计作品 · 房源与交易数据均为演示用途
        </p>
      </aside>

      <main className="auth-main">
        <div className="auth-box">
          <h1 className="auth-title">{title}</h1>
          <p className="auth-sub">
            {mode === 'login' && '登录后可收藏房源、预约看房、签约与缴费'}
            {mode === 'register' && '填写以下信息，注册后即可开始找房'}
            {mode === 'forgot' && '输入用户名，重置验证码会发到你绑定的邮箱'}
            {mode === 'reset' && '粘贴邮件里的验证码，然后设置新密码'}
          </p>

          {top && <div className="auth-alert">{top}</div>}
          {done && <div className="auth-ok">{done}</div>}

          <form onSubmit={submit} noValidate>
            {/* B1：找回密码这一屏原来要的是**手机号**，而后端是按
                用户名查的（`User.username`）——填手机号永远匹配不上，
                于是无论填什么都得到同一句「如果该账号存在…」，
                用户以为是自己记错了账号。 */}
            {mode !== 'reset' && (
              <Field label="用户名" err={errs.username}>
                <input value={form.username} onChange={set('username')}
                       autoComplete="username" placeholder="3–20 位字母、数字或下划线" />
              </Field>
            )}

            {mode === 'reset' && (
              <Field label="验证码" err={errs.code}>
                <input value={form.code} onChange={set('code')}
                       autoComplete="one-time-code"
                       placeholder="粘贴邮件里的验证码" />
              </Field>
            )}

            {mode !== 'forgot' && (
              <Field label={mode === 'reset' ? '新密码' : '密码'} err={errs.password}>
                <input type="password" value={form.password} onChange={set('password')}
                       autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                       placeholder="6–64 位，不能有空格" />
              </Field>
            )}

            {mode === 'reset' && (
              <Field label="确认新密码" err={errs.confirm}>
                <input type="password" value={form.confirm} onChange={set('confirm')}
                       autoComplete="new-password" placeholder="再输入一次" />
              </Field>
            )}

            {mode === 'register' && (
              <>
                <Field label="确认密码" err={errs.confirm}>
                  <input type="password" value={form.confirm} onChange={set('confirm')}
                         autoComplete="new-password" placeholder="再输入一次" />
                </Field>
                <Field label="真实姓名" err={errs.real_name}>
                  <input value={form.real_name} onChange={set('real_name')}
                         placeholder="签约时要用，请填真实姓名" />
                </Field>
              </>
            )}

            {mode === 'register' && (
              <Field label="手机号" err={errs.phone}>
                <input value={form.phone} onChange={set('phone')}
                       inputMode="numeric" placeholder="11 位手机号" />
              </Field>
            )}

            {mode === 'register' && (
              <div className="auth-field">
                <label>我的身份</label>
                <div className="role-pick">
                  {ROLES.map((r) => (
                    <button
                      type="button"
                      key={r.key}
                      className={`role-card${form.role === r.key ? ' on' : ''}`}
                      onClick={() => setForm((f) => ({ ...f, role: r.key }))}
                    >
                      <b>{r.label}</b>
                      <span>{r.desc}</span>
                      {NEEDS_REVIEW.has(r.key) && <em>需管理员审核</em>}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <button className="btn auth-submit" type="submit" disabled={busy}>
              {busy ? '处理中…' : title}
            </button>
          </form>

          <div className="auth-foot">
            {mode === 'login' && (
              <>
                <button className="link" onClick={() => onSwitch('register')}>注册一个</button>
                <span>·</span>
                <button className="link" onClick={() => onSwitch('forgot')}>忘记密码</button>
              </>
            )}
            {mode === 'forgot' && (
              <>
                {/* 验证码走邮箱，用户要离开页面去收。回来时得有个地方
                    把它填进去——没有这个入口，整条找回流程就断在半路 */}
                <button className="link" onClick={() => onSwitch('reset')}>
                  我有验证码
                </button>
                <span>·</span>
              </>
            )}
            {mode !== 'login' && (
              <button className="link" onClick={() => onSwitch('login')}>返回登录</button>
            )}
          </div>

          <div className="demo-accounts">
            演示账号：tenant1 / test123（租客）·
            landlord1 / test123（房东）· admin / admin123（管理员）
          </div>
        </div>
      </main>
    </div>
  )
}

function Field({ label, err, children }) {
  return (
    <div className={`auth-field${err ? ' bad' : ''}`}>
      <label>{label}</label>
      {children}
      {/* 错误就地显示在字段下方，不汇总到顶部——
          汇总的话用户还得自己找是哪一栏出了问题 */}
      {err && <p className="auth-err">{err}</p>}
    </div>
  )
}
