import { useState } from 'react'
import { api } from '../api'

const STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  active: { label: '生效中', cls: 'status-confirmed' },
  ended: { label: '已结束', cls: 'status-cancelled' },
  cancelled: { label: '已取消', cls: 'status-rejected' },
}

export default function ContractCard({ contract, role, onChanged }) {
  const [showText, setShowText] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const isTenant = role === 'tenant'
  const isStaff = role === 'landlord' || role === 'admin'
  const status = STATUS[contract.status] || { label: contract.status, cls: '' }

  // 剩余不足 30 天就提醒，这是租约到期提醒的触发线
  const expiring = contract.status === 'active' && contract.days_left <= 30

  async function act(fn) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await fn()
      onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function endLease() {
    // 扣押金是出租方的权利，租客侧不出现扣款输入
    const deduct = isStaff
      ? Number(window.prompt('押金扣款金额（无扣款填 0）', '0') || 0)
      : 0
    if (Number.isNaN(deduct)) {
      setError('扣款金额必须是数字')
      return
    }
    act(() => api.endContract(contract.contract_no, '退租', deduct))
  }

  return (
    <li>
      <div className="record-head">
        <strong>{contract.house_title || contract.house_no}</strong>
        <span className={`pill ${status.cls}`}>{status.label}</span>
      </div>

      <div className="record-body">
        {contract.start_date} 至 {contract.end_date} · 月租 {contract.rent} 元 ·
        押金 {contract.deposit} 元
        {contract.pay_months > 1 && ` · 每 ${contract.pay_months} 个月一付`}
      </div>

      {contract.status === 'active' && (
        <div className={`record-body ${expiring ? 'warn-text' : ''}`}>
          剩余 {contract.days_left} 天
          {expiring && ' · 即将到期，可发起续租'}
        </div>
      )}

      {contract.status === 'ended' && contract.deposit_refund !== null && (
        <div className="record-body">
          已退还押金 {contract.deposit_refund} 元
          {contract.deposit - contract.deposit_refund > 0 &&
            `（扣款 ${(contract.deposit - contract.deposit_refund).toFixed(0)} 元）`}
          {contract.end_reason && ` · ${contract.end_reason}`}
        </div>
      )}

      {error && <div className="reply failed">{error}</div>}

      <div className="record-actions">
        <button className="btn ghost small" onClick={() => setShowText(!showText)}>
          {showText ? '收起合同' : '查看合同'}
        </button>

        {contract.status === 'pending' && isTenant && (
          <button
            className="btn small"
            disabled={busy}
            onClick={() => act(() => api.activateContract(contract.contract_no))}
          >
            {busy ? '处理中' : '确认签约'}
          </button>
        )}

        {contract.status === 'pending' && isStaff && (
          <span className="own">等待租客确认</span>
        )}

        {contract.status === 'active' && (
          <>
            <button className="btn ghost small" disabled={busy} onClick={endLease}>
              退租
            </button>
            <button
              className="btn ghost small"
              disabled={busy}
              onClick={() => act(() => api.renewContract(contract.contract_no, 12))}
            >
              续租一年
            </button>
          </>
        )}
      </div>

      {showText && <pre className="contract-text">{contract.content}</pre>}
    </li>
  )
}
