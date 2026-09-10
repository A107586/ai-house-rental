import { useEffect, useState } from 'react'
import { api } from '../api'
import ContractCard from './ContractCard'
import ContractDetail from './ContractDetail'
import ProfileForm from './ProfileForm'
import RecordDetail, { DetailToggle } from './RecordDetail'
import ReviewForm from './ReviewForm'
import SavedSearchPanel from './SavedSearchPanel'

const APPOINTMENT_STATUS = {
  pending: '待确认',
  confirmed: '已确认',
  rejected: '已拒绝',
  cancelled: '已取消',
}

const BILL_TYPE = {
  rent: '租金',
  deposit: '押金',
  utility: '水电',
  other: '其它',
}

const REPAIR_STATUS = {
  pending: { label: '待处理', cls: 'status-pending' },
  processing: { label: '处理中', cls: 'status-pending' },
  done: { label: '已完成', cls: 'status-confirmed' },
}

// 紧急程度只给「紧急」上色。三档都染色等于都没染，
// 真正需要一眼看到的只有紧急那一档。
const URGENCY_CLS = { 紧急: 'urgent' }

const PAY_METHOD = {
  alipay: '支付宝', wechat: '微信支付', bank: '银行转账', offline: '线下缴纳',
}

// 支付单三段式状态（D-1）。「发起了」不等于「付成功了」——
// 这一栏正是用来解释「我明明点过缴费，账单怎么还是待缴」的
const PAY_STATE = {
  pending: '已发起，等待到账',
  succeeded: '已到账',
  failed: '支付失败',
  closed: '超时关单，可重新发起',
}

const MSG_TYPE = {
  system: '系统通知', appointment: '预约', contract: '合同',
  bill: '账单', repair: '报修', chat: '会话',
}

