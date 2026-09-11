import { useEffect, useState } from 'react'
import { api } from '../api'
import RatioCell from './RatioCell'
import FocusBar from './FocusBar'

/**
 * 中介工作台（J-10）。
 *
 * 这个面板要回答中介每天开工时的三个问题：**今天要带谁看房、
 * 手上还有几套在托、这个月挣了多少**。所以首屏是「今日待办」而不是
 * 一张漂亮的统计图——统计是月底才看的东西，排在后面的页签里。
 *
 * 数据全部来自 `/api/agent/dashboard` 一次请求（方案 8.5 规矩二）。
 * 十张卡片打十次请求是最容易写、也最容易把首屏拖慢的做法。
 * 后面几个页签按需加载，因为接单大厅和佣金流水不是每次开工作台都看。
 *
 * 资质未通过时**只显示资质页签**。让一个还没过审的人看到「接单大厅」
 * 却点不动，比直接告诉他「先把资质交了」更让人火大。
 */

const LICENSE = {
  unsubmitted: { label: '未提交', cls: '' },
  pending: { label: '审核中', cls: 'status-pending' },
  approved: { label: '已通过', cls: 'status-confirmed' },
  rejected: { label: '未通过', cls: 'status-rejected' },
}

// 预约状态。工作台只会看到 confirmed 的那些（未确认的预约不该出现在
// 「今天要带看」里），但把四个状态都写全，免得后端将来加了别的状态
// 这里直接漏出英文
const APPOINTMENT_STATUS = {
  pending: '待确认', confirmed: '已确认',
  rejected: '已拒绝', cancelled: '已取消',
}

