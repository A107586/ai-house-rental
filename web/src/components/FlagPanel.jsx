import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * M-08 内容审核队列。
 *
 * 这里只出现「机器拿不准」的内容——确定违规的在提交时就被拦下了，
 * 根本没进库。所以这个列表里的每一条都需要人真的看一眼再决定，
 * 界面上因此把**原文摘要**放在最显眼的位置，而不是只列命中的词。
 */
const STATUS_TABS = [
  { key: 'pending', label: '待复核' },
  { key: 'approved', label: '已通过' },
  { key: 'removed', label: '已下架' },
]

export default function FlagPanel() {
  const [status, setStatus] = useState('pending')
  const [rows, setRows] = useState([])
  const [pending, setPending] = useState(0)
  const [busy, setBusy] = useState(0)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load(next = status) {
    setError('')
    try {
      const data = await api.adminFlags(next)
      setRows(data.list || [])
      setPending(data.pending || 0)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load(status)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  async function handle(id, action) {
    setBusy(id)
    setError('')
    setOk('')
    try {
      await api.handleFlag(id, action)
      setOk(action === 'approve' ? '已保留该内容' : '已下架该内容')
      await load(status)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(0)
    }
  }

  return (
    <div className="flags">
      <div className="tabs sub">
        {STATUS_TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${status === t.key ? 'active' : ''}`}
            onClick={() => { setStatus(t.key); setOk(''); setError('') }}
          >
            {t.label}
            {t.key === 'pending' && pending > 0 && (
              <span className="tab-n">{pending}</span>
            )}
          </button>
        ))}
      </div>

      <p className="rd-note">
        这里是需要人工判断的内容。明显违规的内容在提交时已被拦下。
      </p>

      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {rows.length === 0 ? (
        <div className="empty">
          {status === 'pending' ? '没有待复核的内容' : '暂无记录'}
        </div>
      ) : (
        rows.map((row) => (
          <article className="flag-card" key={row.id}>
            <div className="flag-head">
              <span className="flag-scene">{row.scene_label}</span>
              <span className="mono">{row.target_key || `#${row.target_id}`}</span>
              <span className="flag-author">{row.author_name}</span>
              <span className="flag-time mono">{row.created_at}</span>
            </div>

            {/* 原文放最显眼的位置：需要人判断的东西，光看命中词判断不了 */}
            <div className="flag-excerpt">{row.excerpt}</div>

            <div className="chips">
              {row.categories.map((c) => (
                <span className="chip feature" key={c}>{c}</span>
              ))}
            </div>

            {row.reasons.length > 0 && (
              <ul className="flag-reasons">
                {row.reasons.map((r) => <li key={r}>{r}</li>)}
              </ul>
            )}

            {row.status === 'pending' ? (
              <div className="flag-acts">
                <button className="btn ghost small" disabled={busy === row.id}
                        onClick={() => handle(row.id, 'approve')}>
                  内容没问题，保留
                </button>
                <button className="btn small danger" disabled={busy === row.id}
                        onClick={() => handle(row.id, 'remove')}>
                  下架
                </button>
              </div>
            ) : (
              <div className="flag-done">
                {row.status === 'approved' ? '已保留' : '已下架'}
                {row.handled_by && ` · ${row.handled_by}`}
                {row.handled_at && ` · ${row.handled_at}`}
              </div>
            )}
          </article>
        ))
      )}
    </div>
  )
}