export default function MyPanel({ user, online, onClose, onOpenHouse,
                                 onUserUpdated, onLogout, onApplySearch }) {
  const [tab, setTab] = useState('contracts')
  const [appointments, setAppointments] = useState([])
  const [contracts, setContracts] = useState([])
  const [bills, setBills] = useState({ list: [], unpaid_count: 0, unpaid_amount: 0, overdue_count: 0 })
  const [favorites, setFavorites] = useState([])
  const [messages, setMessages] = useState({ list: [], unread: 0 })
  const [history, setHistory] = useState([])
  const [pending, setPending] = useState([])
  const [repairs, setRepairs] = useState([])
  const [detailNo, setDetailNo] = useState('')
  // D5：哪一条记录被展开了。键是「类型:主键」——
  // **同一时刻只展开一条**，全展开等于没有折叠
  const [open, setOpen] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [checkout, setCheckout] = useState(null)
  const [payMethod, setPayMethod] = useState('alipay')
  const [receipt, setReceipt] = useState(null)


  async function load() {
    setError('')
    try {
      // 一次性拉齐，切标签时不再等待
      const [mine, myContracts, myBills, favs, msgs, hist, pend, myRepairs] =
        await Promise.all([
          api.myAppointments(),
          // D3：个人中心一律只问「我作为租客」的那一份。
          // 房东兼租客时不加 side，这里会混进他出租的合同与
          // 名下房源的工单——那些属于工作台
          api.myContracts('', 'tenant'),
          api.myBills(),
          api.listFavorites(),
          api.listMessages(),
          api.history(),
          api.pendingReviews(),
          api.listRepairs({ side: 'tenant' }),
        ])
      setAppointments(mine.list || [])
      setContracts(myContracts.list || [])
      setBills(myBills)
      setFavorites(favs.list || [])
      setMessages(msgs)
      setHistory(hist.list || [])
      setPending(pend.list || [])
      setRepairs(myRepairs.list || [])
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

  // 预约、租约、报修这些列表里只有 house_no，抽屉需要完整房源对象，
  // 所以点开时先取一次详情。取不到就提示，而不是静默无反应——
  // 房源被下架、被删都可能让这一步失败。
  async function openHouseByNo(houseNo) {
    if (!houseNo || !onOpenHouse) return
    try {
      onOpenHouse(await api.getHouse(houseNo))
    } catch {
      setError('这套房源已下架或不存在，看不到详情了。')
    }
  }

  async function run(key, fn) {
    if (busy) return
    setBusy(key)
    setError('')
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  async function submitPayment() {
    if (!checkout || busy) return
    const key = checkout.bill_no
    setBusy(key)
    setError('')
    try {
      const result = await api.payBill(key, payMethod)
      setCheckout(null)
      setReceipt({ bill: result.bill, payment: result.payment })
      await load()
    } catch (err) {
      setCheckout(null)
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  // **个人中心对所有角色是同一套。**
  //
  // 原先这里按 isStaff 换标签：房东看到的是「出租合同」「收租」
  // 「收到的预约」，租客看到「我的租约」「我的账单」「我的预约」。
  // 问题是同一个人可能两者都是——他既租着一套房，又出租着另一套。
  // 换标签只是把两种身份的数据塞进同一个列表，**用户每看一条
  // 都要先判断自己在这条里是哪一方**。
  //
  // 现在的分界很干脆：
  //   个人中心 = 我作为租客的事（我租的、我要交的、我约的）
  //   工作台   = 我作为房东的事（我出租的、我要收的、别人约我的）
  // 两边各自完整，互不掺和。
  //
  // 「我的搜索」整条去掉：搜索记录改成输入时自动出现的下拉，
  // 不再需要一个专门的页签来管理它。
  const toggle = (key) => setOpen((cur) => (cur === key ? '' : key))
  const isOpen = (key) => open === key

  const TABS = [
    { key: 'contracts', label: '我的租约', n: contracts.length },
    { key: 'bills', label: '我的账单', n: bills.unpaid_count },
    { key: 'appointments', label: '我的预约', n: appointments.length },
    { key: 'messages', label: '消息', n: messages.unread },
    { key: 'favorites', label: '收藏', n: favorites.length },
    { key: 'history', label: '足迹', n: history.length },
    { key: 'repairs', label: '我的报修',
      n: repairs.filter((r) => r.status !== 'done').length },
    { key: 'reviews', label: '待评价', n: pending.length },
    { key: 'profile', label: '账号设置' },
  ]

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer wide">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        {/* B1：顶栏那两个低频项搬到这里。
            个人中心本来就是「关于我这个账号」的页面，
            「我是谁、服务通不通、退出」正属于这一层。 */}
        <div className="me-head">
          <span className="me-head-avatar" aria-hidden="true">
            {(user.real_name || user.username || '我').trim()[0]}
          </span>
          <div className="me-head-text">
            <h3>{user.real_name || user.username}</h3>
            <div className="loc">
              {{ tenant: '租客', landlord: '房东', agent: '经纪人',
                 admin: '管理员' }[user.role] || user.role} · {user.phone}
            </div>
          </div>
          <div className="me-head-side">
            <span className={`status-dot ${online === false ? 'offline' : ''}`}>
              <span className={`dot ${online === true ? 'up'
                : online === false ? 'down' : ''}`} />
              {online === true ? '服务正常'
                : online === false ? '服务未连接' : '检查中'}
            </span>
            <button className="btn ghost small" onClick={onLogout}>退出登录</button>
          </div>
        </div>

        <div className="tabs scroll">
          {TABS.map((item) => (
            <button
              key={item.key}
              className={`tab ${tab === item.key ? 'active' : ''}`}
              onClick={() => setTab(item.key)}
            >
              {item.label}
              {item.n > 0 && <span className="tab-n">{item.n}</span>}
            </button>
          ))}
        </div>

        {error && <div className="reply failed">{error}</div>}

        {/* ---------------- 租约 ---------------- */}
        {tab === 'contracts' &&
          (contracts.length === 0 ? (
            <p className="desc">
              还没有租约。看房满意后，房东会发起签约。
            </p>
          ) : (
            <ul className="record-list">
              {contracts.map((contract) => (
                <ContractCard
                  onOpenHouse={openHouseByNo}
                  onOpenDetail={setDetailNo}
                  key={contract.contract_no}
                  contract={contract}
                  role={user.role}
                  onChanged={load}
                />
              ))}
            </ul>
          ))}

        {/* ---------------- 账单 ---------------- */}
        {tab === 'bills' && (
          <>
            <div className="sum-row">
              <div className="sum">
                <div className="sum-k">待缴</div>
                <div className="sum-v money">{bills.unpaid_amount}</div>
                <div className="sum-d">元 · {bills.unpaid_count} 笔</div>
              </div>
              <div className="sum">
                <div className="sum-k">逾期</div>
                <div className={`sum-v ${bills.overdue_count ? 'danger' : ''}`}>
                  {bills.overdue_count}
                </div>
                <div className="sum-d">笔</div>
              </div>
            </div>

            {bills.list.length === 0 ? (
              <p className="desc">还没有账单。合同生效后会自动排出整个租期的账单。</p>
            ) : (
              <ul className="record-list">
                {bills.list.map((bill) => (
                  <li key={bill.bill_no}>
                    <div className="record-head">
                      <strong>
                        {BILL_TYPE[bill.type] || bill.type}
                        {bill.period && ` · ${bill.period}`}
                      </strong>
                      <span
                        className={`pill ${
                          bill.status === 'paid'
                            ? 'status-confirmed'
                            : bill.overdue
                            ? 'status-rejected'
                            : 'status-pending'
                        }`}
                      >
                        {bill.status === 'paid'
                          ? '已缴'
                          : bill.status === 'cancelled'
                          ? '已作废'
                          : bill.overdue
                          ? '已逾期'
                          : '待缴'}
                      </span>
                    </div>
                    <div className="record-body">
                      <span className="amount">{bill.amount} 元</span> · {bill.house_no} ·
                      到期 {bill.due_date}
                    </div>
                    {bill.status === 'paid' && (
                      <div className="record-body">缴费时间 {bill.paid_at}</div>
                    )}

                    {bill.status === 'unpaid' && (
                      <div className="record-actions">
                        <button
                          className="btn small"
                          disabled={busy === bill.bill_no}
                          onClick={() => {
                            setError('')
                            setPayMethod('alipay')
                            setCheckout(bill)
                          }}
                        >
                          {busy === bill.bill_no ? '支付中' : '立即缴费'}
                        </button>
                      </div>
                    )}
                    {bill.status === 'paid' && bill.payment && (
                      <div className="record-actions">
                        <button className="btn small ghost" onClick={() =>
                          setReceipt({ bill, payment: bill.payment })}>
                          查看缴费凭证
                        </button>
                      </div>
                    )}

                    <DetailToggle open={isOpen(`b${bill.bill_no}`)}
                                  onClick={() => toggle(`b${bill.bill_no}`)} />
                    {isOpen(`b${bill.bill_no}`) && (
                      <RecordDetail rows={[
                        ['账单编号', bill.bill_no, { mono: true }],
                        ['费用类型', BILL_TYPE[bill.type] || bill.type],
                        ['所属期', bill.period],
                        ['金额', `${bill.amount} 元`, { money: true }],
                        ['到期日', bill.due_date],
                        ['是否逾期', bill.status === 'paid' ? '否（已结清）'
                          : bill.overdue ? '是' : '否'],
                        ['关联合同', bill.contract_no, { mono: true }],
                        ['房源编号', bill.house_no, { mono: true }],
                        ['缴费时间', bill.paid_at],
                        ['支付方式', bill.payment
                          && (PAY_METHOD[bill.payment.method] || bill.payment.method)],
                        ['支付状态', bill.payment
                          && (PAY_STATE[bill.payment.status] || bill.payment.status)],
                        ['交易流水号', bill.payment && bill.payment.trade_no, { mono: true }],
                        ['备注', bill.remark, { wide: true }],
                      ]} note={bill.payment
                        ? '流水号是与客服核对这笔缴费时要报的号码。'
                        : ''} />
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* ---------------- 预约 ---------------- */}
        {tab === 'appointments' &&
          (appointments.length === 0 ? (
            <p className="desc">还没有预约记录。</p>
          ) : (
            <ul className="record-list">
              {appointments.map((item) => (
                <li key={item.id}>
                  <div className="record-head">
                    {/* 房源名可点。原先是死文字，想再看一眼自己约的那套房
                        只能回首页重新搜一遍 */}
                    <button
                      className="link-title"
                      onClick={() => openHouseByNo(item.house_no)}
                    >
                      {item.house_title || item.house_no}
                    </button>
                    <span className={`pill status-${item.status}`}>
                      {APPOINTMENT_STATUS[item.status] || item.status}
                    </span>
                  </div>
                  <div className="record-body">
                    {/* 编号单独标出来：跟客服或房东沟通时要报的就是它 */}
                    <span className="house-no">{item.house_no}</span>
                    {item.district && `${item.district} · `}
                    {item.community}
                    {item.layout && ` · ${item.layout}`}
                    {item.rent_price ? ` · ${Math.round(item.rent_price)} 元/月` : ''}
                  </div>
                  <div className="record-body">
                    看房时间：{item.viewing_time.slice(0, 16)}
                    {item.contact_name && ` · 联系人 ${item.contact_name}`}
                  </div>
                  {item.remark && <div className="record-body">备注：{item.remark}</div>}
                  {item.reply && <div className="record-body">房东回复：{item.reply}</div>}

                  {item.status === 'pending' && (
                    <div className="record-actions">
                      {/* 「确认 / 拒绝预约」是**房东动作**，已移到工作台。
                          个人中心只答「我作为租客的事」——我约了谁、
                          什么时候、能不能取消。把房东的操作留在这里，
                          等于让用户在同一个列表里既当申请方又当审批方。 */}
                      <button
                        className="btn ghost small"
                        onClick={() =>
                          run(`a${item.id}`, () =>
                            api.updateAppointment(item.id, 'cancelled'))
                        }
                      >
                        取消
                      </button>
                    </div>
                  )}

                  <DetailToggle open={isOpen(`a${item.id}`)}
                                onClick={() => toggle(`a${item.id}`)} />
                  {isOpen(`a${item.id}`) && (
                    <RecordDetail rows={[
                      ['预约编号', `YY${String(item.id).padStart(6, '0')}`, { mono: true }],
                      ['房源编号', item.house_no, { mono: true }],
                      ['房源名称', item.house_title],
                      ['所在区域', [item.district, item.community].filter(Boolean).join(' · ')],
                      ['户型', item.layout],
                      ['月租', item.rent_price ? `${Math.round(item.rent_price)} 元/月` : '',
                        { money: true }],
                      ['看房时间', item.viewing_time.slice(0, 16)],
                      ['当前状态', APPOINTMENT_STATUS[item.status] || item.status],
                      ['看房联系人', item.contact_name],
                      // 号码在看房窗口外一律不给（J-07）。给不出来的时候
                      // 说清楚**什么时候能看到**，比空着一栏强
                      ['联系电话', item.contact_visible ? item.contact_phone
                        : '看房当天可见'],
                      ['我的备注', item.remark, { wide: true }],
                      ['房东回复', item.reply, { wide: true }],
                    ]} />
                  )}
                </li>
              ))}
            </ul>
          ))}

        {/* ---------------- 消息 ---------------- */}
        {tab === 'messages' && (
          <>
            {messages.unread > 0 && (
              <div className="record-actions" style={{ marginBottom: 10 }}>
                <button
                  className="btn ghost small"
                  onClick={() => run('readall', api.readAllMessages)}
                >
                  全部标为已读
                </button>
              </div>
            )}
            {messages.list.length === 0 ? (
              <p className="desc">还没有消息。预约结果、账单与合同变动都会通知到这里。</p>
            ) : (
              <ul className="record-list">
                {messages.list.map((item) => (
                  <li key={item.id} className={item.is_read ? '' : 'unread'}>
                    <div className="record-head">
                      <strong>{item.title}</strong>
                      <span className="pill">{item.created_at.slice(5)}</span>
                    </div>
                    {item.content && <div className="record-body">{item.content}</div>}
                    {!item.is_read && (
                      <div className="record-actions">
                        <button
                          className="btn ghost small"
                          onClick={() => run(`m${item.id}`, () => api.readMessage(item.id))}
                        >
                          标为已读
                        </button>
                      </div>
                    )}

                    <DetailToggle open={isOpen(`m${item.id}`)}
                                  onClick={() => toggle(`m${item.id}`)} />
                    {isOpen(`m${item.id}`) && (
                      <RecordDetail rows={[
                        ['消息类型', MSG_TYPE[item.type] || item.type],
                        ['送达时间', item.created_at],
                        ['阅读状态', item.is_read ? '已读' : '未读'],
                        // link_no 是这条通知说的那个单子的编号。
                        // 「您的账单已生成」——**哪一张？** 有它才对得上
                        ['关联单号', item.link_no, { mono: true }],
                        ['正文', item.content, { wide: true }],
                      ]} />
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* ---------------- 浏览足迹 ---------------- */}
        {tab === 'history' && (
          history.length === 0 ? (
            <p className="desc">还没有浏览记录。看过的房源会自动出现在这里。</p>
          ) : (
            <>
              <div className="record-actions" style={{ marginBottom: 10 }}>
                <button className="btn ghost small"
                  onClick={() => run('clear', api.clearHistory)}>
                  清空足迹
                </button>
              </div>
              <ul className="record-list">
                {history.map((h) => (
                  <li key={h.house_no}>
                    <div className="record-head">
                      <strong>{h.title}</strong>
                      <span className="pill">{Math.round(h.rent_price)} 元</span>
                    </div>
                    <div className="record-body">
                      {h.district} · {h.layout} · {Math.round(h.area)} 平米
                    </div>
                    <div className="record-body own">
                      {h.viewed_at} 看过{h.my_view_count > 1 && ` · 共 ${h.my_view_count} 次`}
                    </div>
                    <div className="record-actions">
                      <button className="btn small" onClick={() => onOpenHouse(h)}>
                        再看看
                      </button>
                    </div>

                    <DetailToggle open={isOpen(`h${h.house_no}`)}
                                  onClick={() => toggle(`h${h.house_no}`)} />
                    {isOpen(`h${h.house_no}`) && (
                      <RecordDetail rows={[
                        ['房源编号', h.house_no, { mono: true }],
                        ['小区', h.community],
                        ['所在区域', h.district],
                        ['户型', h.layout],
                        ['面积', h.area ? `${Math.round(h.area)} 平米` : ''],
                        ['朝向', h.orientation],
                        ['楼层', h.floor],
                        ['月租', h.rent_price ? `${Math.round(h.rent_price)} 元/月` : '',
                          { money: true }],
                        ['最近浏览', h.viewed_at],
                        ['我看过', h.my_view_count ? `${h.my_view_count} 次` : ''],
                        ['当前状态', h.status === 'available' ? '在架可租'
                          : h.status === 'rented' ? '已出租' : h.status],
                      ]} />
                    )}
                  </li>
                ))}
              </ul>
            </>
          )
        )}

        {/* ---------------- 我的搜索（K-09） ---------------- */}
        {tab === 'searches' && (
          <SavedSearchPanel
            onApply={(conditions) => {
              // 一键恢复：把条件交回首页并关掉面板。
              // 这是保存搜索的主要用途——留在面板里看着条件却用不上，
              // 等于只做了一半
              onApplySearch && onApplySearch(conditions)
              onClose()
            }}
          />
        )}

        {/* ---------------- 待评价 ---------------- */}
        {/* ---------------- 我的报修 ---------------- */}
        {tab === 'repairs' &&
          (repairs.length === 0 ? (
            <p className="desc">
              还没有报修记录。房子有问题可以在房源详情页提交，
              也可以直接跟租房助手说。
            </p>
          ) : (
            <ul className="record-list">
              {repairs.map((item) => {
                const st = REPAIR_STATUS[item.status]
                  || { label: item.status, cls: '' }
                return (
                  <li key={item.id} className={item.status !== 'done' ? 'unread' : ''}>
                    <div className="record-head">
                      <button
                        className="link-title"
                        onClick={() => openHouseByNo(item.house_no)}
                      >
                        {item.house_no}
                      </button>
                      {item.category && (
                        <span className="pill">{item.category}</span>
                      )}
                      {item.urgency && (
                        <span className={`pill ${URGENCY_CLS[item.urgency] || ''}`}>
                          {item.urgency}
                        </span>
                      )}
                      <span className={`pill ${st.cls}`}>{st.label}</span>
                    </div>

                    <div className="record-body">{item.content}</div>

                    {item.overdue && (
                      <div className="record-body overdue-note">
                        已等待 {item.waited_hours} 小时，超过承诺时限
                      </div>
                    )}

                    {item.analysis && (
                      <div className="record-body ai-note">{item.analysis}</div>
                    )}

                    {/* 完成的工单要说清楚「谁修的、修了什么」。
                        只显示到这里为止——维修花了多少钱是房东的成本 */}
                    {item.result ? (
                      <div className="record-body done-note">
                        {item.result.finished_at} 由 {item.result.worker_name} 处理完成
                        {item.result.note && `：${item.result.note}`}
                      </div>
                    ) : (
                      <div className="record-body muted-note">
                        提交于 {item.created_at}
                        {item.assignee && ` · 建议派单：${item.assignee}`}
                      </div>
                    )}

                    <DetailToggle open={isOpen(`r${item.id}`)}
                                  onClick={() => toggle(`r${item.id}`)} />
                    {isOpen(`r${item.id}`) && (
                      <RecordDetail rows={[
                        ['工单号', `GD${String(item.id).padStart(6, '0')}`, { mono: true }],
                        ['房源编号', item.house_no, { mono: true }],
                        ['故障类别', item.category],
                        ['紧急程度', item.urgency],
                        ['当前状态', st.label],
                        ['提交时间', item.created_at],
                        ['报修联系人', item.contact_name],
                        ['联系电话', item.contact_phone],
                        ['已等待', item.waited_hours !== undefined
                          ? `${item.waited_hours} 小时${item.overdue ? '（已超时限）' : ''}` : ''],
                        ['故障描述', item.content, { wide: true }],
                        // 智能体的初判要标明出处。**用户得知道这句话是谁说的**——
                        // 是维修师傅看过之后的结论，还是模型读了描述给的猜测
                        ['智能体初判', item.analysis, { wide: true }],
                        ['建议派单', item.assignee],
                        ['处理人', item.result && item.result.worker_name],
                        ['完成时间', item.result && item.result.finished_at],
                        ['处理说明', item.result && item.result.note, { wide: true }],
                      ]} note={item.analysis
                        ? '「智能体初判」是模型读故障描述给出的推测，实际以上门检查为准。'
                        : ''} />
                    )}
                  </li>
                )
              })}
            </ul>
          ))}

        {tab === 'reviews' && (
          pending.length === 0 ? (
            <p className="desc">没有待评价的租约。租约结束后可以对房源与房东评分。</p>
          ) : (
            <ul className="record-list">
              {pending.map((c) => (
                <ReviewForm key={c.contract_no} contract={c} onDone={load} />
              ))}
            </ul>
          )
        )}

        {/* ---------------- 账号设置 ---------------- */}
        {tab === 'profile' && (
          <ProfileForm user={user} onUpdated={onUserUpdated} onLogout={onLogout} />
        )}

        {/* ---------------- 收藏 ---------------- */}
        {tab === 'favorites' &&
          (favorites.length === 0 ? (
            <p className="desc">还没有收藏房源。</p>
          ) : (
            <ul className="record-list">
              {favorites.map((house) => (
                <li key={house.house_no}>
                  <div className="record-head">
                    <strong>{house.title}</strong>
                    <span className="pill">{Math.round(house.rent_price)} 元</span>
                  </div>
                  <div className="record-body">
                    {house.district} · {house.layout} · {Math.round(house.area)} 平米
                  </div>
                  <div className="record-actions">
                    <button className="btn small" onClick={() => onOpenHouse(house)}>
                      查看
                    </button>
                    <button
                      className="btn ghost small"
                      onClick={() =>
                        run(`f${house.house_no}`, () => api.removeFavorite(house.house_no))
                      }
                    >
                      取消收藏
                    </button>
                  </div>

                  <DetailToggle open={isOpen(`f${house.house_no}`)}
                                onClick={() => toggle(`f${house.house_no}`)} />
                  {isOpen(`f${house.house_no}`) && (
                    <RecordDetail rows={[
                      ['房源编号', house.house_no, { mono: true }],
                      ['小区', house.community],
                      ['所在区域', house.district],
                      ['户型', house.layout],
                      ['面积', house.area ? `${Math.round(house.area)} 平米` : ''],
                      ['朝向', house.orientation],
                      ['楼层', house.floor],
                      ['租赁方式', house.rent_type],
                      ['月租', house.rent_price ? `${Math.round(house.rent_price)} 元/月` : '',
                        { money: true }],
                      ['押付方式', house.payment_way],
                      ['评分', house.rating ? `${house.rating} 分` : ''],
                      // 收藏是「先存着回头再比」，那么**它还在不在架上**
                      // 就是打开这一栏最先想知道的事
                      ['当前状态', house.status === 'available' ? '在架可租'
                        : house.status === 'rented' ? '已出租'
                        : house.status === 'offline' ? '已下架' : house.status],
                      ['配套', (house.features || house.tags || []).join('、'), { wide: true }],
                    ]} />
                  )}
                </li>
              ))}
            </ul>
          ))}
      </aside>
      {checkout && (
        <div className="modal-mask payment-mask" onClick={() => !busy && setCheckout(null)}>
          <div className="dialog payment-dialog" onClick={(e) => e.stopPropagation()}>
            <h3>模拟收银台</h3>
            <p className="desc">本功能仅用于课程项目演示，不会产生真实扣款。</p>
            <div className="payment-summary">
              <div><span>房源</span><strong>{checkout.house_no}</strong></div>
              <div><span>账单</span><strong>{BILL_TYPE[checkout.type] || checkout.type}{checkout.period ? ` · ${checkout.period}` : ''}</strong></div>
              <div><span>到期日</span><strong>{checkout.due_date}</strong></div>
              <div><span>应付金额</span><strong className="amount">¥ {Number(checkout.amount).toFixed(2)}</strong></div>
            </div>
            <label>支付方式</label>
            <select value={payMethod} onChange={(e) => setPayMethod(e.target.value)}>
              <option value="alipay">支付宝（模拟）</option>
              <option value="wechat">微信支付（模拟）</option>
              <option value="bank">银行卡（模拟）</option>
            </select>
            <div className="dialog-foot">
              <button className="btn ghost" disabled={!!busy} onClick={() => setCheckout(null)}>暂不支付</button>
              <button className="btn" disabled={!!busy} onClick={submitPayment}>
                {busy ? '处理中…' : `确认支付 ¥${Number(checkout.amount).toFixed(2)}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {receipt && (
        <div className="modal-mask payment-mask" onClick={() => setReceipt(null)}>
          <div className="dialog payment-dialog receipt" onClick={(e) => e.stopPropagation()}>
            <h3>缴费凭证</h3>
            <div className="receipt-ok">✓ 支付成功</div>
            <div className="payment-summary">
              <div><span>交易号</span><strong>{receipt.payment.trade_no}</strong></div>
              <div><span>账单号</span><strong>{receipt.bill.bill_no}</strong></div>
              <div><span>房源</span><strong>{receipt.bill.house_no}</strong></div>
              <div><span>支付方式</span><strong>{{ alipay: '支付宝', wechat: '微信支付', bank: '银行卡' }[receipt.payment.method] || receipt.payment.method}</strong></div>
              <div><span>支付时间</span><strong>{receipt.payment.paid_at}</strong></div>
              <div><span>实付金额</span><strong className="amount">¥ {Number(receipt.payment.amount).toFixed(2)}</strong></div>
            </div>
            <div className="dialog-foot">
              <button className="btn" onClick={() => setReceipt(null)}>完成</button>
            </div>
          </div>
        </div>
      )}

      {detailNo && (
        <ContractDetail
          contractNo={detailNo}
          onClose={() => setDetailNo('')}
          onOpenHouse={(no) => {
            // 档案里点房源：先收掉档案再开房源抽屉，免得三层叠在一起
            setDetailNo('')
            openHouseByNo(no)
          }}
        />
      )}
    </>
  )
}
