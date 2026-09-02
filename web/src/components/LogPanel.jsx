import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * M-07 操作日志。
 *
 * 日志翻起来最常见的两个诉求是「谁动了这条数据」和「最近有没有人在撞权限」，
 * 所以关键词与「只看失败」这两个筛选放在最显眼的位置。
 */
const ROLE_LABEL = { tenant: '租客', landlord: '房东', admin: '管理员', '': '匿名' }
const PAGE_SIZE = 30

export default function LogPanel() {
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [actions, setActions] = useState([])
  const [keyword, setKeyword] = useState('')
  const [action, setAction] = useState('')
  const [role, setRole] = useState('')
  const [failedOnly, setFailedOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function load(nextPage = 1, override = {}) {
    setBusy(true)
    setError('')
    const params = {
      keyword, action, role, failed_only: failedOnly,
      ...override, page: nextPage, limit: PAGE_SIZE,
    }
    try {
      const data = await api.adminLogs(params)
      setRows(data.list || [])
      setTotal(data.total || 0)
      setPage(nextPage)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    load(1)
    api.adminLogActions()
      .then((d) => setActions(d.list || []))
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function applyFilter(patch) {
    if ('action' in patch) setAction(patch.action)
    if ('role' in patch) setRole(patch.role)
    if ('failed_only' in patch) setFailedOnly(patch.failed_only)
    load(1, patch)
  }

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="oplog">
      <form
        className="log-filters"
        onSubmit={(e) => { e.preventDefault(); load(1) }}
      >
        <input
          placeholder="搜用户名、动作或路径"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <select value={action} onChange={(e) => applyFilter({ action: e.target.value })}>
          <option value="">全部动作</option>
          {actions.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
        <select value={role} onChange={(e) => applyFilter({ role: e.target.value })}>
          <option value="">全部角色</option>
          <option value="tenant">租客</option>
          <option value="landlord">房东</option>
          <option value="admin">管理员</option>
        </select>
        <label className="log-check">
          <input
            type="checkbox"
            checked={failedOnly}
            onChange={(e) => applyFilter({ failed_only: e.target.checked })}
          />
          只看失败
        </label>
        <button className="btn small" type="submit" disabled={busy}>
          {busy ? '查询中…' : '查询'}
        </button>
      </form>

      {error && <div className="reply failed">{error}</div>}

      <div className="log-count">
        共 {total} 条
        <span className="log-note">
          只记录改变了状态的操作，浏览类请求不入库；请求体一律不记，
          改密码这类接口的正文里有明文密码。
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="empty">没有符合条件的日志。</div>
      ) : (
        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>结果</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.status_code >= 400 ? 'bad' : ''}>
                  <td className="mono">{row.created_at}</td>
                  <td>
                    {row.username}
                    <span className="log-role">{ROLE_LABEL[row.role] ?? row.role}</span>
                  </td>
                  <td>
                    {row.action}
                    <div className="log-path mono">{row.method} {row.path}</div>
                  </td>
                  <td>
                    <span className={`log-code ${row.status_code >= 400 ? 'bad' : 'ok'}`}>
                      {row.status_code}
                    </span>
                  </td>
                  <td className="mono">{row.ip || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="log-pager">
          <button className="btn ghost small" disabled={page <= 1 || busy}
                  onClick={() => load(page - 1)}>
            上一页
          </button>
          <span>{page} / {pages}</span>
          <button className="btn ghost small" disabled={page >= pages || busy}
                  onClick={() => load(page + 1)}>
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
