import { useState } from 'react'
import { api } from '../api'

/**
 * 工单详情底下那一排：看房源、联系相关的人（2026-10-22）。
 *
 * 原先工单只能看：租客想催房东得自己去找那套房点「咨询」，
 * 房东要问报修人现场情况、管理员要催房东，都没有入口。
 * 三端各自能联系谁：
 *
 *   租客   → 房东（房源会话）、平台（转人工，带上工单号）
 *   房东   → 报修人（房源会话）、平台
 *   管理员 → 房东、报修人（站内消息；对方回话走「联系平台」）
 *
 * 发给平台和管理员发出的都是一段文字，用同一个就地展开的输入框。
 */
export default function RepairContact({ repair, viewer, me, onOpenHouse, onTalk }) {
  const [compose, setCompose] = useState('')   // '' | 'platform' | 'landlord' | 'tenant'
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  const no = `GD${String(repair.id).padStart(6, '0')}`
  // 房东自己报的修（can_report_repair 放行管理这套房的人），没有「另一个报修人」可联系
  const reporter = repair.tenant_username && repair.tenant_id !== me?.id

  function open(kind) {
    setCompose(compose === kind ? '' : kind)
    setText('')
    setNote('')
    setError('')
  }

  async function act(fn, done) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await fn()
      setCompose('')
      setText('')
      setNote(done)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function talkToLandlord() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const d = await api.startConversation(repair.house_no)
      onTalk(d.conversation.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function send() {
    const body = text.trim()
    if (!body) { setError('先写点内容'); return }
    if (compose === 'platform') {
      act(() => api.supportHandoff(
        `【工单 ${no}】${body}`, `工单 ${no}`,
        `房源 ${repair.house_no}${repair.house_title ? ` · ${repair.house_title}` : ''}；`
        + `${repair.category || '报修'}：${repair.content}`),
      '已交给平台，回复会发到你的站内消息。')
    } else {
      act(() => api.adminRepairMessage(repair.id, compose, body),
        `已发给${compose === 'landlord' ? '房东' : '报修人'}，对方会收到站内消息。`)
    }
  }

  const placeholder = compose === 'platform'
    ? '想让平台帮什么忙？比如：房东三天没回应、维修没修好……'
    : `给${compose === 'landlord' ? '房东' : '报修人'}的话，比如：这张工单已超时，请尽快安排`

  return (
    <div className="repair-contact">
      <div className="record-actions">
        {onOpenHouse && (
          <button className="btn ghost small" onClick={() => onOpenHouse(repair.house_no)}>
            查看房源
          </button>
        )}
        {viewer === 'tenant' && onTalk && (
          <button className="btn ghost small" disabled={busy} onClick={talkToLandlord}>
            联系房东
          </button>
        )}
        {viewer === 'landlord' && reporter && onTalk && (
          <button className="btn ghost small"
                  onClick={() => onTalk(repair.house_no, repair.tenant_username)}>
            联系报修人
          </button>
        )}
        {viewer === 'admin' && (
          <>
            <button className="btn ghost small" onClick={() => open('landlord')}>联系房东</button>
            {repair.tenant_id && (
              <button className="btn ghost small" onClick={() => open('tenant')}>联系报修人</button>
            )}
          </>
        )}
        {viewer !== 'admin' && (
          <button className="btn ghost small" onClick={() => open('platform')}>联系平台</button>
        )}
      </div>

      {compose && (
        <div className="repair-compose">
          <textarea rows={3} maxLength={480} value={text} autoFocus
                    placeholder={placeholder}
                    onChange={(e) => setText(e.target.value)} />
          <div className="record-actions">
            <button className="btn small" disabled={busy} onClick={send}>
              {busy ? '发送中…' : '发送'}
            </button>
            <button className="btn ghost small" onClick={() => open(compose)}>取消</button>
            <span className="own">会带上工单号 {no}</span>
          </div>
        </div>
      )}
      {note && <div className="record-body done-note">{note}</div>}
      {error && <div className="reply failed">{error}</div>}
    </div>
  )
}
