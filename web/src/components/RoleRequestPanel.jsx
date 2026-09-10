import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 角色升级申请审核（D1）。
 *
 * 「注册成房东要管理员审核」这条规则，后端在 D1 那次就落好了：申请进
 * `role_requests`，人先按租客用着。但**没有一个地方能批**——队列在库里
 * 攒着，申请人等着，管理员看不到。规则做完一半比不做更糟：用户被拦住了，
 * 却没有任何人有能力放行。
 *
 * 两个刻意的处理：
 *
 * 1. **默认只看待审。** 打开这一页是来清队列的，历史记录是偶尔才查的东西，
 *    放在切换里。
 * 2. **驳回强制填理由。** 后端已经拦了（422），前端也拦一次——
 *    等提交被拒再回来重填，理由已经打了一半的字也没了。
 */

const TABS = [
  { key: 'pending', label: '待审' },
  { key: 'approved', label: '已通过' },
  { key: 'rejected', label: '已驳回' },
]

export default function RoleRequestPanel({ onReviewed }) {
  const [status, setStatus] = useState('pending')
  const [list, setList] = useState([])
  const [notes, setNotes] = useState({})
  const [busy, setBusy] = useState(0)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load(next = status) {
    setError('')
    try {
      const d = await api.adminRoleRequests(next)
      setList(d.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load(status)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  async function review(req, action) {
    if (busy) return
    const note = (notes[req.id] || '').trim()
    if (action === 'reject' && !note) {
      setError('驳回要写清原因，申请人才知道该补什么。')
      return
    }
    setBusy(req.id)
    setError('')
    setOk('')
    try {
      await api.reviewRoleRequest(req.id, action, note)
      setOk(action === 'approve'
        ? `已通过 ${req.real_name || req.username} 的${req.target_role_label}申请。`
        : `已驳回并通知 ${req.real_name || req.username}。`)
      setNotes((p) => ({ ...p, [req.id]: '' }))
      await load()
      onReviewed?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(0)
    }
  }

  return (
    <div className="role-req">
      <div className="seg">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`seg-btn ${status === t.key ? 'on' : ''}`}
            onClick={() => setStatus(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {list.length === 0 ? (
        <p className="desc">
          {status === 'pending'
            ? '没有待审的申请。'
            : '这一档下还没有记录。'}
        </p>
      ) : (
        <ul className="record-list">
          {list.map((req) => (
            <li key={req.id}>
              <div className="record-head">
                <strong>{req.real_name || req.username}</strong>
                <span className="pill">申请成为{req.target_role_label}</span>
                <span className={`pill status-${req.status}`}>
                  {{ pending: '待审', approved: '已通过', rejected: '已驳回' }[req.status]}
                </span>
              </div>

              <div className="record-body">
                账号 {req.username} · 手机 {req.phone} · 提交于 {req.created_at}
              </div>

              {req.reason && (
                <div className="record-body">申请说明：{req.reason}</div>
              )}

              {/* 已处理的把当时的结论摆出来。**审核记录要能回看**——
                  用户来问「为什么没过」时，得答得出是谁、什么时候、
                  以什么理由驳的 */}
              {req.status !== 'pending' && (
                <div className="record-body muted-note">
                  {req.reviewed_at} 处理
                  {req.review_note && ` · ${req.review_note}`}
                </div>
              )}

              {req.status === 'pending' && (
                <>
                  <label className="fld" style={{ marginTop: 8 }}>
                    <span>审核意见（驳回必填，会原样发给申请人）</span>
                    <input
                      value={notes[req.id] || ''}
                      maxLength={200}
                      placeholder="例：请补充房产证或委托证明"
                      onChange={(e) =>
                        setNotes((p) => ({ ...p, [req.id]: e.target.value }))}
                    />
                  </label>
                  <div className="record-actions">
                    <button className="btn small" disabled={busy === req.id}
                            onClick={() => review(req, 'approve')}>
                      {busy === req.id ? '处理中…' : '通过'}
                    </button>
                    <button className="btn ghost small" disabled={busy === req.id}
                            onClick={() => review(req, 'reject')}>
                      驳回
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
