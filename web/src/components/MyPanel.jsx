import { useEffect, useState } from 'react'
import { api } from '../api'
import ContractCard from './ContractCard'
import ProfileForm from './ProfileForm'

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
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const isStaff = user.role === 'landlord' || user.role === 'admin'

  async function load() {
    setError('')
    try {
      // 一次性拉齐，切标签时不再等待
      const [mine, myContracts, myBills, favs, msgs] = await Promise.all([
        api.myAppointments(),
        api.myContracts(),
        api.myBills(),
        api.listFavorites(),
        api.listMessages(),
      ])
      setAppointments(mine.list || [])
      setContracts(myContracts.list || [])
      setBills(myBills)
      setFavorites(favs.list || [])
      setMessages(msgs)
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

  const TABS = [
    { key: 'contracts', label: isStaff ? '出租合同' : '我的租约', n: contracts.length },
    { key: 'bills', label: isStaff ? '收租' : '我的账单', n: bills.unpaid_count },
    { key: 'appointments', label: isStaff ? '收到的预约' : '我的预约', n: appointments.length },
    { key: 'messages', label: '消息', n: messages.unread },
    { key: 'favorites', label: '收藏', n: favorites.length },
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
                          onClick={() =>
                            run(bill.bill_no, () => api.payBill(bill.bill_no, 'alipay'))
                          }
                        >
                          {busy === bill.bill_no ? '支付中' : '立即缴费'}
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
                    <strong>{item.house_no}</strong>
                    <span className={`pill status-${item.status}`}>
                      {APPOINTMENT_STATUS[item.status] || item.status}
                    </span>
                  </div>
                  <div className="record-body">
                    {item.viewing_time.slice(0, 16)} · {item.contact_name}
                  </div>
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
    </>
  )
}
