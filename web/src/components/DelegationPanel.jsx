import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 房东这一侧的委托管理（J-04）。
 *
 * 这一页存在的理由是整个中介功能的出发点：**房东不想自己带看**。
 * 一个在外地工作、或者手上有五套房的房东，得自己处理每条预约、
 * 自己去开门、自己回每一句咨询——不给他一个把这些交出去的出口，
 * 系统对他就是不可用的。
 *
 * 三条规则在界面上必须让房东看得见，否则他不敢用：
 *
 * 1. **委托不是过户。** 委托期间房东自己照样能改房源、回消息、签合同。
 *    页面上把这句写出来，比在文档里写十遍有用。
 * 2. **随时可以单方面解除**，不需要中介同意。
 * 3. **签约永远是房东自己点。** 中介不能替他签——那是钱和法律责任。
 *
 * 委托是双向确认的：房东邀请只到 pending，要中介点头；中介申请也一样。
 * 所以这一页同时是「我发出的邀请」和「等我处理的申请」两个收件箱。
 */

const STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  active: { label: '委托中', cls: 'status-confirmed' },
  rejected: { label: '已拒绝', cls: 'status-rejected' },
  ended: { label: '已解除', cls: 'status-cancelled' },
}

export default function DelegationPanel({ houses = [] }) {
  const [rows, setRows] = useState([])
  const [draft, setDraft] = useState(
    { house_no: '', agent_username: '', commission_rate: 50 })
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')
  // J-09-b 智能派单：给选中的房源推荐几个中介
  const [suggested, setSuggested] = useState(null)

  async function load() {
    setError('')
    try {
      const data = await api.delegations({ role: 'landlord' })
      setRows(data.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function run(key, fn, message) {
    if (busy) return
    setBusy(key)
    setError('')
    setOk('')
    try {
      await fn()
      setOk(message)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  function endOne(row) {
    const reason = window.prompt(
      `解除对「${row.house_title}」的委托？中介的管理权限会立刻消失。`,
      '想自己出租')
    if (reason === null) return
    run(`end${row.id}`, () => api.endDelegation(row.id, reason),
      '已解除。中介立刻管不了这套房了，已产生的带看记录保留。')
  }

  // 我发出去等中介确认的 / 中介申请等我确认的，分开显示。
  // 混在一起的话，房东分不清哪些需要他动手
  const mine = rows.filter(
    (r) => r.status === 'pending' && r.initiated_by === 'landlord')
  const inbox = rows.filter(
    (r) => r.status === 'pending' && r.initiated_by === 'agent')
  const active = rows.filter((r) => r.status === 'active')
  const history = rows.filter(
    (r) => r.status === 'ended' || r.status === 'rejected')

  // 没被委托出去的房源才能再发邀请
  const busyHouses = new Set(
    rows.filter((r) => r.status === 'active' || r.status === 'pending')
      .map((r) => r.house_no))
  const free = houses.filter((h) => !busyHouses.has(h.house_no))

  return (
    <div className="roster">
      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      <p className="desc">
        把房源委托给中介，由他代你带看和回咨询。
        <b>委托不是过户</b>——委托期间你照样能改房源、回消息，
        随时可以单方面解除；<b>签合同永远是你自己点</b>，中介签不了。
      </p>

      {/* --------------------------------------- 中介申请接单，等我确认 */}
      {inbox.length > 0 && (
        <>
          <div className="section-title">等我确认的申请</div>
          <ul className="record-list">
            {inbox.map((row) => (
              <li key={row.id}>
                <div className="record-head">
                  <strong>{row.house_title}</strong>
                  <span className="pill status-pending">待我确认</span>
                </div>
                <div className="record-body">
                  中介 {row.agent_name} 申请接管 ·
                  佣金 {row.commission_rate}% 月租 · {row.created_at}
                </div>
                <div className="record-actions">
                  <button
                    className="btn small"
                    disabled={busy === `y${row.id}`}
                    onClick={() => run(`y${row.id}`,
                      () => api.respondDelegation(row.id, true),
                      '已同意。他现在能帮你带看和回咨询了，你的权限不受影响。')}
                  >
                    同意
                  </button>
                  <button
                    className="btn ghost small"
                    disabled={busy === `n${row.id}`}
                    onClick={() => run(`n${row.id}`,
                      () => api.respondDelegation(row.id, false),
                      '已拒绝。')}
                  >
                    拒绝
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* --------------------------------------- 委托中 */}
      <div className="section-title">委托中</div>
      {active.length === 0 ? (
        <p className="desc">还没有委托出去的房源。</p>
      ) : (
        <ul className="record-list">
          {active.map((row) => (
            <li key={row.id}>
              <div className="record-head">
                <strong>{row.house_title}</strong>
                <span className="pill status-confirmed">委托中</span>
              </div>
              <div className="record-body">
                {row.house_no} · 中介 {row.agent_name} ·
                佣金 {row.commission_rate}% 月租 · 起于 {row.start_at}
              </div>
              <div className="record-actions">
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
      )}

      {/* --------------------------------------- 我发出的邀请 */}
      {mine.length > 0 && (
        <>
          <div className="section-title">等中介确认</div>
          <ul className="record-list">
            {mine.map((row) => (
              <li key={row.id}>
                <div className="record-head">
                  <strong>{row.house_title}</strong>
                  <span className="pill status-pending">等对方确认</span>
                </div>
                <div className="record-body">
                  已邀请 {row.agent_name} · 佣金 {row.commission_rate}% 月租 ·
                  {row.created_at}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* --------------------------------------- 发起邀请 */}
      <div className="section-title">委托给指定中介</div>
      {free.length === 0 ? (
        <p className="desc">
          没有可委托的房源——你的房源要么已经委托出去了，要么还没发布。
        </p>
      ) : (
        <form
          className="house-form"
          onSubmit={(e) => {
            e.preventDefault()
            run('invite', () => api.createDelegation({
              house_no: draft.house_no,
              agent_username: draft.agent_username.trim(),
              commission_rate: Number(draft.commission_rate),
            }), '邀请已发出，等中介确认。他同意之前不会有任何权限。')
          }}
        >
          <div className="fld-row">
            <label className="fld">
              <span>房源</span>
              <select
                value={draft.house_no}
                onChange={(e) => setDraft({ ...draft, house_no: e.target.value })}
                required
              >
                <option value="">请选择</option>
                {free.map((h) => (
                  <option key={h.house_no} value={h.house_no}>
                    {h.title}（{h.house_no}）
                  </option>
                ))}
              </select>
            </label>
            <label className="fld">
              <span>中介账号</span>
              <input
                value={draft.agent_username}
                onChange={(e) => setDraft(
                  { ...draft, agent_username: e.target.value })}
                placeholder="对方的登录用户名"
                required
              />
            </label>
          </div>
          {/* ---------- J-09-b 智能派单 ---------- */}
          {draft.house_no && (
            <div className="suggest">
              <button
                type="button"
                className="btn ghost small"
                disabled={busy === 'suggest'}
                onClick={async () => {
                  setBusy('suggest')
                  setError('')
                  try {
                    setSuggested(await api.suggestAgents(draft.house_no))
                  } catch (err) {
                    setError(err.message)
                    setSuggested(null)
                  } finally {
                    setBusy('')
                  }
                }}
              >
                {busy === 'suggest' ? '匹配中…' : '帮我推荐中介'}
              </button>

              {suggested && (
                suggested.list.length === 0 ? (
                  <p className="desc">
                    这个区暂时没有可接单的中介。中介要资质过审、开着接单开关，
                    并且主营区域覆盖 {suggested.district} 才会出现在这里。
                  </p>
                ) : (
                  <>
                    <ul className="record-list">
                      {suggested.list.map((a) => (
                        <li key={a.user_id}>
                          <div className="record-head">
                            <strong>{a.name || a.username}</strong>
                            <span className="pill">{a.score} 分</span>
                          </div>
                          <div className="record-body">
                            {a.districts.length > 0
                              && `主营 ${a.districts.join('、')} · `}
                            从业 {a.years} 年
                            {a.intro && ` · ${a.intro}`}
                          </div>
                          <div className="record-body log-path">
                            {a.reasons.join(' · ')}
                          </div>
                          <div className="record-actions">
                            <button
                              type="button"
                              className="btn small"
                              onClick={() => setDraft(
                                { ...draft, agent_username: a.username })}
                            >
                              选他
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                    <p className="rd-note">
                      排序完全由规则算出：活跃度 {suggested.weights.activity} 分、
                      好评 {suggested.weights.praise} 分、
                      负载 {suggested.weights.load} 分、
                      成交 {suggested.weights.deals} 分。
                      <b>没有用大模型</b>——派单决定谁接到这单生意，
                      必须可复算、可申诉：中介问「为什么不派给我」，
                      要能拿出一行一行的算式。
                      「负载」是反向计分，<b>手上二十套的中介接不过来</b>；
                      评价不满 5 条时好评率按中性算，否则新中介永远排最后。
                    </p>
                  </>
                )
              )}
            </div>
          )}

          <label className="fld">
            <span>佣金比例（按月租金的百分比，成交后一次性结算）</span>
            <input
              type="number" min="0" max="100"
              value={draft.commission_rate}
              onChange={(e) => setDraft(
                { ...draft, commission_rate: e.target.value })}
            />
          </label>
          <button className="btn" type="submit" disabled={busy === 'invite'}>
            发出邀请
          </button>
        </form>
      )}

      {history.length > 0 && (
        <>
          <div className="section-title">历史</div>
          <div className="table-wrap">
            <table className="log-table">
              <thead>
                <tr>
                  <th>房源</th><th>中介</th><th>状态</th>
                  <th>结束原因</th><th>发起时间</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
                    <td>{row.house_title}</td>
                    <td>{row.agent_name}</td>
                    <td>{(STATUS[row.status] || {}).label || row.status}</td>
                    <td>{row.end_reason || '—'}</td>
                    <td className="mono">{row.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <p className="rd-note">
        一套房同时只能委托给一个中介。两个中介同时在托，带看和佣金归谁
        就说不清了——那正是链家要用一整套 ACN 规则去解决、而我们没必要
        解决的问题。解除委托后带看记录仍然保留，出了纠纷需要查得到。
      </p>
    </div>
  )
}
