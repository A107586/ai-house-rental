import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 合同详情：一份租约的完整档案（待完善清单 A-4）。
 *
 * 「我的租约」原先只有一张摘要卡——起止日期、月租、押金、剩余天数。
 * 缺的是「这份合同上发生过什么」：排了几期账单、交了几期、租期内报过
 * 几次修、状态什么时候变的。账单和合同原本是两个互不相干的列表，
 * 用户得自己在脑子里对应。
 *
 * 三块数据由后端一个接口一起返回，不是前端拉三次——它们在界面上同时
 * 出现，分三次请求既慢，也会出现「账单加载好了、报修还在转」的割裂感。
 */
const BILL_TYPE = { rent: '租金', deposit: '押金', utility: '水电', other: '其它' }

const REPAIR_STATUS = {
  pending: '待处理', processing: '处理中', done: '已完成',
}

const CONTRACT_STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  active: { label: '生效中', cls: 'status-confirmed' },
  ended: { label: '已结束', cls: 'status-cancelled' },
  cancelled: { label: '已取消', cls: 'status-rejected' },
}

const money = (value) => Math.round(Number(value) || 0)

function billState(bill) {
  if (bill.status === 'paid') return { label: '已缴', cls: 'status-confirmed' }
  if (bill.status === 'cancelled') return { label: '已作废', cls: '' }
  if (bill.overdue) return { label: '已逾期', cls: 'status-rejected' }
  return { label: '待缴', cls: 'status-pending' }
}

// asPayee：从房东工作台或管理端打开（收款方），账单上给「已线下收款」
export default function ContractDetail({ contractNo, onClose, onOpenHouse, asPayee = false }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let alive = true
    api
      .contractDetail(contractNo)
      .then((body) => { if (alive) setData(body) })
      .catch((err) => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [contractNo, reload])

  async function markReceived(bill) {
    if (busy) return
    if (!window.confirm(`确认已线下收到这笔 ${money(bill.amount)} 元？确认后账单记为已缴，租客会收到通知。`)) return
    setBusy(bill.bill_no)
    setError('')
    try {
      await api.offlineReceipt(bill.bill_no)
      setReload((n) => n + 1)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const contract = data?.contract
  const sum = data?.summary
  const status = contract
    ? (CONTRACT_STATUS[contract.status] || { label: contract.status, cls: '' })
    : null

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer wide">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        <h3>租约档案</h3>

        {error && <div className="reply failed">{error}</div>}
        {!data && !error && <div className="empty">加载中…</div>}

        {contract && (
          <>
            <div className="loc">
              <button
                className="link-title"
                onClick={() => onOpenHouse && onOpenHouse(contract.house_no)}
              >
                {contract.house_title || contract.house_no}
              </button>
              {' · '}{contract.contract_no}
              {' '}<span className={`pill ${status.cls}`}>{status.label}</span>
            </div>

            {/* ---------- 一眼看清这份租约的钱 ---------- */}
            <div className="dash-grid">
              <div className="dcell">
                <div className="dk">租期</div>
                <div className="dv small">{contract.start_date}</div>
                <div className="dk">至 {contract.end_date}</div>
              </div>
              <div className="dcell">
                <div className="dk">已缴</div>
                <div className="dv">{money(sum.paid_amount)}</div>
                <div className="dk">{sum.paid_count} / {sum.total_periods} 期</div>
              </div>
              <div className="dcell">
                <div className="dk">待缴</div>
                <div className="dv accent">{money(sum.unpaid_amount)}</div>
                <div className="dk">{sum.unpaid_count} 期</div>
              </div>
              <div className="dcell">
                <div className="dk">月租 / 押金</div>
                <div className="dv small">
                  {money(contract.rent)} / {money(contract.deposit)}
                </div>
              </div>
            </div>

            {sum.overdue_count > 0 && (
              <div className="reply failed">
                有 {sum.overdue_count} 期账单已逾期，请尽快缴纳。
              </div>
            )}

            {/* ---------- 账单明细 ---------- */}
            <section className="cd-sec">
              <h5>账单明细</h5>
              <div className="table-wrap">
                <table className="log-table">
                  <thead>
                    <tr>
                      <th>期次</th><th>类型</th><th>金额</th>
                      <th>到期日</th><th>状态</th><th>缴费信息</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.bills.map((bill, index) => {
                      const st = billState(bill)
                      return (
                        <tr key={bill.bill_no}
                            className={st.label === '已逾期' ? 'bad' : ''}>
                          <td>{index + 1}</td>
                          <td>
                            {BILL_TYPE[bill.type] || bill.type}
                            {bill.period && (
                              <div className="muted-note">{bill.period}</div>
                            )}
                          </td>
                          <td className="accent"><b>{money(bill.amount)}</b></td>
                          <td className="mono">{bill.due_date}</td>
                          <td><span className={`pill ${st.cls}`}>{st.label}</span></td>
                          <td>
                            {/* 交易号是租客截图存证时唯一有价值的信息 */}
                            {bill.payment ? (
                              <>
                                <div className="mono">{bill.payment.trade_no}</div>
                                <div className="muted-note">
                                  {bill.paid_at}
                                </div>
                              </>
                            ) : asPayee && bill.status === 'unpaid'
                                && ['active', 'ended'].includes(contract.status) ? (
                              // 合同结束后的欠款线上付不了，只能线下结；结清了要有地方记下来
                              <button className="btn ghost small" disabled={!!busy}
                                      onClick={() => markReceived(bill)}>
                                {busy === bill.bill_no ? '登记中' : '已线下收款'}
                              </button>
                            ) : <span className="muted-note">—</span>}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>

            {/* ---------- 租期内的报修 ---------- */}
            <section className="cd-sec">
              <h5>租期内报修（{data.repairs.length}）</h5>
              {data.repairs.length === 0 ? (
                <div className="empty">这份租约期间没有报修记录。</div>
              ) : (
                <ul className="record-list">
                  {data.repairs.map((item) => (
                    <li key={item.id}>
                      <div className="record-head">
                        <strong>{item.content}</strong>
                        {item.urgency === '紧急' && (
                          <span className="pill urgent">紧急</span>
                        )}
                        <span className="pill">
                          {REPAIR_STATUS[item.status] || item.status}
                        </span>
                      </div>
                      {item.result ? (
                        <div className="record-body done-note">
                          {item.result.finished_at} 由 {item.result.worker_name} 处理完成
                          {item.result.note && `：${item.result.note}`}
                        </div>
                      ) : (
                        <div className="record-body muted-note">
                          提交于 {item.created_at}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* ---------- 时间线 ---------- */}
            <section className="cd-sec">
              <h5>状态时间线</h5>
              <ol className="timeline">
                {data.timeline.map((item, index) => (
                  <li key={`${item.at}-${index}`}>
                    <div className="tl-at mono">{item.at}</div>
                    <div className="tl-body">
                      <b>{item.title}</b>
                      {item.detail && <span>{item.detail}</span>}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </>
        )}
      </aside>
    </>
  )
}
