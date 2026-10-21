import { useEffect, useState } from 'react'
import { api } from '../api'

const ROLE_LABEL = { tenant: '租客', landlord: '房东', agent: '中介', admin: '管理员' }
const CONTRACT_STATUS = { pending: '待确认', active: '生效中', ended: '已结束', cancelled: '已取消' }
const HOUSE_STATUS = {
  available: '在架', rented: '已出租', offline: '已下架', pending: '待审核', rejected: '未通过',
}
const LICENSE = { approved: '已通过', pending: '审核中', rejected: '已驳回', unsubmitted: '未提交' }

/**
 * 管理员看一个人的全貌：作为租客、房东、中介各做过什么，最近的操作。
 *
 * 用户管理、户主档案、中介档案三处原先都只有一行摘要，点不开。
 * 一个账号可以同时是租客和房东，所以三块都可能出现，有数据才显示。
 */
export default function UserOverview({ userId, onClose, onOpenContract }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api.adminUserOverview(userId)
      .then((d) => { if (alive) setData(d) })
      .catch((err) => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [userId])

  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const u = data?.user
  const tenant = data?.as_tenant
  const landlord = data?.as_landlord
  const agent = data?.as_agent

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer user-overview" role="dialog" aria-modal="true">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        {!data && !error && <div className="empty">加载中…</div>}
        {error && <div className="reply failed">{error}</div>}

        {data && (
          <>
            <h3>{u.real_name || u.username}</h3>
            <div className="loc">
              {u.username} · {ROLE_LABEL[u.role] || u.role}
              {u.status !== 'active' && ' · 已禁用'}
            </div>

            <dl className="uo-facts">
              <div><dt>手机</dt><dd>{u.phone || '—'}</dd></div>
              <div><dt>邮箱</dt><dd>{u.email || '—'}</dd></div>
              <div><dt>身份证</dt><dd>{u.id_card || '未实名'}</dd></div>
              <div><dt>注册时间</dt><dd>{u.created_at || '—'}</dd></div>
            </dl>

            {tenant && (
              <>
                <div className="section-title">作为租客</div>
                <div className="td-stats">
                  <div><b>{tenant.active}</b><span>在租合同</span></div>
                  <div className={tenant.overdue_count ? 'bad' : ''}>
                    <b>{tenant.overdue_count}</b><span>逾期账单（{tenant.overdue_total} 元）</span>
                  </div>
                  <div><b>{tenant.repairs}</b><span>报修次数</span></div>
                </div>
                <ContractList rows={tenant.contracts} onOpenContract={onOpenContract} />
              </>
            )}

            {landlord && (
              <>
                <div className="section-title">作为房东</div>
                <div className="td-stats">
                  <div><b>{landlord.house_count}</b><span>名下房源</span></div>
                  <div><b>{landlord.active_contracts}</b><span>在租合同</span></div>
                  <div className={landlord.open_repairs > 2 ? 'bad' : ''}>
                    <b>{landlord.open_repairs}</b><span>未完成工单</span>
                  </div>
                </div>
                <ul className="record-list compact">
                  {landlord.houses.map((h) => (
                    <li key={h.house_no}>
                      <div className="record-head">
                        <strong>{h.title}</strong>
                        <span className="pill">{HOUSE_STATUS[h.status] || h.status}</span>
                      </div>
                      <div className="record-body">{h.house_no} · {h.rent_price} 元/月</div>
                    </li>
                  ))}
                </ul>
                {landlord.house_count > landlord.houses.length && (
                  <p className="desc">只列了前 {landlord.houses.length} 套</p>
                )}
              </>
            )}

            {agent && (
              <>
                <div className="section-title">作为中介</div>
                <dl className="uo-facts">
                  <div><dt>资质</dt><dd>{LICENSE[agent.license_status] || agent.license_status}</dd></div>
                  <div><dt>证书编号</dt><dd>{agent.license_no || '—'}</dd></div>
                  <div><dt>主营区域</dt><dd>{agent.service_districts || '不限'}</dd></div>
                  <div><dt>接受新委托</dt><dd>{agent.accepting ? '是' : '否'}</dd></div>
                </dl>
                <div className="td-stats">
                  <div><b>{agent.delegations}</b><span>在托房源</span></div>
                  <div><b>{agent.viewings}</b><span>带看次数</span></div>
                  <div><b>{agent.deals}</b><span>成交（佣金 {agent.commission_total} 元）</span></div>
                </div>
              </>
            )}

            <div className="section-title">最近操作</div>
            {data.recent_ops.length === 0 ? (
              <p className="desc">暂无操作记录</p>
            ) : (
              <table className="log-table td-bills">
                <tbody>
                  {data.recent_ops.map((op, i) => (
                    <tr key={i} className={op.status_code >= 400 ? 'bad' : ''}>
                      <td className="mono">{op.created_at}</td>
                      <td>{op.action}</td>
                      <td>{op.status_code >= 400 ? '失败' : '成功'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </>
  )
}

function ContractList({ rows, onOpenContract }) {
  if (!rows.length) return null
  return (
    <ul className="record-list compact">
      {rows.map((c) => (
        <li key={c.contract_no}>
          <div className="record-head">
            {onOpenContract ? (
              <button className="link-title" onClick={() => onOpenContract(c.contract_no)}>
                {c.house_title || c.house_no}
              </button>
            ) : <strong>{c.house_title || c.house_no}</strong>}
            <span className="pill">{CONTRACT_STATUS[c.status] || c.status}</span>
          </div>
          <div className="record-body">
            {c.contract_no} · {c.start_date} 至 {c.end_date} · {c.rent} 元/月
          </div>
        </li>
      ))}
    </ul>
  )
}
