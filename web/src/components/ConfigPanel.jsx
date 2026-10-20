import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * M-09 系统配置。
 *
 * 界面上每一项都写清楚「改了会怎样」。配置页最容易变成一堆没人敢动的
 * 开关——不是因为复杂，是因为看不出影响范围。说明文字比输入框更重要。
 *
 * 提交是整批的：后端要么全部写入要么全部拒绝，界面也就不做逐项保存，
 * 免得让人以为改了一项就已经生效了。
 */
export default function ConfigPanel() {
  const [rows, setRows] = useState([])
  const [draft, setDraft] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load() {
    setError('')
    try {
      const data = await api.adminConfig()
      setRows(data.list || [])
      setDraft(Object.fromEntries((data.list || []).map((r) => [r.key, r.value])))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const dirty = rows.filter((r) => draft[r.key] !== r.value)

  async function save() {
    if (!dirty.length) return
    setBusy(true)
    setError('')
    setOk('')
    try {
      const data = await api.updateConfig(
        Object.fromEntries(dirty.map((r) => [r.key, draft[r.key]])))
      setRows(data.list || [])
      setDraft(Object.fromEntries((data.list || []).map((r) => [r.key, r.value])))
      setOk(`已保存 ${data.updated} 项，立即生效。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setDraft(Object.fromEntries(rows.map((r) => [r.key, r.value])))
    setOk('')
    setError('')
  }

  // 按分组渲染，顺序沿用后端给的顺序
  const groups = []
  rows.forEach((row) => {
    const found = groups.find((g) => g.name === row.group)
    if (found) found.items.push(row)
    else groups.push({ name: row.group, items: [row] })
  })

  return (
    <div className="sysconfig">
      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {groups.map((group) => (
        <section className="cfg-group" key={group.name}>
          <h5>{group.name}</h5>
          {group.items.map((row) => (
            <div className="cfg-row" key={row.key}>
              <div className="cfg-meta">
                <div className="cfg-label">
                  {row.label}
                  {row.is_public && <span className="cfg-public">公开</span>}
                  {draft[row.key] !== row.value && (
                    <span className="cfg-dirty">未保存</span>
                  )}
                </div>
                <div className="cfg-desc">{row.description}</div>
              </div>

              <div className="cfg-input">
                {row.value_type === 'bool' ? (
                  <label className="cfg-switch">
                    <input
                      type="checkbox"
                      checked={draft[row.key] === 'true'}
                      onChange={(e) => setDraft({
                        ...draft,
                        [row.key]: e.target.checked ? 'true' : 'false',
                      })}
                    />
                    {draft[row.key] === 'true' ? '已开启' : '已关闭'}
                  </label>
                ) : (
                  <input
                    type={row.value_type === 'int' ? 'number' : 'text'}
                    min={row.value_type === 'int' ? 1 : undefined}
                    value={draft[row.key] ?? ''}
                    onChange={(e) => setDraft({ ...draft, [row.key]: e.target.value })}
                  />
                )}
              </div>
            </div>
          ))}
        </section>
      ))}

      <div className="cfg-actions">
        <button className="btn" onClick={save} disabled={busy || !dirty.length}>
          {busy ? '保存中…' : dirty.length ? `保存 ${dirty.length} 项修改` : '没有修改'}
        </button>
        {dirty.length > 0 && (
          <button className="btn ghost" onClick={reset} disabled={busy}>
            撤销
          </button>
        )}
        <span className="cfg-note">
          有一项填错时，本次修改都不会保存。
        </span>
      </div>
    </div>
  )
}
