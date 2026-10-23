import { useEffect, useState } from 'react'
import { api } from '../api'

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
 *
 * ## 四种情形（`mode`）
 *
 * 2026-10-22 起房东在租期内退租也要租客同意，原先房东点了就退、押金扣多少自己定：
 *
 * - `tenant_request`   租客提申请，等房东确认，不填扣款；
 * - `landlord_request` 房东提前退租：写明原因与**押金拟扣**，等租客同意；
 * - `settle`           房东确认租客的申请、办理到期退租，或管理员处理：当场结算；
 * - `tenant_accept`    租客同意房东的提前退租：原因与扣款是房东写的，这里只读。
 */
const MODES = {
  tenant_request: {
    title: '申请退租', button: '提交申请', editDeduct: false,
    warn: '提交后等房东确认，确认前合同照常有效。',
    hint: '房东确认时结算押金；提前退租可能需要按合同支付违约金。',
  },
  landlord_request: {
    title: '申请提前退租', button: '发给租客确认', editDeduct: true,
    warn: <>租约还没到期，提前退租要<b>租客同意</b>才生效。</>,
    hint: '按合同第六条，提前解约应提前 30 日书面通知对方并支付一个月租金的违约金，请先与租客协商。'
      + '押金拟扣金额会写进申请，租客同意的就是这个数。',
  },
  settle: {
    title: '确认退租', button: '确认退租', editDeduct: true,
    warn: <>退租<b>不可撤销</b>。</>,
    hint: '未到期的租金账单会作废，房源重新上架，押金按下面金额结算。',
  },
  tenant_accept: {
    title: '同意提前退租', button: '同意退租', editDeduct: false,
    warn: <>确认后合同<b>立即结束</b>，不可撤销。</>,
    hint: '未到期的租金账单作废，押金按房东写明的扣款结算。不同意请关掉弹窗，在合同上点「不同意」。',
  },
}

export default function EndLeaseDialog({ contract, mode = 'tenant_request', onConfirm, onClose }) {
  const conf = MODES[mode] || MODES.tenant_request
  const request = contract.request || {}
  const accepting = mode === 'tenant_accept'
  // 确认对方的申请时带上对方填的原因；同意房东申请时原因与扣款都是房东写的
  const [reason, setReason] = useState(request.reason || (contract.expired ? '租期届满' : '退租'))
  const [deduct, setDeduct] = useState(accepting ? String(request.deduct || 0) : '0')
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
  // 租客提申请时这一栏根本不出现，恒为 0
  const amount = conf.editDeduct || accepting ? Number(deduct) : 0
  const valid = !conf.editDeduct || (deduct.trim() !== '' && Number.isFinite(amount)
    && amount >= 0 && amount <= deposit)

  // 结算预览：押金、已付未住的租金、作废与折算的账单、还欠多少——
  // 和点了确认之后后端真正做的是同一份计算（GET /contracts/{no}/settlement）。
  // 原先这里只算「押金 − 扣款」，提前退租退回的预付租金、按天折算的欠款一概看不见
  const [plan, setPlan] = useState(null)
  const [planError, setPlanError] = useState('')
  useEffect(() => {
    if (!valid) return undefined
    let alive = true
    const timer = setTimeout(() => {
      api.contractSettlement(contract.contract_no, accepting ? 0 : amount)
        .then((d) => { if (alive) { setPlan(d.settlement); setPlanError('') } })
        .catch((err) => { if (alive) { setPlan(null); setPlanError(err.message) } })
    }, 250)
    return () => { alive = false; clearTimeout(timer) }
  }, [contract.contract_no, amount, valid, accepting])

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    if (conf.editDeduct) {
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
      // 同意房东申请时扣款不由租客传——后端按申请里写明的数结算
      await onConfirm(reason.trim() || '退租', accepting ? 0 : amount)
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
        <h3 id="end-lease-title">
          {mode === 'settle' && contract.expired ? '办理到期退租' : conf.title}
        </h3>

        <div className="assist warn">
          <div className="assist-body">
            {conf.warn}
            <div className="assist-hint">{conf.hint}</div>
          </div>
        </div>

        {error && <div className="reply failed">{error}</div>}

        <form onSubmit={submit} className="house-form">
          {accepting ? (
            <div className="fld">
              <span>房东写的原因</span>
              <div className="record-body">{request.reason || '（未填写）'}</div>
            </div>
          ) : (
            <label className="fld">
              <span>退租原因</span>
              <input value={reason} maxLength={100}
                     onChange={(e) => setReason(e.target.value)}
                     placeholder="如：租期届满 / 提前退租" autoFocus />
            </label>
          )}

          {conf.editDeduct && (
            <label className="fld">
              <span>{mode === 'landlord_request' ? '押金拟扣（元）' : '押金扣款（元）'}</span>
              <input type="number" min="0" max={deposit} step="1"
                     value={deduct}
                     onChange={(e) => { setDeduct(e.target.value); setError('') }} />
              <span className="fld-hint">
                押金共 {Math.round(deposit)} 元，无扣款填 0。
              </span>
            </label>
          )}

          {/* 钱怎么算的要当场摊开。退租时用户最关心的就是「我能拿回多少、还欠多少」，
              让他自己去算是没必要的，也容易和后端算出来的数对不上 */}
          <SettlementList plan={valid ? plan : null} error={planError}
                          showDeduct={conf.editDeduct || accepting} />

          <div className="fld-row">
            <button type="button" className="btn ghost block"
                    onClick={onClose} disabled={busy}>
              取消
            </button>
            <button type="submit" className="btn block danger" disabled={busy}>
              {busy ? '处理中…' : conf.button}
            </button>
          </div>
        </form>
      </div>
    </>
  )
}


