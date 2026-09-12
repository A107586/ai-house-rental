import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 我的搜索（K-09）。
 *
 * 对标贝壳个人中心的第三栏。**它和收藏解决的不是同一个问题**：
 * 收藏是「我看中了这套」，保存搜索是「我还没看中，但符合我条件的
 * 一出现就告诉我」。租房场景里后者才是常态——合适的房子随时上架、
 * 也随时被租走，用户不可能靠反复刷列表守到。
 *
 * 界面上三件事：
 *
 * - **一键恢复**。点一下就把这组条件填回筛选栏并重新检索，
 *   这是保存搜索的主要用途，所以整行可点，不是角落里一个小链接。
 * - **提醒可关不可丢**。用户可能暂时不想被打扰，但条件还想留着。
 *   只给「删除」等于逼他把条件也一起丢掉。
 * - **显示上次提醒时间**。没有它，用户没法判断「这条搜索是真的
 *   没有新房，还是提醒坏了」。
 */
export default function SavedSearchPanel({ onApply }) {
  const [rows, setRows] = useState([])
  const [busy, setBusy] = useState(0)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(0)
  const [draft, setDraft] = useState('')

  async function load() {
    setError('')
    try {
      const data = await api.savedSearches()
      setRows(data.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => { load() }, [])

  async function act(id, fn) {
    setBusy(id)
    setError('')
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(0)
    }
  }

  if (rows.length === 0) {
    return (
      <div className="empty">
        还没有保存的搜索。在首页设好筛选条件后点「保存这组条件」，以后有符合条件的新房源上架，会通过这里通知你。
      </div>
    )
  }

  return (
    <div className="saved-searches">
      {error && <div className="reply failed">{error}</div>}

      <ul className="record-list">
        {rows.map((row) => (
          <li key={row.id}>
            <div className="record-head">
              {editing === row.id ? (
                <input
                  className="rename-input"
                  value={draft}
                  maxLength={50}
                  autoFocus
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      act(row.id, () =>
                        api.updateSavedSearch(row.id, { name: draft }))
                      setEditing(0)
                    }
                    if (e.key === 'Escape') setEditing(0)
                  }}
                />
              ) : (
                <button
                  className="link-title"
                  onClick={() => onApply(row.conditions)}
                  title="用这组条件重新检索"
                >
                  {row.name}
                </button>
              )}
              {!row.notify_on && <span className="pill">已关提醒</span>}
            </div>

            <div className="record-body muted-note">
              保存于 {row.created_at}
              {row.last_notified_at
                ? ` · 最近提醒 ${row.last_notified_at}`
                : ' · 还没有匹配到新房源'}
            </div>

            <div className="record-actions">
              <button className="btn small" onClick={() => onApply(row.conditions)}>
                用这组条件找房
              </button>

              <button
                className="btn ghost small"
                disabled={busy === row.id}
                onClick={() => act(row.id, () =>
                  api.updateSavedSearch(row.id, { notify_on: !row.notify_on }))}
              >
                {row.notify_on ? '关闭上新提醒' : '开启上新提醒'}
              </button>

              <button
                className="btn ghost small"
                onClick={() => { setEditing(row.id); setDraft(row.name) }}
              >
                重命名
              </button>

              <button
                className="link danger"
                disabled={busy === row.id}
                onClick={() => act(row.id, () => api.deleteSavedSearch(row.id))}
              >
                删除
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
