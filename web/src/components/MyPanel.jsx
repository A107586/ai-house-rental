import { useEffect, useState } from 'react'
import { api } from '../api'
import ContractCard from './ContractCard'
import ProfileForm from './ProfileForm'
import ReviewForm from './ReviewForm'

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

export default function MyPanel({ user, onClose, onOpenHouse, onUserUpdated, onLogout }) {
  const [tab, setTab] = useState('contracts')
  const [appointments, setAppointments] = useState([])
  const [contracts, setContracts] = useState([])
  const [bills, setBills] = useState({ list: [], unpaid_count: 0, unpaid_amount: 0, overdue_count: 0 })
  const [favorites, setFavorites] = useState([])
  const [messages, setMessages] = useState({ list: [], unread: 0 })
  const [history, setHistory] = useState([])
  const [pending, setPending] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [checkout, setCheckout] = useState(null)
  const [payMethod, setPayMethod] = useState('alipay')
  const [receipt, setReceipt] = useState(null)

  const isStaff = user.role === 'landlord' || user.role === 'admin'

  async function load() {
    setError('')
    try {
      // 一次性拉齐，切标签时不再等待
      const [mine, myContracts, myBills, favs, msgs, hist, pend] =
        await Promise.all([
          api.myAppointments(),
          api.myContracts(),
          api.myBills(),
          api.listFavorites(),
          api.listMessages(),
          api.history(),
          api.pendingReviews(),
        ])
      setAppointments(mine.list || [])
      setContracts(myContracts.list || [])
      setBills(myBills)
      setFavorites(favs.list || [])
      setMessages(msgs)
      setHistory(hist.list || [])
      setPending(pend.list || [])
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

  const TABS = [
    { key: 'contracts', label: isStaff ? '出租合同' : '我的租约', n: contracts.length },
    { key: 'bills', label: isStaff ? '收租' : '我的账单', n: bills.unpaid_count },
    { key: 'appointments', label: isStaff ? '收到的预约' : '我的预约', n: appointments.length },
    { key: 'messages', label: '消息', n: messages.unread },
    { key: 'favorites', label: '收藏', n: favorites.length },
    { key: 'history', label: '足迹', n: history.length },
    { key: 'reviews', label: '待评价', n: pending.length },
    { key: 'profile', label: '账号设置' },
  ]

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <h3>{user.real_name || user.username}</h3>
        <div className="loc">
          {{ tenant: '租客', landlord: '房东', admin: '管理员' }[user.role]} · {user.phone}
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
              还没有租约。{isStaff ? '确认看房预约后可以为租客发起签约。' : '看房满意后，房东会发起签约。'}
            </p>
          ) : (
            <ul className="record-list">
              {contracts.map((contract) => (
                <ContractCard
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
                <div className="sum-k">{isStaff ? '待收' : '待缴'}</div>
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

                    {bill.status === 'unpaid' && !isStaff && (
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
                    <strong>{item.house_title || item.house_no}</strong>
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
                      {isStaff && (
                        <>
                          <button
                            className="btn small"
                            onClick={() =>
                              run(`a${item.id}`, () =>
                                api.updateAppointment(item.id, 'confirmed'))
                            }
                          >
                            确认
                          </button>
                          <button
                            className="btn ghost small"
                            onClick={() =>
                              run(`a${item.id}`, () =>
                                api.updateAppointment(item.id, 'rejected'))
                            }
                          >
                            拒绝
                          </button>
                        </>
                      )}
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
                  </li>
                ))}
              </ul>
            </>
          )
        )}

        {/* ---------------- 待评价 ---------------- */}
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
    </>
  )
}