export default function AgentPanel({ user, onClose, onOpenHouse, embedded,
                            onSections, activeTab, onTabChange }) {
  // 嵌入整页工作台时当前页签由左栏说了算；独立抽屉时自己管。
  // **两套状态不能并存**——并存会出现「左栏高亮 A、正文显示 B」，
  // 而这种不一致要切换几次之后才显形，最难查
  const [ownTab, setOwnTab] = useState('today')
  const tab = embedded && activeTab ? activeTab : ownTab
  const setTab = (key) => {
    if (embedded && onTabChange) onTabChange(key)
    else setOwnTab(key)
  }
  const [data, setData] = useState(null)
  const [openList, setOpenList] = useState([])
  const [viewings, setViewings] = useState([])
  const [commissions, setCommissions] = useState(null)
  const [form, setForm] = useState(
    { districts: '', intro: '', years: 0, accepting: true })
  const [license, setLicense] = useState('')
  // J-09-c 带看纪要：口述原文、整理出来的结构化结果、以及它的来源
  const [note, setNote] = useState(
    { house_no: '', tenant_username: '', text: '' })
  const [draft, setDraft] = useState(null)
  const [tidying, setTidying] = useState(false)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    setError('')
    try {
      const d = await api.brokerDashboard()
      setData(d)
      setForm({
        districts: (d.profile.districts || []).join('、'),
        intro: d.profile.intro || '',
        years: d.profile.years || 0,
        accepting: d.profile.accepting,
      })
      if (d.profile.license_status !== 'approved') setTab('profile')
    } catch (err) {
      setError(err.message)
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

  // 依赖里带上三份数据本身，这样 run() 把它们清空之后会立刻重取，
  // 而不必等用户切走页签再切回来
  useEffect(() => {
    if (tab === 'market' && openList.length === 0) {
      api.openHouses(30).then((r) => setOpenList(r.list || [])).catch(() => {})
    }
    if (tab === 'viewings' && viewings.length === 0) {
      api.viewings({ limit: 50 }).then((r) => setViewings(r.list || []))
        .catch(() => {})
    }
    if (tab === 'money' && !commissions) {
      api.brokerCommissions().then(setCommissions).catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, openList.length, viewings.length, commissions])

  async function run(key, fn, message = '') {
    if (busy) return
    setBusy(key)
    setError('')
    setOk('')
    try {
      await fn()
      if (message) setOk(message)
      await load()
      // 按需加载的那几个页签也要作废重取。
      //
      // 只重取工作台的话，「申请接单」之后那套房仍然留在接单大厅里，
      // 再点一次得到的是「这套房已经有生效中的委托」——**用户会以为
      // 是自己刚才没点上**。凡是会改变这些列表内容的操作，都得让它们
      // 重新取一次，而不是等用户切走再切回来。
      setOpenList([])
      setViewings([])
      setCommissions(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  function endOne(row) {
    const reason = window.prompt(
      `解除对「${row.house_title}」的委托？理由会通知房东。`,
      '房源长期无人问询')
    if (reason === null) return
    run(`end${row.id}`, () => api.endDelegation(row.id, reason),
      '已解除。这套房的管理权限立刻回到房东手上，带看记录保留。')
  }

  const profile = data?.profile
  const stats = data?.stats
  const approved = profile?.license_status === 'approved'
  const pendingAppts = data?.todo.pending_appointments || []
  const todoCount = (data?.todo.invitations.length || 0)
    + (data?.todo.today_viewings.length || 0) + pendingAppts.length

  // 资质没过就只留资质页签——见文件头注释
  //
  // F5：顺序按「离带看有多近」排。中介这份工作的主线是
  // 接单 → 联系 → 带看 → 成交，佣金和数据是**回头看**的东西，
  // 排在主线后面。原来「接单大厅」夹在在托房源和带看记录中间，
  // 把一条连贯的动线断成了两截。
  const TABS = approved ? [
    { key: 'today', label: '今日带看与联系', n: todoCount, group: '要做的事' },
    { key: 'market', label: '接单大厅', group: '要做的事' },
    { key: 'houses', label: '在托房源', n: data?.delegations.length,
      group: '要做的事' },
    { key: 'viewings', label: '带看记录', group: '回头看' },
    { key: 'money', label: '佣金', group: '回头看' },
    { key: 'stats', label: '我的数据', group: '回头看' },
    { key: 'profile', label: '资质与档案', group: '回头看' },
  ] : [{ key: 'profile', label: '资质与档案' }]

  // F5 待办条：三件都是「有人在等你回话」的事。
  // 邀请不回、预约不确认、带看不去，损失的都是这一单本身
  const FOCUS = approved ? [
    { key: 'today', label: '待接受邀请',
      n: data?.todo.invitations.length || 0, urgent: true,
      hint: '房东发来的委托' },
    { key: 'today', label: '待确认预约', n: pendingAppts.length, urgent: true,
      hint: '租客在等回话' },
    { key: 'today', label: '今日带看',
      n: data?.todo.today_viewings.length || 0, hint: '今天要去的场次' },
  ] : []

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
            <h3>中介工作台</h3>
            <div className="loc">
              {user.real_name || user.username} · 经纪人
              {profile && `　资质${LICENSE[profile.license_status]?.label || ''}`}
            </div>
          </>
        )}

        {approved && (
          <FocusBar items={FOCUS} onPick={setTab}
                    hint="邀请与预约都有时效，超时房东会另找人。" />
        )}

        {!embedded && (
        <div className="tabs scroll grouped">
          {TABS.map((item, i) => (
            <span className="tab-slot" key={item.key}>
              {/* 分组名只在换组时画一次。写成两个独立的 tabs 容器会让
                  横向滚动被切成两段，一屏放不下时更难找 */}
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
        {!data && !error && <div className="empty">加载中…</div>}

        {/* ------------------------------------------------ 今日待办 */}
        {tab === 'today' && data && (
          <>
            <div className="dash-grid">
              <div className="dcell">
                <div className="dk">在托房源</div>
                <div className="dv">{stats.active_delegations}</div>
                <div className="dd">套</div>
              </div>
              <div className="dcell">
                <div className="dk">今日带看</div>
                <div className="dv">{data.todo.today_viewings.length}</div>
                <div className="dd">场</div>
              </div>
              <div className="dcell">
                <div className="dk">本月成交</div>
                <div className="dv">{stats.month_deals}</div>
                <div className="dd">累计 {stats.deal_total} 单</div>
              </div>
              <div className="dcell money">
                <div className="dk">待结佣金</div>
                <div className="dv">{Math.round(stats.commission_pending)}</div>
                <div className="dd">已结 {Math.round(stats.commission_settled)} 元</div>
              </div>
            </div>

            <div className="section-title">房东邀请我接管</div>
            {data.todo.invitations.length === 0 ? (
              <p className="desc">
                没有待处理的邀请。可以去「接单大厅」自己找房源申请。
              </p>
            ) : (
              <ul className="record-list">
                {data.todo.invitations.map((row) => (
                  <li key={row.id}>
                    <div className="record-head">
                      <strong>{row.house_title}</strong>
                      <span className="pill status-pending">待我确认</span>
                    </div>
                    <div className="record-body">
                      {row.district} · <span className="amount">
                        {Math.round(row.rent_price)} 元</span>
                      {' '}· 佣金 {row.commission_rate}% 月租 · 房东 {row.landlord_name}
                    </div>
                    <div className="record-actions">
                      <button
                        className="btn small"
                        disabled={busy === `y${row.id}`}
                        onClick={() => run(`y${row.id}`,
                          () => api.respondDelegation(row.id, true),
                          '已接受。这套房现在你也能管了，房东的权限不受影响。')}
                      >
                        接受
                      </button>
                      <button
                        className="btn ghost small"
                        disabled={busy === `n${row.id}`}
                        onClick={() => run(`n${row.id}`,
                          () => api.respondDelegation(row.id, false),
                          '已婉拒。')}
                      >
                        婉拒
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}

            <div className="section-title">今天的带看</div>
            {data.todo.today_viewings.length === 0 ? (
              <p className="desc">今天没有已确认的看房预约。</p>
            ) : (
              <div className="table-wrap">
                <table className="log-table">
                  <thead>
                    <tr>
                      <th>时间</th><th>房源</th><th>租客</th>
                      <th>联系方式</th><th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.todo.today_viewings.map((a) => (
                      <tr key={a.id}>
                        <td className="mono">
                          {(a.viewing_time || '').slice(5, 16)}
                        </td>
                        <td>{a.house_title || a.house_no}</td>
                        <td>{a.contact_name || '租客'}</td>
                        <td className="mono">
                          {a.contact_visible ? a.contact_phone : (
                            <span className="log-path">看房当天才可见</span>
                          )}
                        </td>
                        <td>
                          <button
                            className="btn ghost small"
                            disabled={a.accept_contact === false
                              || busy === `c${a.id}`}
                            title={a.accept_contact === false
                              ? '对方已设置不接受主动联系' : '发起站内会话'}
                            onClick={() => run(`c${a.id}`,
                              () => api.contactTenant(a.house_no,
                                a.tenant_username),
                              '会话已建立，去「咨询」里继续沟通。')}
                          >
                            {a.accept_contact === false ? '免打扰' : '联系'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="section-title">等我确认的预约</div>
            {pendingAppts.length === 0 ? (
              <p className="desc">没有待确认的预约。</p>
            ) : (
              <div className="table-wrap">
                <table className="log-table">
                  <thead>
                    <tr><th>看房时间</th><th>房源</th><th>租客</th><th>备注</th></tr>
                  </thead>
                  <tbody>
                    {pendingAppts.map((a) => (
                      <tr key={a.id}>
                        <td className="mono">
                          {(a.viewing_time || '').slice(5, 16)}
                        </td>
                        <td>{a.house_title || a.house_no}</td>
                        <td>{a.contact_name || '-'}</td>
                        <td className="log-path">{a.remark || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="rd-note">
              租客的号码**只在看房当天且预约已确认时**显示。平时联系请走站内会话；对方设了免打扰后无法主动呼叫，预约和带看通知照常送达。
            </p>

            <p className="rd-note">
              「今天的带看」只统计**已确认**的预约；待确认的单列在下方，请先确认再安排行程。
            </p>

            <p className="rd-note">
              带看完后请补一条带看记录。租客的异议（如嫌厨房小、楼层低）是房东调整价格或描述的依据，也是日后解除委托时的凭证。
            </p>
          </>
        )}

        {/* ------------------------------------------------ 在托房源 */}
        {tab === 'houses' && data && (
          data.delegations.length === 0 ? (
            <div className="empty">还没有在托房源。去「接单大厅」看看。</div>
          ) : (
            <ul className="record-list">
              {data.delegations.map((row) => (
                <li key={row.id}>
                  <div className="record-head">
                    <strong>{row.house_title}</strong>
                    <span className="pill status-confirmed">委托中</span>
                  </div>
                  <div className="record-body">
                    {row.house_no} · {row.district} ·{' '}
                    <span className="amount">{Math.round(row.rent_price)} 元</span>
                    {' '}· 佣金 {row.commission_rate}% 月租 · 起于 {row.start_at}
                  </div>
                  <div className="record-actions">
                    <button
                      className="btn ghost small"
                      onClick={() => onOpenHouse({ house_no: row.house_no })}
                    >
                      查看房源
                    </button>
                    <button
                      className="btn ghost small danger"
                      disabled={busy === `end${row.id}`}
                      onClick={() => endOne(row)}
                    >
                      解除委托
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        )}

        {/* ------------------------------------------------ 接单大厅 */}
        {tab === 'market' && (
          <>
            <p className="desc">
              你主营区域内、当前没有委托的可租房源。申请需房东确认后生效。
            </p>
            {openList.length === 0 ? (
              <div className="empty">
                暂时没有可申请的房源。主营区域填得太窄也会让这里空着，可到「资质与档案」修改。
              </div>
            ) : (
              <ul className="record-list">
                {openList.map((h) => (
                  <li key={h.house_no}>
                    <div className="record-head">
                      <strong>{h.title}</strong>
                      <span className="pill">{h.district}</span>
                    </div>
                    <div className="record-body">
                      {h.house_no} · <span className="amount">
                        {Math.round(h.rent_price)} 元</span>
                      {' '}· {h.layout} · {Math.round(h.area)}㎡
                    </div>
                    <div className="record-actions">
                      <button
                        className="btn small"
                        disabled={busy === h.house_no}
                        onClick={() => run(h.house_no,
                          () => api.createDelegation({ house_no: h.house_no }),
                          '申请已发出，等房东确认。')}
                      >
                        申请接单
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* ------------------------------------------------ 带看记录 */}
        {tab === 'viewings' && (
          <>
            <div className="section-title">补一条带看纪要</div>
            <p className="desc">
              照平常说话写就行，智能体会整理成结构化纪要。
              <b>整理完可以改</b>——它给的是草稿，不是结论；
              智能体不可用时按关键词提取，纪要照样能存。
            </p>
            <form
              className="house-form"
              onSubmit={(e) => {
                e.preventDefault()
                if (!draft) return
                run('viewing', () => api.createViewing({
                  house_no: note.house_no,
                  tenant_username: note.tenant_username.trim(),
                  showed_up: draft.showed_up,
                  intention: draft.intention,
                  concerns: draft.concerns,
                  feedback: draft.feedback,
                }), '纪要已提交，房东会收到一条通知。')
                setNote({ house_no: '', tenant_username: '', text: '' })
                setDraft(null)
              }}
            >
              <div className="fld-row">
                <label className="fld">
                  <span>房源</span>
                  <select
                    value={note.house_no}
                    onChange={(e) => setNote({ ...note, house_no: e.target.value })}
                    required
                  >
                    <option value="">请选择</option>
                    {(data?.delegations || []).map((d) => (
                      <option key={d.house_no} value={d.house_no}>
                        {d.house_title}（{d.house_no}）
                      </option>
                    ))}
                  </select>
                </label>
                <label className="fld">
                  <span>租客账号</span>
                  <input
                    value={note.tenant_username}
                    onChange={(e) => setNote(
                      { ...note, tenant_username: e.target.value })}
                    placeholder="对方的登录用户名"
                    required
                  />
                </label>
              </div>
              <label className="fld">
                <span>带看情况（口述即可）</span>
                <textarea
                  rows={3}
                  value={note.text}
                  onChange={(e) => setNote({ ...note, text: e.target.value })}
                  placeholder="小两口，觉得朝向可以，嫌厨房小，问了能不能养猫，说要和家里商量下"
                />
              </label>
              <button
                type="button"
                className="btn ghost"
                disabled={tidying || !note.text.trim()}
                onClick={async () => {
                  setTidying(true)
                  setError('')
                  try {
                    const r = await api.viewingSummary(note.text)
                    if (r.ok) setDraft({ ...r.record, source: r.source_label })
                    else setError(r.message || '整理失败')
                  } catch (err) {
                    setError(err.message)
                  } finally {
                    setTidying(false)
                  }
                }}
              >
                {tidying ? '整理中…' : '智能整理'}
              </button>

              {draft && (
                <>
                  <div className="section-title">整理结果（可改）</div>
                  <div className="fld-row">
                    <label className="fld">
                      <span>是否到场</span>
                      <select
                        value={draft.showed_up ? 'yes' : 'no'}
                        onChange={(e) => setDraft(
                          { ...draft, showed_up: e.target.value === 'yes' })}
                      >
                        <option value="yes">已到场</option>
                        <option value="no">未到场</option>
                      </select>
                    </label>
                    <label className="fld">
                      <span>意向</span>
                      <select
                        value={draft.intention}
                        onChange={(e) => setDraft(
                          { ...draft, intention: e.target.value })}
                      >
                        <option value="high">意向强</option>
                        <option value="medium">一般</option>
                        <option value="low">意向弱</option>
                      </select>
                    </label>
                  </div>
                  <div className="record-body">
                    {draft.concerns.length > 0 && (
                      <>顾虑：{draft.concerns.map((c) => (
                        <span className="chip" key={c}>{c}</span>))}</>
                    )}
                    {draft.positives.length > 0 && (
                      <>　满意：{draft.positives.map((c) => (
                        <span className="chip" key={c}>{c}</span>))}</>
                    )}
                    {draft.questions.length > 0 && (
                      <>　问到：{draft.questions.map((c) => (
                        <span className="chip" key={c}>{c}</span>))}</>
                    )}
                  </div>
                  <label className="fld">
                    <span>纪要（房东会看到）</span>
                    <textarea
                      rows={2}
                      value={draft.feedback}
                      onChange={(e) => setDraft(
                        { ...draft, feedback: e.target.value })}
                    />
                  </label>
                  <p className="fld-hint">来源：{draft.source}</p>
                  <button className="btn" type="submit"
                    disabled={busy === 'viewing'}>
                    提交纪要
                  </button>
                </>
              )}
            </form>

            <div className="section-title">历史记录</div>
            {viewings.length === 0 ? (
              <div className="empty">还没有带看记录。</div>
            ) : (
            <div className="table-wrap">
              <table className="log-table">
                <thead>
                  <tr>
                    <th>时间</th><th>房源</th><th>租客</th>
                    <th>到场</th><th>意向</th><th>关注点</th>
                  </tr>
                </thead>
                <tbody>
                  {viewings.map((v) => (
                    <tr key={v.id} className={v.showed_up ? '' : 'bad'}>
                      <td className="mono">{v.created_at}</td>
                      <td>{v.house_title}</td>
                      <td>{v.tenant_name}</td>
                      <td>
                        {v.showed_up ? `${v.duration_minutes} 分钟` : '未到场'}
                      </td>
                      <td>{v.intention_label}</td>
                      <td>
                        {v.concerns.length
                          ? v.concerns.map((c) => (
                            <span className="chip" key={c}>{c}</span>))
                          : <span className="log-path">{v.feedback || '—'}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            )}
          </>
        )}

        {/* ------------------------------------------------ 佣金 */}
        {tab === 'money' && commissions && (
          <>
            <div className="dash-grid">
              <div className="dcell money">
                <div className="dk">待结算</div>
                <div className="dv">{Math.round(commissions.pending_amount)}</div>
                <div className="dd">元</div>
              </div>
              <div className="dcell">
                <div className="dk">已结算</div>
                <div className="dv">{Math.round(commissions.settled_amount)}</div>
                <div className="dd">元</div>
              </div>
            </div>
            {commissions.list.length === 0 ? (
              <div className="empty">
                还没有佣金记录。合同生效时才会产生。
              </div>
            ) : (
              <div className="table-wrap">
                <table className="log-table">
                  <thead>
                    <tr>
                      <th>合同</th><th>房源</th><th>计佣基数</th>
                      <th>比例</th><th>金额</th><th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {commissions.list.map((c) => (
                      <tr key={c.id}>
                        <td className="mono">{c.contract_no}</td>
                        <td className="mono">{c.house_no}</td>
                        <td>{Math.round(c.base_amount)}</td>
                        <td>{c.rate}%</td>
                        <td className="accent">{Math.round(c.amount)}</td>
                        <td>
                          {c.status === 'settled'
                            ? `已结 ${c.settled_at}` : '待结算'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="rd-note">
              计佣基数和比例分开显示，方便当场核对。结算走线下，平台只记录状态。
            </p>
          </>
        )}

        {/* ------------------------------------------------ 我的数据 */}
        {tab === 'stats' && stats && (
          <>
            <div className="section-title">近 {stats.recent_days} 天</div>
            <div className="table-wrap">
              <table className="log-table">
                <tbody>
                  <tr>
                    <td>带看场次</td>
                    <td className="accent">{stats.recent_viewings}</td>
                    <td className="log-path">
                      滚动 {stats.recent_days} 天，不是自然月——
                      月初不会突然清零
                    </td>
                  </tr>
                  <tr>
                    <td>带看成交率</td>
                    <td>
                      <RatioCell value={stats.conversion}
                        label="促成合同 / 带看总数" />
                    </td>
                    <td className="log-path">你最该盯的一个数</td>
                  </tr>
                  <tr>
                    <td>租客放鸽子率</td>
                    <td>
                      <RatioCell value={stats.no_show} warnAbove={30}
                        label="未到场 / 带看总数" />
                    </td>
                    <td className="log-path">
                      这个数高多半说明预约确认流程有问题，不是你的服务问题
                    </td>
                  </tr>
                  <tr>
                    <td>好评率</td>
                    <td>
                      <RatioCell value={stats.praise}
                        label="4 分及以上 / 有效评价" />
                    </td>
                    <td className="log-path">只统计你促成的那些合同</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="rd-note">
              所有比率都带分母；样本不足 5 时显示原始数字，不折算百分比。管理员后台看到的是**同一份计算**。
            </p>
          </>
        )}

        {/* ------------------------------------------------ 资质与档案 */}
        {tab === 'profile' && profile && (
          <>
            {profile.license_status === 'rejected' && (
              <div className="reply failed">
                资质未通过：{profile.reject_reason || '未说明理由'}。
                按说明补充后可以重新提交。
              </div>
            )}
            {profile.license_status === 'pending' && (
              <div className="reply">
                资质已提交，等管理员审核。通过前不能接单。
              </div>
            )}

            {!approved && (
              <>
                <div className="section-title">提交从业资质</div>
                <p className="desc">
                  接单前必须完成实名和从业资质核验。中介能看到租客联系方式、代房东处理咨询，这些权限不能给未核验的人。
                </p>
                <form
                  className="house-form"
                  onSubmit={(e) => {
                    e.preventDefault()
                    run('license', () => api.submitLicense(license.trim()),
                      '已提交，等待审核。')
                  }}
                >
                  <label className="fld">
                    <span>从业资格证编号</span>
                    <input
                      value={license}
                      onChange={(e) => setLicense(e.target.value)}
                      placeholder="至少 4 位"
                      required
                    />
                  </label>
                  <button
                    className="btn"
                    type="submit"
                    disabled={busy === 'license'
                      || profile.license_status === 'pending'}
                  >
                    {profile.license_status === 'pending' ? '审核中' : '提交审核'}
                  </button>
                </form>
              </>
            )}

            <div className="section-title">我的档案</div>
            <form
              className="house-form"
              onSubmit={(e) => {
                e.preventDefault()
                run('profile', () => api.updateBrokerProfile({
                  service_districts: form.districts
                    .split(/[、,，\s]+/).filter(Boolean),
                  intro: form.intro,
                  years: Number(form.years) || 0,
                  accepting: form.accepting,
                }), '档案已更新。')
              }}
            >
              <div className="fld-row">
                <label className="fld">
                  <span>主营区域（顿号分隔）</span>
                  <input
                    value={form.districts}
                    onChange={(e) => setForm({ ...form, districts: e.target.value })}
                    placeholder="甘井子区、沙河口区"
                  />
                </label>
                <label className="fld">
                  <span>从业年限</span>
                  <input
                    type="number" min="0" max="60"
                    value={form.years}
                    onChange={(e) => setForm({ ...form, years: e.target.value })}
                  />
                </label>
              </div>
              <label className="fld">
                <span>简介（租客会看到）</span>
                <textarea
                  rows={3}
                  maxLength={300}
                  value={form.intro}
                  onChange={(e) => setForm({ ...form, intro: e.target.value })}
                  placeholder="擅长的房源类型、服务方式"
                />
              </label>
              <label className="fld inline">
                <input
                  type="checkbox"
                  checked={form.accepting}
                  onChange={(e) => setForm({ ...form, accepting: e.target.checked })}
                />
                <span>接受新委托</span>
              </label>
              <button className="btn" type="submit" disabled={busy === 'profile'}>
                保存档案
              </button>
            </form>
            <p className="rd-note">
              主营区域决定「接单大厅」展示哪些房源，留空则不过滤。关闭「接受新委托」只挡新邀请，**已在托的房源不受影响**。
            </p>
          </>
        )}
      </div>
    </>
  )
}