/** 以「今天退租」为准的结算明细。金额全部来自后端，前端不再自己减一遍。 */
function SettlementList({ plan, error, showDeduct }) {
  if (error) return <div className="reply failed">结算预览没算出来：{error}</div>
  if (!plan) return <p className="desc">正在计算结算明细…</p>
  const yuan = (v) => `${Math.round(v * 100) / 100} 元`
  return (
    <dl className="settle">
      <div>
        <dt>押金</dt>
        <dd>{plan.deposit_paid ? yuan(plan.deposit) : '未缴纳，作废，无需再缴'}</dd>
      </div>
      {plan.deposit_paid && showDeduct && (
        <div><dt>扣款</dt><dd>{plan.deduct > 0 ? '−' + yuan(plan.deduct) : '0 元'}</dd></div>
      )}
      {plan.deposit_paid && (
        <div><dt>退还押金</dt><dd>{yuan(plan.deposit_refund)}</dd></div>
      )}
      {plan.rent_refunds.map((r) => (
        <div key={r.bill_no}>
          <dt>退回预付租金 {r.period}</dt>
          <dd>
            {yuan(r.refund)}
            <span className="fld-hint">
              {r.lived_days > 0 ? ` 已住 ${r.lived_days}/${r.total_days} 天` : ' 还没开始住'}
            </span>
          </dd>
        </div>
      ))}
      {plan.prorated.map((p) => (
        <div key={p.bill_no}>
          <dt>本期租金按天折算 {p.period}</dt>
          <dd>{yuan(p.original)} → {yuan(p.amount)}
            <span className="fld-hint"> 住 {p.lived_days}/{p.total_days} 天</span></dd>
        </div>
      ))}
      {plan.cancelled.length > 0 && (
        <div><dt>作废的账单</dt><dd>{plan.cancelled.length} 张</dd></div>
      )}
      {plan.arrears > 0 && (
        <div>
          <dt>仍欠的费用（住过期间的房租、加收的水电等）</dt>
          <dd className="warn-text">{yuan(plan.arrears)}（合同结束后线下结清）</dd>
        </div>
      )}
      <div className="settle-total">
        <dt>合计退给租客</dt>
        <dd>{yuan(plan.refund_total)}</dd>
      </div>
    </dl>
  )
}
