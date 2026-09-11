import { useEffect, useState } from 'react'
import { api } from '../api'
import AgentQueue from './AgentQueue'
import AgentRoster from './AgentRoster'
import ConfigPanel from './ConfigPanel'
import FlagPanel from './FlagPanel'
import FocusBar from './FocusBar'
import HouseReportPanel from './HouseReportPanel'
import LandlordRoster from './LandlordRoster'
import RepairDataPanel from './RepairDataPanel'
import RoleRequestPanel from './RoleRequestPanel'
import LogPanel from './LogPanel'
import ReportPanel from './ReportPanel'
import SupportPanel from './SupportPanel'

const ROLE_LABEL = {
  tenant: '租客', landlord: '房东', agent: '经纪人', admin: '管理员',
}

const CONTRACT_STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  active: { label: '生效中', cls: 'status-confirmed' },
  ended: { label: '已结束', cls: 'status-cancelled' },
  cancelled: { label: '已取消', cls: 'status-rejected' },
}

const APPOINTMENT_STATUS = {
  pending: '待确认', confirmed: '已确认',
  rejected: '已拒绝', cancelled: '已取消',
}

export default function AdminPanel({ user, onClose, embedded,
                                    onSections, activeTab, onTabChange }) {
  // 嵌入整页后台时当前页签由左栏说了算；独立抽屉时自己管。
  // **两套状态不能并存**——并存会出现「左栏高亮 A、正文显示 B」，
  // 而这种不一致要切换几次之后才显形，最难查。
  //
  // 进来第一屏是待审队列，不是看板（F5）
  // 管理员签发的一次性重置码。**只放在内存里，不落 state 之外的任何地方**——
  // 它是一把临时钥匙，不该被写进 localStorage 或 URL
  const [resetCode, setResetCode] = useState(null)
  const [ownTab, setOwnTab] = useState('review')
  const tab = embedded && activeTab ? activeTab : ownTab
  const setTab = (key) => {
    if (embedded && onTabChange) onTabChange(key)
    else setOwnTab(key)
  }
  const [stats, setStats] = useState(null)
  const [pending, setPending] = useState([])
  const [users, setUsers] = useState([])
  const [contracts, setContracts] = useState([])
  const [appointments, setAppointments] = useState([])
  const [repairs, setRepairs] = useState([])
  const [notices, setNotices] = useState([])
  const [draft, setDraft] = useState({ title: '', content: '', category: '公告' })
  const [keyword, setKeyword] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')
  // 只为了页签上那个待审角标。中介档案页自己会再取一次完整口径，
  // 但角标必须在打开后台的第一屏就出现——待审的东西藏在页签里没人点
  const [agentPending, setAgentPending] = useState(0)
  // 同理：角色升级申请与内容审核的待办数，也得在第一屏就看得见
  const [rolePending, setRolePending] = useState(0)
  const [flagPending, setFlagPending] = useState(0)

  async function load() {
    setError('')
    try {
      const [s, p, u, c, a, r, n] = await Promise.all([
        api.stats(),
        api.adminHouses({ status: 'pending' }),
        api.adminUsers({ keyword }),
        api.adminContracts(),
        api.adminAppointments(),
        api.listRepairs(),
        api.adminNotices(),
      ])
      setStats(s)
      setPending(p.list || [])
      setUsers(u.list || [])
      setContracts(c.list || [])
      setAppointments(a.list || [])
      setRepairs(r.list || [])
      setNotices(n.list || [])
    } catch (err) {
      setError(err.message)
    }
    // 中介口径单独取，且**不并进上面那个 Promise.all**：
    // 这是后加的功能，跑在旧数据库上时表可能还没建全，
    // 让它把整个后台首屏一起拖挂不值得
    try {
      const a = await api.adminAgentStats()
      setAgentPending(a.license_pending || 0)
    } catch {
      setAgentPending(0)
    }
    // 同上，各自 catch。**一个角标取不到不该拖垮另一个**——
    // 三个并进一个 Promise.all，任何一条挂了就三个数字全没了
    try {
      const r = await api.adminRoleRequests('pending')
      setRolePending((r.list || []).length)
    } catch {
      setRolePending(0)
    }
    try {
      const f = await api.adminFlags('pending')
      setFlagPending((f.list || []).length)
    } catch {
      setFlagPending(0)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function run(key, fn, message = '') {
    if (busy) return
    setBusy(key)
    setError('')
    setOk('')
    try {
      await fn()
      if (message) setOk(message)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  function reject(houseNo) {
    const reason = window.prompt('驳回理由（会通知房东）', '房源描述与实际不符')
    if (reason === null) return
    run(houseNo, () => api.reviewHouse(houseNo, false, reason), '已驳回并通知房东。')
  }

  // 月度收入柱状图的最大值，用于算每根柱子的高度
  const maxIncome = Math.max(
    1, ...(stats?.monthly_income || []).map((m) => m.amount))

  const openRepairs = repairs.filter((r) => r.status !== 'done').length

  // F5：十六个页签平铺时，「系统配置」和「房源审核」看着一样重。
  // 按**这件事有没有人在等**分三组：
  //
  //   审核与治理  有人被卡住了，管理员不动他就一直卡着
  //   运营与数据  没人等，是自己想看才看的
  //   系统        很少动，动一次影响所有人
  //
  // 顺序也跟着换了：原来第一屏是数据看板，管理员进来先看到一堆
  // 好看的数字，待审队列在第三个。**后台是干活的地方，不是仪表盘。**
  const TABS = [
    { key: 'review', label: '房源审核', n: pending.length, group: '审核与治理' },
    { key: 'agentQueue', label: '中介资质', n: agentPending, group: '审核与治理' },
    { key: 'roleRequests', label: '身份升级', n: rolePending, group: '审核与治理' },
    { key: 'houseReports', label: '举报处理', group: '审核与治理' },
    { key: 'flags', label: '内容审核', n: flagPending, group: '审核与治理' },
    { key: 'repairs', label: '工单', n: openRepairs, group: '审核与治理' },
    { key: 'dashboard', label: '数据看板', group: '运营与数据' },
    { key: 'report', label: '经营分析', group: '运营与数据' },
    { key: 'orders', label: '订单总览', n: contracts.length, group: '运营与数据' },
    { key: 'users', label: '用户管理', n: users.length, group: '运营与数据' },
    { key: 'landlords', label: '户主档案', group: '运营与数据' },
    { key: 'agents', label: '中介档案', group: '运营与数据' },
    { key: 'repairdata', label: '维修数据', group: '运营与数据' },
    { key: 'notices', label: '公告', n: notices.length, group: '系统' },
    { key: 'support', label: '智能客服', group: '系统' },
    { key: 'logs', label: '操作日志', group: '系统' },
    { key: 'config', label: '系统配置', group: '系统' },
  ]

  // 待办条只放**审核类**的四件事：它们的共同点是「有一个具体的人
  // 正在等这个结果」。工单也有人等，但等的是维修师傅上门，
  // 不是等管理员点一下——放进来会稀释这条的含义
  const FOCUS = [
    { key: 'review', label: '房源待审', n: pending.length, urgent: true,
      hint: '房东发布后卡在这' },
    { key: 'agentQueue', label: '资质待审', n: agentPending, urgent: true,
      hint: '中介不能接单' },
    { key: 'roleRequests', label: '身份升级待审', n: rolePending, urgent: true,
      hint: '申请人还是租客权限' },
    { key: 'flags', label: '内容待审', n: flagPending, hint: '命中敏感词的文本' },
    { key: 'repairs', label: '未完成工单', n: openRepairs, hint: '含处理中' },
  ]

  // 把页签清单（含计数）报给外面的左栏。
  //
  // **必须走 useEffect，不能在渲染里直接调**：那是在渲染子组件的过程中
  // 去改父组件的状态，React 会直接报错。依赖用序列化后的字符串——
  // 数组每次都是新对象，按引用比会无限重渲染。
  const sectionsKey = JSON.stringify(
    TABS.map((t) => [t.key, t.label, t.n || 0, t.group || '']))
  useEffect(() => {
    if (embedded && onSections) onSections(TABS)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [embedded, sectionsKey])

      {/* 嵌入模式：整页工作台接管了左栏与标题，这里只出正文。
          **没有把面板重写一遍**——每个页签下的内容都已经调好并在用，
          重写只会得到两套各自漂移的逻辑。要脱掉的只有抽屉那层壳。
          待办条留着：它是「现在该做什么」，和左栏的「有哪些功能」
          不是一回事。 */}
  return (
    <>
      {!embedded && <div className="drawer-mask" onClick={onClose} />}
      <div className={embedded ? 'wb-embed' : 'drawer wide'}>
        {!embedded && (
          <>
            <button className="drawer-close" onClick={onClose} aria-label="关闭">
              ×
            </button>
            <h3>管理后台</h3>
            <div className="loc">{user.real_name || user.username} · 管理员</div>
          </>
        )}

        <FocusBar items={FOCUS} onPick={setTab}
                  hint="这几项都有具体的人在等结果，压着不处理会一直卡着。" />

        {!embedded && (
        <div className="tabs scroll grouped">
          {TABS.map((item, i) => (
            <span className="tab-slot" key={item.key}>
              {item.group && item.group !== TABS[i - 1]?.group && (
                <span className="tab-group">{item.group}</span>
              )}
              <button
                className={`tab ${tab === item.key ? 'active' : ''}`}
                onClick={() => { setTab(item.key); setOk(''); setError('') }}
              >
                {item.label}
                {item.n > 0 && <span className="tab-n">{item.n}</span>}
              </button>
            </span>
          ))}
        </div>
        )}

        {error && <div className="reply failed">{error}</div>}
        {ok && <div className="reply created">{ok}</div>}

        {/* ---------------- 户主档案（F-02） ---------------- */}
        {tab === 'landlords' && <LandlordRoster />}

        {/* ---------------- 中介治理（J-11） ---------------- */}
        {tab === 'agentQueue' && <AgentQueue onReviewed={load} />}

        {/* D1 身份升级申请。规则在后端，批准的地方在这里 */}
        {tab === 'roleRequests' && <RoleRequestPanel onReviewed={load} />}

        {tab === 'agents' && <AgentRoster />}

        {/* ---------------- 内容审核（M-08） ---------------- */}
        {tab === 'support' && <SupportPanel />}

        {tab === 'flags' && <FlagPanel />}

        {tab === 'houseReports' && <HouseReportPanel />}

        {/* ---------------- 维修数据（F-09） ---------------- */}
        {tab === 'repairdata' && <RepairDataPanel />}

        {/* ---------------- 系统配置（M-09） ---------------- */}
        {tab === 'config' && <ConfigPanel />}

        {/* ---------------- 操作日志（M-07） ---------------- */}
        {tab === 'logs' && <LogPanel />}

        {/* ---------------- 经营分析（AI-05） ---------------- */}
        {tab === 'report' && <ReportPanel />}

        {/* ---------------- 数据看板 ---------------- */}
        {tab === 'dashboard' && stats && (
          <>
            <div className="dash-grid">
              <div className="dcell">
                <div className="dk">房源总数</div>
                <div className="dv">{stats.house_total}</div>
                <div className="dd">在租 {stats.house_rented} · 可租 {stats.house_available}</div>
              </div>
              <div className="dcell">
                <div className="dk">出租率</div>
                <div className="dv">{stats.occupancy_rate}%</div>
                <div className="dd">均价 {Math.round(stats.avg_rent)} 元</div>
              </div>
              <div className="dcell money">
                <div className="dk">已收租金</div>
                <div className="dv">{Math.round(stats.income_received)}</div>
                <div className="dd">待收 {Math.round(stats.income_pending)} 元</div>
              </div>
              <div className={`dcell ${stats.repair_urgent ? 'warn' : ''}`}>
                <div className="dk">紧急工单</div>
                <div className="dv">{stats.repair_urgent}</div>
                <div className="dd">待处理 {stats.repair_pending} 条</div>
              </div>
            </div>

            <div className="section-title">月度收租</div>
            {stats.monthly_income.length === 0 ? (
              <p className="desc">还没有收租记录。</p>
            ) : (
              <div className="bar-chart">
                {stats.monthly_income.map((m) => (
                  <div className="bar-item" key={m.month}>
                    <div className="bar-val">{Math.round(m.amount)}</div>
                    <div
                      className="bar-fill"
                      style={{ height: `${Math.max((m.amount / maxIncome) * 100, 4)}%` }}
                    />
                    <div className="bar-lbl">{m.month.slice(5)}月</div>
                  </div>
                ))}
              </div>
            )}

            <div className="section-title">各区房源分布</div>
            <ul className="dist-list">
              {stats.by_district.map((d) => (
                <li key={d.district}>
                  <span className="dist-name">{d.district}</span>
                  <span className="dist-bar">
                    <span
                      style={{
                        width: `${(d.count / stats.by_district[0].count) * 100}%`,
                      }}
                    />
                  </span>
                  <span className="dist-n">{d.count} 套</span>
                  <span className="dist-avg">均 {Math.round(d.avg_rent)}</span>
                </li>
              ))}
            </ul>

            <div className="section-title">其它</div>
            <div className="sum-row">
              <div className="sum">
                <div className="sum-k">生效合同</div>
                <div className="sum-v">{stats.contract_active}</div>
                <div className="sum-d">共 {stats.contract_total} 份</div>
              </div>
              <div className="sum">
                <div className="sum-k">逾期账单</div>
                <div className={`sum-v ${stats.overdue_count ? 'danger' : ''}`}>
                  {stats.overdue_count}
                </div>
                <div className="sum-d">笔</div>
              </div>
            </div>
          </>
        )}

        {/* ---------------- 房源审核 ---------------- */}
        {tab === 'review' &&
          (pending.length === 0 ? (
            <p className="desc">没有待审核的房源。房东发布或修改房源后会出现在这里。</p>
          ) : (
            <ul className="record-list">
              {pending.map((house) => (
                <li key={house.house_no}>
                  <div className="record-head">
                    <strong>{house.title}</strong>
                    <span className="pill status-pending">待审核</span>
                  </div>
                  <div className="record-body">
                    {house.house_no} · {house.district} {house.community} ·
                    <span className="amount"> {house.rent_price} 元</span> ·
                    {' '}{house.layout} · {Math.round(house.area)}㎡
                  </div>
                  <div className="record-body">房东：{house.landlord_name}</div>
                  {house.description && (
                    <div className="record-body desc-clip">{house.description}</div>
                  )}
                  <div className="record-actions">
                    <button
                      className="btn small"
                      disabled={busy === house.house_no}
                      onClick={() => run(house.house_no,
                        () => api.reviewHouse(house.house_no, true),
                        '已通过审核并上架。')}
                    >
                      通过
                    </button>
                    <button
                      className="btn ghost small"
                      disabled={busy === house.house_no}
                      onClick={() => reject(house.house_no)}
                    >
                      驳回
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ))}

        {/* ---------------- 订单总览 ---------------- */}
        {tab === 'orders' && (
          <>
            <div className="section-title">租约（{contracts.length}）</div>
            {contracts.length === 0 ? (
              <p className="desc">暂无租约。</p>
            ) : (
              <ul className="record-list">
                {contracts.map((c) => {
                  const st = CONTRACT_STATUS[c.status] || { label: c.status, cls: '' }
                  return (
                    <li key={c.contract_no}>
                      <div className="record-head">
                        <strong>{c.house_no}</strong>
                        <span className={`pill ${st.cls}`}>{st.label}</span>
                      </div>
                      <div className="record-body">
                        {c.contract_no} · {c.start_date} 至 {c.end_date} ·
                        <span className="amount"> {c.rent} 元/月</span>
                        {c.status === 'active' && ` · 剩余 ${c.days_left} 天`}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}

            <div className="section-title">看房预约（{appointments.length}）</div>
            {appointments.length === 0 ? (
              <p className="desc">暂无预约。</p>
            ) : (
              <ul className="record-list">
                {appointments.slice(0, 20).map((a) => (
                  <li key={a.id}>
                    <div className="record-head">
                      <strong>{a.house_no}</strong>
                      <span className={`pill status-${a.status}`}>
                        {APPOINTMENT_STATUS[a.status] || a.status}
                      </span>
                    </div>
                    <div className="record-body">
                      {a.viewing_time.slice(0, 16)} · {a.contact_name} {a.contact_phone}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* ---------------- 工单总览 ---------------- */}
        {tab === 'repairs' &&
          (repairs.length === 0 ? (
            <p className="desc">全平台暂无报修工单。</p>
          ) : (
            <ul className="record-list">
              {[...repairs]
                .sort((a, b) => (b.urgency === '紧急') - (a.urgency === '紧急'))
                .map((item) => (
                  <li key={item.id} className={item.urgency === '紧急'
                    && item.status !== 'done' ? 'unread' : ''}>
                    <div className="record-head">
                      <strong>{item.house_no}</strong>
                      {item.urgency === '紧急' && <span className="pill urgent">紧急</span>}
                      <span className={`pill ${item.status === 'done'
                        ? 'status-confirmed' : 'status-pending'}`}>
                        {item.status === 'done' ? '已完成'
                          : item.status === 'processing' ? '处理中' : '待处理'}
                      </span>
                    </div>
                    <div className="record-body">
                      {item.category && `${item.category} · `}{item.content}
                    </div>
                    <div className="record-body">
                      {item.contact_name} {item.contact_phone} · {item.created_at}
                    </div>
                  </li>
                ))}
            </ul>
          ))}

        {/* ---------------- 公告管理 ---------------- */}
        {tab === 'notices' && (
          <>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                if (!draft.title.trim() || !draft.content.trim()) {
                  setError('标题和正文都要填')
                  return
                }
                run('notice', () => api.createNotice(draft), '公告已发布。')
                  .then(() => setDraft({ title: '', content: '', category: '公告' }))
              }}
            >
              <label className="fld">
                <span>标题</span>
                <input value={draft.title}
                  onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
              </label>
              <label className="fld">
                <span>分类</span>
                <select value={draft.category}
                  onChange={(e) => setDraft({ ...draft, category: e.target.value })}>
                  <option>公告</option>
                  <option>费用说明</option>
                  <option>服务规则</option>
                  <option>合同条款</option>
                </select>
              </label>
              <label className="fld">
                <span>正文</span>
                <textarea rows={4} value={draft.content}
                  onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
              </label>
              <button className="btn block" type="submit" disabled={busy === 'notice'}>
                {busy === 'notice' ? '发布中…' : '发布公告'}
              </button>
            </form>

            <div className="section-title">已有公告</div>
            <ul className="record-list">
              {notices.map((n) => (
                <li key={n.id}>
                  <div className="record-head">
                    <strong>{n.title}</strong>
                    <span className={`pill ${n.is_published
                      ? 'status-confirmed' : 'status-cancelled'}`}>
                      {n.is_published ? '已发布' : '已下架'}
                    </span>
                  </div>
                  <div className="record-body">
                    {n.category} · {n.published_at} · 浏览 {n.view_count}
                  </div>
                  <div className="record-body desc-clip">{n.content}</div>
                  <div className="record-actions">
                    <button
                      className="btn ghost small"
                      disabled={busy === `n${n.id}`}
                      onClick={() => run(`n${n.id}`,
                        () => api.toggleNotice(n.id, !n.is_published))}
                    >
                      {n.is_published ? '下架' : '重新发布'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}

        {/* ---------------- 用户管理 ---------------- */}
        {tab === 'users' && (
          <>
            <form
              className="field"
              onSubmit={(e) => { e.preventDefault(); load() }}
            >
              <input
                placeholder="搜索用户名或姓名"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
              <button className="btn small" type="submit">搜索</button>
            </form>

            <ul className="record-list">
              {users.map((u) => (
                <li key={u.id}>
                  <div className="record-head">
                    <strong>{u.real_name || u.username}</strong>
                    <span className={`pill ${u.status === 'active'
                      ? 'status-confirmed' : 'status-rejected'}`}>
                      {u.status === 'active' ? '正常' : '已禁用'}
                    </span>
                  </div>
                  <div className="record-body">
                    {u.username} · {ROLE_LABEL[u.role] || u.role} · {u.phone}
                  </div>
                  {u.id !== user.id ? (
                    <div className="record-actions">
                      <button
                        className="btn ghost small"
                        disabled={busy === `u${u.id}`}
                        onClick={() => run(`u${u.id}`, () => api.updateUser(u.id, {
                          status: u.status === 'active' ? 'disabled' : 'active',
                        }))}
                      >
                        {u.status === 'active' ? '禁用' : '启用'}
                      </button>
                      {u.role !== 'admin' && (
                        <button
                          className="btn ghost small"
                          disabled={busy === `u${u.id}`}
                          onClick={() => run(`u${u.id}`, () => api.updateUser(u.id, {
                            role: u.role === 'tenant' ? 'landlord' : 'tenant',
                          }))}
                        >
                          转为{u.role === 'tenant' ? '房东' : '租客'}
                        </button>
                      )}
                      {/* B1：人工兜底的找回密码。
                          邮箱通道是「尽力而为」的——平台没配 SMTP、
                          用户压根没绑邮箱，那条自助流程就只会返回一句
                          「如果该账号存在……」然后什么也不发生。
                          **对忘了密码的人来说那等于没有这个功能**。
                          真实平台这时走人工客服，这个按钮就是那条路。 */}
                      {u.role !== 'admin' && u.status === 'active' && (
                        <button
                          className="btn ghost small"
                          disabled={busy === `u${u.id}`}
                          onClick={async () => {
                            setResetCode(null)
                            setError('')
                            try {
                              const r = await api.issueResetCode(u.id)
                              setResetCode({ ...r, who: u.real_name || u.username })
                            } catch (err) {
                              setError(err.message)
                            }
                          }}
                        >
                          发重置码
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="own" style={{ marginTop: 8 }}>
                      当前登录账号，不可修改自身状态
                    </div>
                  )}
                </li>
              ))}
            </ul>

            {/* 码只显示这一次：它等于一把临时钥匙，留在页面上
                谁路过都能看见。**必须连同用法一起给**——
                只甩一串字符，管理员不知道该让用户拿它去哪儿填 */}
            {resetCode && (
              <div className="reset-code-box">
                <div className="rc-head">
                  <strong>给「{resetCode.who}」的一次性重置码</strong>
                  <button className="link" onClick={() => setResetCode(null)}>
                    我记下了，关闭
                  </button>
                </div>
                <code className="rc-code">{resetCode.code}</code>
                <p className="rc-note">
                  {resetCode.expires_in_minutes} 分钟内有效，只能用一次。
                  {resetCode.emailed
                    ? '同一份已发到该用户绑定的邮箱。'
                    : '该用户没有绑定邮箱，只能由你转告。'}
                </p>
                <p className="rc-warn">{resetCode.warning}</p>
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
