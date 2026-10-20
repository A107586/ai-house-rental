import { useEffect, useState } from 'react'

/**
 * 退租确认弹窗（K-01）。
 *
 * 这里换掉的是一个真实缺陷，不是界面美化。原先退租用的是
 * `window.prompt` 收扣款金额：
 *
 *     const deduct = Number(window.prompt('押金扣款金额…', '0') || 0)
 *     if (Number.isNaN(deduct)) { ... }
 *     act(() => api.endContract(...))
 *
 * 用户点「取消」时 `window.prompt` 返回 `null`，`null || 0` 得到 `0`，
 * `Number.isNaN(0)` 为假——于是**代码继续往下走，把租约退掉了**。
 * 一个本意是「我再想想」的动作直接结束了合同，而且退租不可撤销：
 * 未到期的租金账单会作废，房源会重新上架。
 *
 * 所以这个弹窗的第一职责不是收扣款金额，是**让「取消」真的等于取消**。
 * 关闭的三条路径（叉、取消按钮、Esc）一律不发请求。
 *
 * 另外两点：
 *
 * - **金额校验放在提交之前**，非数字、负数、超过押金都就地报错，
 *   不发请求。后端也有同样的校验（`leasing.end_contract`），
 *   前端这一层是为了让用户在原地看到问题，而不是弹窗关掉之后
 *   在卡片角落里看到一行红字。
 * - **请求失败时弹窗不关**。表单一关，用户填的原因和金额就没了，
 *   还得重头再来一遍。
 */
export default function EndLeaseDialog({ contract, isStaff, onConfirm, onClose }) {
  // 房东确认租客的退租申请时，带上租客填的原因
  const [reason, setReason] = useState(contract.request?.reason || '退租')
  const [deduct, setDeduct] = useState('0')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Esc 关闭。不可逆操作的弹窗尤其要给一条「什么都不做就退出」的快捷路径
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  const deposit = Number(contract.deposit) || 0
  // 只有出租方能扣押金，租客侧这一栏根本不出现，也就恒为 0
  const amount = isStaff ? Number(deduct) : 0
  const valid = !isStaff || (deduct.trim() !== '' && Number.isFinite(amount)
    && amount >= 0 && amount <= deposit)
  const refund = valid ? deposit - amount : null

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    if (isStaff) {
      if (deduct.trim() === '' || !Number.isFinite(amount)) {
        setError('扣款金额必须是数字，无扣款请填 0。')
        return
      }
      if (amount < 0) {
        setError('扣款金额不能为负。')
        return
      }
      if (amount > deposit) {
        setError('扣款金额不能超过押金 ' + Math.round(deposit) + ' 元。')
        return
      }
    }

    setBusy(true)
    setError('')
    try {
      await onConfirm(reason.trim() || '退租', amount)
      onClose()
    } catch (err) {
      // 失败留在弹窗里，用户填的内容不丢
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={() => !busy && onClose()} />
      <div className="dialog" role="dialog" aria-modal="true"
           aria-labelledby="end-lease-title">
        <button className="drawer-close" onClick={onClose} aria-label="关闭"
                disabled={busy}>×</button>
        <h3 id="end-lease-title">{isStaff ? '确认退租' : '申请退租'}</h3>

        {/* 租客这一步只是提申请：房东确认时才结算押金、作废账单 */}
        <div className="assist warn">
          <div className="assist-body">
            {isStaff ? <>退租<b>不可撤销</b>。</> : '提交后等房东确认，确认前合同照常有效。'}
            <div className="assist-hint">
              {isStaff
                ? '未到期的租金账单会作废，房源重新上架，押金按下面金额结算。'
                : '房东确认时结算押金；提前退租可能需要按合同支付违约金。'}
            </div>
          </div>
        </div>

        {error && <div className="reply failed">{error}</div>}

        <form onSubmit={submit} className="house-form">
          <label className="fld">
            <span>退租原因</span>
            <input value={reason} maxLength={100}
                   onChange={(e) => setReason(e.target.value)}
                   placeholder="如：租期届满 / 提前退租" autoFocus />
          </label>

          {isStaff && (
            <label className="fld">
              <span>押金扣款（元）</span>
              <input type="number" min="0" max={deposit} step="1"
                     value={deduct}
                     onChange={(e) => { setDeduct(e.target.value); setError('') }} />
              <span className="fld-hint">
                押金共 {Math.round(deposit)} 元，无扣款填 0。
              </span>
            </label>
          )}

          {/* 押金怎么算的要当场摊开。退租时用户最关心的就是「我能拿回多少」，
              让他自己去减一次是没必要的，也容易和后端算出来的数对不上 */}
          <dl className="settle">
            <div><dt>押金</dt><dd>{Math.round(deposit)} 元</dd></div>
            {isStaff && (
              <div>
                <dt>扣款</dt>
                <dd>{valid && amount > 0 ? '−' + Math.round(amount) : '0'} 元</dd>
              </div>
            )}
            <div className="settle-total">
              <dt>预计退还</dt>
              <dd>{refund === null ? '—' : Math.round(refund) + ' 元'}</dd>
            </div>
          </dl>

          <div className="fld-row">
            <button type="button" className="btn ghost block"
                    onClick={onClose} disabled={busy}>
              取消
            </button>
            <button type="submit" className="btn block danger" disabled={busy}>
              {busy ? '处理中…' : isStaff ? '确认退租' : '提交申请'}
            </button>
          </div>
        </form>
      </div>
    </>
  )
}
