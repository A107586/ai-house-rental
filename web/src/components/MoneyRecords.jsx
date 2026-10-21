/**
 * 收支记录：真正发生过的每一笔钱——缴费成功的，和退回去的（GET /api/money/records）。
 *
 * 原先缴费凭证只能一张张点开账单看；退款有接口（/api/refunds/mine）却没有任何页面用它，
 * 押金退了多少、提前退租退回多少预付租金、哪天退的，租客房东都看不到（2026-10-22）。
 *
 * `direction` 由后端按当前这一方写好：租客交钱是 out、收到退款是 in；房东正好相反。
 * 管理员看全平台，不分方向。失败、超时关单的支付尝试不在这里——
 * 那些没有发生资金流动，挂在各自账单的「支付记录」里。
 */
const HEAD = {
  tenant: (m) => `收支记录 · 已交 ${m.total_out} 元 · 已退回 ${m.total_in} 元`,
  landlord: (m) => `收支记录 · 已收 ${m.total_in} 元 · 已退出 ${m.total_out} 元`,
  admin: (m) => `资金流水（${m.total} 笔）`,
}

export default function MoneyRecords({ money, side = 'tenant', limit = 50 }) {
  if (!money || !money.list || !money.list.length) return null
  const rows = money.list.slice(0, limit)
  return (
    <>
      <div className="section-title">{(HEAD[side] || HEAD.tenant)(money)}</div>
      <ul className="record-list">
        {rows.map((r) => (
          <li key={`${r.kind}${r.no}`}>
            <div className="record-head">
              <strong>{r.title}{r.detail && r.kind === 'payment' ? ` · ${r.detail}` : ''}</strong>
              <span className="amount"
                    style={r.direction === 'in' ? { color: 'var(--ok)' } : undefined}>
                {r.direction === 'in' ? '+' : r.direction === 'out' ? '−' : ''}{r.amount} 元
              </span>
            </div>
            <div className="record-body">
              {r.at} · {r.house_no} · <span className="house-no">{r.no}</span>
              {r.kind === 'refund' && r.detail && ` · ${r.detail}`}
            </div>
          </li>
        ))}
      </ul>
      {money.list.length > rows.length && (
        <p className="desc">只显示最近 {rows.length} 笔，共 {money.list.length} 笔。</p>
      )}
    </>
  )
}
