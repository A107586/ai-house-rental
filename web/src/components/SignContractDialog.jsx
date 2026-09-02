import { useState } from 'react'
import { api } from '../api'

/**
 * 房东发起签约。
 *
 * 在这之前，创建合同的**唯一入口是接口文档页**——测试文档里明写着
 * 「在 /docs 调 POST /api/contracts」。签约是整个交易链的起点，
 * 主干流程却要去调接口，这条链在界面上就是断的。
 *
 * 表单只收四个必要参数，其余由后端算：
 * 租金取房源当前定价、押金按系统配置的默认月数、结束日按起始日推算。
 * 让房东手填这些既啰嗦又容易和房源信息对不上。
 */
function today() {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.toISOString().slice(0, 10)
}

export default function SignContractDialog({ houses, onDone, onClose }) {
  const [houseNo, setHouseNo] = useState('')
  const [tenant, setTenant] = useState('')
  const [start, setStart] = useState(today())
  const [months, setMonths] = useState(12)
  const [depositMonths, setDepositMonths] = useState('')
  const [payMonths, setPayMonths] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // 只有「可租」的房源能签。已租的再签一份会出现两份生效合同，
  // 待审核的还没上架就更不该签
  const signable = (houses || []).filter((h) => h.status === 'available')
  const picked = signable.find((h) => h.house_no === houseNo)

  async function submit(e) {
    e.preventDefault()
    if (!houseNo || !tenant.trim()) {
      setError('请选择房源并填写租客用户名。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = {
        house_no: houseNo,
        tenant_username: tenant.trim(),
        start_date: new Date(`${start}T00:00:00`).toISOString(),
        months: Number(months) || 12,
        pay_months: Number(payMonths) || 1,
      }
      // 不填就交给系统配置里的默认押金月数，别在前端写死一个 1
      if (depositMonths !== '') body.deposit_months = Number(depositMonths)
      await api.createContract(body)
      onDone()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        <h3>发起签约</h3>
        <div className="loc">合同生成后需要租客在「我的租约」里确认才生效</div>

        {error && <div className="reply failed">{error}</div>}

        {signable.length === 0 ? (
          <div className="empty">
            名下没有「可租」状态的房源。已出租的房源不能重复签约，
            待审核的要先通过审核。
          </div>
        ) : (
          <form onSubmit={submit} className="house-form">
            <label className="fld">
              <span>房源</span>
              <select value={houseNo} onChange={(e) => setHouseNo(e.target.value)}>
                <option value="">选择一套可租房源…</option>
                {signable.map((h) => (
                  <option key={h.house_no} value={h.house_no}>
                    {h.house_no} · {h.title} · {Math.round(h.rent_price)} 元/月
                  </option>
                ))}
              </select>
            </label>

            <label className="fld">
              <span>租客用户名</span>
              <input value={tenant} onChange={(e) => setTenant(e.target.value)}
                     placeholder="如 tenant1" />
            </label>

            <div className="fld-row">
              <label className="fld">
                <span>起租日</span>
                <input type="date" value={start}
                       onChange={(e) => setStart(e.target.value)} />
              </label>
              <label className="fld">
                <span>租期（月）</span>
                <input type="number" min="1" max="60" value={months}
                       onChange={(e) => setMonths(e.target.value)} />
              </label>
            </div>

            <div className="fld-row">
              <label className="fld">
                <span>押金月数</span>
                <input type="number" min="0" max="6" value={depositMonths}
                       onChange={(e) => setDepositMonths(e.target.value)}
                       placeholder="留空用系统默认" />
              </label>
              <label className="fld">
                <span>付款周期（月）</span>
                <input type="number" min="1" max="12" value={payMonths}
                       onChange={(e) => setPayMonths(e.target.value)} />
              </label>
            </div>

            {picked && (
              <div className="assist">
                <div className="assist-body">
                  月租按房源当前定价 <b>{Math.round(picked.rent_price)}</b> 元计。
                  <div className="assist-hint">
                    确认生效时会一次排满整个租期的账单：1 条押金 +
                    {' '}{months} 条租金，不用每月手工补。
                  </div>
                </div>
              </div>
            )}

            <button className="btn block" type="submit" disabled={busy}>
              {busy ? '提交中…' : '生成合同'}
            </button>
          </form>
        )}
      </aside>
    </>
  )
}
