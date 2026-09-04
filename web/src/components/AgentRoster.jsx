import { useEffect, useState } from 'react'
import { api } from '../api'
import RatioCell from './RatioCell'

/**
 * 中介档案 + 平台中介口径（J-11）。
 *
 * 这一页要回答的是「这个中介靠不靠谱」，所以**被举报记录必须在这里**，
 * 而不是只躺在举报页签里——管理员不该为了看一个人的底细去翻两个页面。
 *
 * 表里的统计走后端的 `agent_stats()`，和中介自己工作台上看到的是
 * 同一份计算。各算各的话，答辩时被问「工作台说 12 次、后台说 9 次，
 * 哪个对」就没法回答。
 *
 * 顶部的「委托 vs 自管成交」是这一页最该看的一个数：它是唯一能回答
 * 「中介功能到底有没有用」的口径。如果委托的反而更低，就该如实显示——
 * 看板不是用来证明功能有价值的，是用来发现它没价值的。
 */

const LICENSE = {
  unsubmitted: { label: '未提交', cls: '' },
  pending: { label: '审核中', cls: 'status-pending' },
  approved: { label: '已通过', cls: 'status-confirmed' },
  rejected: { label: '未通过', cls: 'status-rejected' },
}

export default function AgentRoster() {
  const [rows, setRows] = useState([])
  const [platform, setPlatform] = useState(null)
  const [status, setStatus] = useState('')
  const [keyword, setKeyword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function load(params = { status, keyword }) {
    setBusy(true)
    setError('')
    try {
      const [list, stats] = await Promise.all([
        api.adminAgents(params),
        api.adminAgentStats(),
      ])
      setRows(list.list || [])
      setPlatform(stats)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    load({ status: '', keyword: '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 委托房源成交占比。分母是全部合同，0 单时不显示百分比而显示「暂无成交」，
  // 否则新系统上线第一天就会看到一个刺眼的「0%」
  const dealTotal = platform
    ? platform.deals_with_agent + platform.deals_self_managed : 0

  return (
    <div className="roster">
      <form
        className="log-filters"
        onSubmit={(e) => { e.preventDefault(); load({ status, keyword }) }}
      >
        <input
          placeholder="搜中介用户名或姓名"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部资质状态</option>
          <option value="approved">已通过</option>
          <option value="pending">审核中</option>
          <option value="rejected">未通过</option>
          <option value="unsubmitted">未提交</option>
        </select>
        <button className="btn small" type="submit" disabled={busy}>
          {busy ? '查询中…' : '查询'}
        </button>
      </form>

      {error && <div className="reply failed">{error}</div>}

      {platform && (
        <>
          <div className="dash-grid">
            <div className="dcell">
              <div className="dk">中介</div>
              <div className="dv">{platform.agent_total}</div>
              <div className="dd">已过审 {platform.agent_approved}</div>
            </div>
            <div className={`dcell ${platform.license_pending ? 'warn' : ''}`}>
              <div className="dk">待审资质</div>
              <div className="dv">{platform.license_pending}</div>
              <div className="dd">份</div>
            </div>
            <div className="dcell">
              <div className="dk">在托房源</div>
              <div className="dv">{platform.delegated_houses}</div>
              <div className="dd">
                占可租 <RatioCell value={platform.delegated_share}
                  label="有委托的可租房源 / 全部可租房源" />
              </div>
            </div>
            <div className="dcell">
              <div className="dk">近 {platform.recent_days} 天带看</div>
              <div className="dv">{platform.recent_viewings}</div>
              <div className="dd">场</div>
            </div>
          </div>

          <div className="section-title">中介到底有没有用</div>
          {dealTotal === 0 ? (
            <p className="desc">还没有成交合同，这个对比暂时无从谈起。</p>
          ) : (
            <div className="table-wrap">
              <table className="log-table">
                <tbody>
                  <tr>
                    <td>中介促成的合同</td>
                    <td className="accent">{platform.deals_with_agent}</td>
                  </tr>
                  <tr>
                    <td>房东自管签出的合同</td>
                    <td className="accent">{platform.deals_self_managed}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
          <p className="rd-note">
            这两个数是唯一能回答「中介功能有没有价值」的口径。做了这个功能
            就该敢于把它摆出来——如果委托的反而更低，说明功能没起作用，
            应当如实显示，而不是把这一行藏起来。
          </p>
        </>
      )}

      <div className="section-title">中介档案</div>
      {rows.length === 0 ? (
        <div className="empty">没有符合条件的中介。</div>
      ) : (
        <div className="table-wrap">
          <table className="log-table wide">
            <thead>
              <tr>
                {/* 只留管理员真正要据以做决定的列。十列塞进抽屉，
                    每一列都窄到标签自己折行，等于一列都读不了；
                    累计成交折进「在托」那格，待结佣金是中介自己的事，
                    管理员这里既不能结也不需要看 */}
                <th>中介</th>
                <th>资质</th>
                <th>在托</th>
                <th>近期带看</th>
                <th>成交率</th>
                <th>放鸽子率</th>
                <th>好评率</th>
                <th>被举报</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const st = LICENSE[row.license_status]
                  || { label: row.license_status, cls: '' }
                return (
                  <tr key={row.user_id}
                    className={row.reported.accepted > 0 ? 'bad' : ''}>
                    <td>
                      {row.name || row.username}
                      <div className="log-path mono">
                        {row.username}
                        {row.districts.length > 0
                          && ` · ${row.districts.join('、')}`}
                      </div>
                    </td>
                    <td>
                      <span className={`pill ${st.cls}`}>{st.label}</span>
                      {row.license_status === 'rejected' && row.reject_reason && (
                        <div className="log-path">{row.reject_reason}</div>
                      )}
                    </td>
                    <td>
                      {row.stats.active_delegations}
                      <div className="log-path">
                        累计成交 {row.stats.deal_total}
                      </div>
                    </td>
                    <td>{row.stats.recent_viewings}</td>
                    <td><RatioCell value={row.stats.conversion} /></td>
                    <td><RatioCell value={row.stats.no_show} warnAbove={30} /></td>
                    <td><RatioCell value={row.stats.praise} /></td>
                    <td>
                      {row.reported.total === 0 ? '—' : (
                        <>
                          {row.reported.total}
                          <div className="log-path">
                            成立 {row.reported.accepted}
                          </div>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="rd-note">
        被举报按「举报发生时这套房是不是他在托」归集，而不是按当前受托人——
        否则委托一解除，历史举报就全都不算在他头上了。有成立举报的行会标红。
        所有比率都带分母：样本不足 5 时显示原始数字而不是百分比，
        「成交率 25%」在只带看过 4 次时是一句没有信息量的话。
      </p>
    </div>
  )
}
