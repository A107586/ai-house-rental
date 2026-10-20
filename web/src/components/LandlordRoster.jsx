import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * F-02 户主档案。
 *
 * 与「用户管理」看的是同一批人，但回答的问题不同：那边关心账号状态
 * （谁注册了、是不是被禁用），这里关心经营状况（有多少房、租出去几套、
 * 这个月收了多少、有没有积压的工单）。
 *
 * 空置率与积压工单是这张表真正的用处——房源多但一套没租出去、
 * 或者工单堆了七八条不处理，都是需要平台介入的信号，
 * 光看账号列表看不出来。
 */
export default function LandlordRoster({ onOpenUser }) {
  const [rows, setRows] = useState([])
  const [keyword, setKeyword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function load(kw = keyword) {
    setBusy(true)
    setError('')
    try {
      const data = await api.adminLandlords({ keyword: kw })
      setRows(data.list || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    load('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const totals = rows.reduce((acc, r) => ({
    houses: acc.houses + r.house_total,
    rented: acc.rented + r.house_rented,
    income: acc.income + r.month_income,
    repairs: acc.repairs + r.repair_open,
  }), { houses: 0, rented: 0, income: 0, repairs: 0 })

  return (
    <div className="roster">
      <form
        className="log-filters"
        onSubmit={(e) => { e.preventDefault(); load() }}
      >
        <input
          placeholder="搜房东用户名或姓名"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <button className="btn small" type="submit" disabled={busy}>
          {busy ? '查询中…' : '查询'}
        </button>
      </form>

      {error && <div className="reply failed">{error}</div>}

      <div className="dash-grid">
        <div className="dcell">
          <div className="dk">房东</div>
          <div className="dv">{rows.length}</div>
          <div className="dd">位</div>
        </div>
        <div className="dcell">
          <div className="dk">名下房源</div>
          <div className="dv">{totals.houses}</div>
          <div className="dd">在租 {totals.rented}</div>
        </div>
        <div className="dcell money">
          <div className="dk">本月收租</div>
          <div className="dv">{Math.round(totals.income)}</div>
          <div className="dd">元</div>
        </div>
        <div className={`dcell ${totals.repairs ? 'warn' : ''}`}>
          <div className="dk">积压工单</div>
          <div className="dv">{totals.repairs}</div>
          <div className="dd">条</div>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="empty">没有找到合适的房源。上面「已生效条件」里删掉几条，再试试。</div>
      ) : (
        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr>
                <th>房东</th>
                <th>联系电话</th>
                <th>名下房源</th>
                <th>生效合同</th>
                <th>本月收租</th>
                <th>待收</th>
                <th>积压工单</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.repair_open > 2 ? 'bad' : ''}>
                  <td>
                    <button className="link-title" onClick={() => onOpenUser?.(row.id)}>
                      {row.real_name || row.username}
                    </button>
                    <div className="log-path mono">{row.username}</div>
                  </td>
                  <td className="mono">{row.phone || '-'}</td>
                  <td>
                    {row.house_total} 套
                    <div className="log-path">
                      在租 {row.house_rented}
                      {row.house_pending > 0 && ` · 待审 ${row.house_pending}`}
                    </div>
                  </td>
                  <td>{row.contract_active}</td>
                  <td className="accent">{Math.round(row.month_income)}</td>
                  <td>{Math.round(row.unpaid)}</td>
                  <td>{row.repair_open || '—'}</td>
                  <td>{row.status === 'active' ? '正常' : '已禁用'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="rd-note">
        电话只显示部分号码，联系房东请用站内消息。未处理工单超过 2 条的房东标红。
      </p>
    </div>
  )
}
