import { useState } from 'react'
import { api } from '../api'
import EndLeaseDialog from './EndLeaseDialog'
import SignContractDialog from './SignContractDialog'

const STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  active: { label: '生效中', cls: 'status-confirmed' },
  ended: { label: '已结束', cls: 'status-cancelled' },
  cancelled: { label: '已取消', cls: 'status-rejected' },
}

export default function ContractCard({ contract, role, onChanged, onOpenHouse,
                                      onOpenDetail }) {
  const [showText, setShowText] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [review, setReview] = useState(null)
  const [reviewing, setReviewing] = useState(false)
  const [ending, setEnding] = useState(false)
  const [editing, setEditing] = useState(false)

  async function runReview() {
    if (reviewing) return
    setReviewing(true)
    setError('')
    try {
      const result = await api.agentContractReview(contract.content)
      setReview(result)

      // 把结论摘要回写到合同上，下次打开列表不用重跑一遍就能看到分数。
      // 回写失败不影响本次展示——结果已经在眼前了。
      try {
        await api.saveReviewResult(contract.contract_no, {
          score: result.score ?? 0,
          summary: result.message || '',
          risk_count: (result.risks || []).length,
          good_count: (result.favourable || []).length,
          source: result.source || '',
        })
        onChanged?.()
      } catch {
        // 静默：审阅结果本身已经显示出来了，存不存得下是次要的
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setReviewing(false)
    }
  }

  // 已经审过的合同直接显示上次的结论，不用每次都重跑
  const saved = contract.review_result

  const isTenant = role === 'tenant'
  const isStaff = role === 'landlord' || role === 'admin'
  // 已到期、还没办退租：状态仍是「生效中」，但要单独标出来——押金要结算、
  // 屋子要验收，这一步得房东办。原先只显示「剩余 0 天」，谁也不知道该做什么
  const expired = contract.status === 'active' && contract.expired
  const status = expired
    ? { label: '已到期', cls: 'status-pending' }
    : STATUS[contract.status] || { label: contract.status, cls: '' }

  // 剩余不足 30 天就提醒，这是租约到期提醒的触发线
  const expiring = contract.status === 'active' && !expired && contract.days_left <= 30

  // 待处理的退租 / 续租申请。mine：是我这一方提的（我只能撤回，确认按钮在对方那边）
  const request = contract.request || null
  const mySide = isTenant ? 'tenant' : 'landlord'
  const mine = request && request.by === mySide
  const byLabel = request?.by === 'tenant' ? '租客' : '房东'
  const otherLabel = isTenant ? '房东' : '租客'
  const landlordEnd = request?.type === 'end' && request?.by === 'landlord'
  const requestWhat = request?.type === 'renew' ? `续租 ${request.months} 个月`
    : landlordEnd ? '提前退租' : '退租'

  // 退租弹窗的四种情形（2026-10-22 起房东提前退租也要租客同意）：
  //   tenant_request   租客提申请，等房东确认
  //   landlord_request 房东在租期内提前退租：写明原因与押金拟扣，等租客同意
  //   settle           房东确认租客的申请 / 办理到期退租 / 管理员处理：当场结算
  //   tenant_accept    租客同意房东的提前退租，押金按房东写明的扣款结算
  const directEnd = role === 'admin' || expired

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

  /** 退租。**请求由弹窗按「确认退租」时发起**，这里只负责把它打开。
   *
   * 原先这里是 `window.prompt`，点「取消」返回 `null`，`null || 0` 得到 0，
   * 于是取消也照样把租约退掉了（K-01）。退租不可撤销，这条路径必须是
   * 「用户明确按下确认」才走得通。 */
  async function endLease(reason, deduct) {
    await api.endContract(contract.contract_no, reason, deduct)
    onChanged()
  }

  return (
    <li>
      <div className="record-head">
        {/* 房源名可点。没传 onOpenHouse 的地方退回纯文本，
            不至于渲染出一个点了没反应的按钮 */}
        {onOpenHouse ? (
          <button
            className="link-title"
            onClick={() => onOpenHouse(contract.house_no)}
          >
            {contract.house_title || contract.house_no}
          </button>
        ) : (
          <strong>{contract.house_title || contract.house_no}</strong>
        )}
        <span className={`pill ${status.cls}`}>{status.label}</span>
      </div>

      <div className="record-body">
        {contract.start_date} 至 {contract.end_date} · 月租 {contract.rent} 元
        {contract.list_rent != null && contract.list_rent !== contract.rent &&
          `（挂牌 ${contract.list_rent}）`} ·
        押金 {contract.deposit} 元
        {contract.pay_months > 1 && ` · 每 ${contract.pay_months} 个月一付`}
        {/* 佣金是房东和中介之间的事，租客那边不显示 */}
        {isStaff && contract.commission_rate != null &&
          ` · 约定佣金 ${contract.commission_rate}%`}
      </div>

      {contract.status === 'active' && !expired && (
        <div className={`record-body ${expiring ? 'warn-text' : ''}`}>
          剩余 {contract.days_left} 天
          {expiring && ' · 即将到期，可发起续租'}
        </div>
      )}
      {expired && (
        <div className="record-body warn-text">
          已于 {contract.end_date} 到期 ·{' '}
          {isTenant ? '等房东办理退租结算，也可以申请续租' : '请办理退租结算，或与租客续租'}
        </div>
      )}

      {/* deposit_refund 是实际退还的押金；押金没收到过就是空的，不能写成「已退还」 */}
      {contract.status === 'ended' && (
        <div className="record-body">
          {contract.deposit_refund !== null ? (
            <>
              已退还押金 {contract.deposit_refund} 元
              {contract.deposit - contract.deposit_refund > 0 &&
                `（扣款 ${(contract.deposit - contract.deposit_refund).toFixed(0)} 元）`}
            </>
          ) : contract.deposit > 0 ? '押金未缴纳，无需退还' : '免押'}
          {contract.end_reason && ` · ${contract.end_reason}`}
        </div>
      )}
      {contract.status === 'cancelled' && contract.end_reason && (
        <div className="record-body">{contract.end_reason}</div>
      )}

      {error && <div className="reply failed">{error}</div>}

      <div className="record-actions">
        {onOpenDetail && (
          <button
            className="btn ghost small"
            onClick={() => onOpenDetail(contract.contract_no)}
          >
            查看档案
          </button>
        )}

        <button className="btn ghost small" onClick={() => setShowText(!showText)}>
          {showText ? '收起合同' : '查看合同'}
        </button>

        <button className="btn ghost small" onClick={runReview} disabled={reviewing}>
          {reviewing ? '审阅中…' : saved ? '重新审阅' : '智能审阅'}
        </button>

        {/* 已审阅过就把上次的分数摆出来。每次点进来都要重跑一遍才知道
            分数，那这个字段存了也等于没存 */}
        {saved && !review && (
          <span className="review-badge" title={saved.summary}>
            已审阅 · {saved.score} 分
            {saved.risk_count > 0 && ` · ${saved.risk_count} 条风险`}
            <span className="review-time">{saved.reviewed_at}</span>
          </span>
        )}

        {contract.status === 'pending' && isTenant && (
          <>
            <button
              className="btn small"
              disabled={busy}
              onClick={() => act(() => api.activateContract(contract.contract_no,
                                                            contract.terms_digest))}
            >
              {busy ? '处理中' : '确认签约'}
            </button>
            <button
              className="btn ghost small"
              disabled={busy}
              onClick={() => window.confirm('不接受这份合同？房东会收到通知，合同随即作废。')
                && act(() => api.cancelContract(contract.contract_no))}
            >
              拒签
            </button>
          </>
        )}

        {contract.status === 'pending' && isStaff && (
          <>
            <button className="btn ghost small" onClick={() => setEditing(true)}>
              修改条款
            </button>
            <button
              className="btn ghost small"
              disabled={busy}
              onClick={() => window.confirm('撤回这份合同？租客会收到通知，合同随即作废。')
                && act(() => api.cancelContract(contract.contract_no))}
            >
              撤回
            </button>
            <span className="own">等待租客确认</span>
          </>
        )}

        {/* 退租 / 续租：一方提，另一方确认。没有待处理的申请时两边都能发起 */}
        {contract.status === 'active' && !request && (
          <>
            <button className="btn ghost small" disabled={busy}
                    onClick={() => setEnding(isTenant ? 'tenant_request'
                      : directEnd ? 'settle' : 'landlord_request')}>
              {isTenant ? '申请退租' : directEnd ? '办理退租' : '提前退租'}
            </button>
            <button
              className="btn ghost small"
              disabled={busy}
              onClick={() => act(() => api.renewContract(contract.contract_no, 12))}
            >
              申请续租一年
            </button>
          </>
        )}
      </div>

      {contract.status === 'active' && request && (
        <div className={`lease-request ${mine ? '' : 'todo'}`}>
          <span>
            {mine
              ? `你已申请${requestWhat}，等${otherLabel}确认`
              : `${byLabel}申请${requestWhat}`}
            {request.reason && request.type === 'end' && `（${request.reason}）`}
            {landlordEnd && ` · 押金拟扣 ${Math.round(request.deduct || 0)} 元`}
            <em>{request.at}</em>
          </span>
          <span className="lease-request-actions">
            {mine ? (
              <button className="btn ghost small" disabled={busy}
                      onClick={() => act(() => api.withdrawContractRequest(contract.contract_no))}>
                撤回申请
              </button>
            ) : (
              <>
                {request.type === 'end' ? (
                  // 房东确认租客的申请时在弹窗里结算押金；
                  // 租客同意房东的提前退租时，押金按房东写明的扣款结算
                  <button className="btn small" disabled={busy}
                          onClick={() => setEnding(landlordEnd ? 'tenant_accept' : 'settle')}>
                    {landlordEnd ? '同意退租' : '确认退租'}
                  </button>
                ) : (
                  <button className="btn small" disabled={busy}
                          onClick={() => act(() => api.renewContract(contract.contract_no))}>
                    同意续租
                  </button>
                )}
                <button className="btn ghost small" disabled={busy}
                        onClick={() => act(() => api.withdrawContractRequest(contract.contract_no))}>
                  不同意
                </button>
              </>
            )}
          </span>
        </div>
      )}

      {review && (
        <div className="review-panel">
          <div className="review-score">
            <span className={`score-num ${review.score >= 80 ? 'good'
              : review.score >= 50 ? 'warn' : 'bad'}`}>
              {review.score}
            </span>
            <span className="score-unit">/ 100</span>
            <span className="score-note">
              {review.source === 'llm' ? 'AI 审阅' : '按常见条款自动检查'}
            </span>
          </div>

          <p className="record-body">{review.message}</p>

          <div className="clause-grid">
            {Object.entries(review.clauses).map(([k, v]) => (
              <div className="clause" key={k}>
                <div className="clause-k">{k}</div>
                <div className="clause-v">{String(v)}</div>
              </div>
            ))}
          </div>

          {review.risks.length > 0 && (
            <ul className="risk-list">
              {review.risks.map((r) => (
                <li key={r.id} className={`risk-${r.level}`}>
                  <div className="risk-head">
                    <span className={`pill risk-tag risk-${r.level}`}>
                      {r.level === 'high' ? '签前必谈'
                        : r.level === 'medium' ? '需注意' : '提示'}
                    </span>
                    <strong>{r.title}</strong>
                    {r.source === 'llm' && <span className="pill ai-tag">AI 补充</span>}
                  </div>
                  <div className="risk-quote">「{r.quote}」</div>
                  <div className="record-body">{r.why}</div>
                  <div className="risk-advice">建议：{r.advice}</div>
                </li>
              ))}
            </ul>
          )}

          {/* E1：有保障的条款也要列出来。
              **只报风险的审阅是偏的**——用户看不到「这份合同哪里写得清楚」，
              就没法判断它到底比别的好在哪，也就理解不了那个分数是怎么来的。 */}
          {(review.favourable || []).length > 0 && (
            <ul className="good-list">
              {review.favourable.map((g) => (
                <li key={g.id}>
                  <div className="risk-head">
                    <span className="pill good-tag">写清楚了</span>
                    <strong>{g.title}</strong>
                  </div>
                  <div className="record-body">{g.note}</div>
                </li>
              ))}
            </ul>
          )}

          <button className="btn ghost small" onClick={() => setReview(null)}>
            收起审阅结果
          </button>
        </div>
      )}

      {showText && <pre className="contract-text">{contract.content}</pre>}

      {editing && (
        <SignContractDialog
          contract={contract}
          onClose={() => setEditing(false)}
          onDone={() => {
            setEditing(false)
            onChanged()
          }}
        />
      )}

      {ending && (
        <EndLeaseDialog
          contract={contract}
          mode={ending}
          onConfirm={endLease}
          onClose={() => setEnding(false)}
        />
      )}
    </li>
  )
}
