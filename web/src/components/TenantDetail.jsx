import { useEffect, useState } from 'react'
import { api } from '../api'

const CONTRACT_STATUS = {
  pending: '待确认', active: '生效中', ended: '已结束', cancelled: '已取消',
}
const REPAIR_STATUS = { pending: '待处理', processing: '处理中', done: '已完成' }

/**
 * 租客详情（房东视角）：他和我之间的合同、账单、报修。
 *
 * 原先租客列表点了没反应，房东想知道「他哪笔没交」「上次报修修好没」，
 * 要去合同、工单两个栏目各翻一遍再自己对。
 */
export default function TenantDetail({ tenantId, onClose, onOpenContract, onOpenHouse, onTalk }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    api.landlordTenant(tenantId)
      .then((d) => { if (alive) setData(d) })
      .catch((err) => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [tenantId])

  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function remind() {
    setBusy(true)
    setError('')
    try {
      const r = await api.remindTenant(tenantId)
      setOk(`已提醒，${r.count} 笔逾期共 ${r.amount} 元。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const t = data?.tenant
  const s = data?.summary
  const active = (data?.contracts || []).find((c) => c.status === 'active')

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer tenant-detail" role="dialog" aria-modal="true">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        {!data && !error && <div className="empty">加载中…</div>}
        {error && <div className="reply failed">{error}</div>}

        {data && (
          <>
            <h3>{t.real_name || t.username}</h3>
            <div className="loc">{t.phone || '未留手机号'}</div>

            <div className="td-stats">
              <div><b>{s.paid_total}</b><span>已收（元）</span></div>
              <div><b>{s.unpaid_total}</b><span>待收（元）</span></div>
              <div className={s.overdue_count ? 'bad' : ''}>
                <b>{s.overdue_count ? s.overdue_total : 0}</b>
                <span>{s.overdue_count ? `逾期 ${s.overdue_count} 笔` : '无逾期'}</span>
              </div>
            </div>

            {ok && <div className="reply created">{ok}</div>}

            <div className="record-actions">
              {onTalk && active && (
                <button className="btn small" onClick={() => onTalk(active.house_no, t.username)}>
                  发消息
                </button>
              )}
              {s.overdue_count > 0 && (
                <button className="btn ghost small" disabled={busy} onClick={remind}>
                  {busy ? '发送中…' : '提醒缴费'}
                </button>
              )}
            </div>

            <div className="section-title">合同</div>
            <ul className="record-list compact">
              {data.contracts.map((c) => (
                <li key={c.contract_no}>
                  <div className="record-head">
                    <button className="link-title" onClick={() => onOpenHouse?.(c.house_no)}>
                      {c.house_title || c.house_no}
                    </button>
                    <span className="pill">{CONTRACT_STATUS[c.status] || c.status}</span>
                  </div>
                  <div className="record-body">
                    {c.start_date} 至 {c.end_date} · 月租 {c.rent} 元
                    {c.status === 'active' && ` · 剩 ${c.days_left} 天`}
                    {c.request && ` · 有${c.request.type === 'end' ? '退租' : '续租'}申请待处理`}
                  </div>
                  {onOpenContract && (
                    <div className="record-actions">
                      <button className="btn ghost small" onClick={() => onOpenContract(c.contract_no)}>
                        查看合同档案
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>

            <div className="section-title">账单</div>
            {data.bills.length === 0 ? (
              <p className="desc">暂无账单</p>
            ) : (
              <table className="log-table td-bills">
                <thead>
                  <tr><th>期数</th><th>房源</th><th>金额</th><th>到期日</th><th>状态</th></tr>
                </thead>
                <tbody>
                  {data.bills.map((b) => (
                    <tr key={b.bill_no} className={b.overdue ? 'bad' : ''}>
                      <td>{b.type === 'deposit' ? '押金' : b.period}</td>
                      <td>{b.house_no}</td>
                      <td>{b.amount}</td>
                      <td>{b.due_date}</td>
                      <td>{b.status === 'paid' ? '已缴' : b.overdue ? '逾期' : '待缴'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">报修</div>
            {data.repairs.length === 0 ? (
              <p className="desc">暂无报修</p>
            ) : (
              <ul className="record-list compact">
                {data.repairs.map((r) => (
                  <li key={r.id}>
                    <div className="record-head">
                      <strong>{r.category || '报修'} · {r.house_no}</strong>
                      <span className="pill">{REPAIR_STATUS[r.status] || r.status}</span>
                    </div>
                    <div className="record-body">{r.created_at} · {r.content}</div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </>
  )
}
