import { useEffect, useRef, useState } from 'react'
import { api, getToken } from '../api'

/**
 * AI-09 多轮对话面板。
 *
 * 会话编号存在 localStorage 里，刷新页面能接着上一次聊。
 * 登录用户的对话存在后端，换设备也能续上；未登录用户由前端把自己
 * 说过的话带给智能体——换设备记不住，但当前这轮对话照样是多轮的。
 */
const SESSION_KEY = 'rental_chat_session'

function loadSessionId() {
  try {
    let id = localStorage.getItem(SESSION_KEY)
    if (!id) {
      id = 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
      localStorage.setItem(SESSION_KEY, id)
    }
    return id
  } catch {
    // 隐私模式下 localStorage 会直接抛异常，退回一次性会话
    return 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
  }
}

const OPENING = {
  role: 'assistant',
  content: '你好，我是租房助手。找房可以一句一句地补充条件，我都记得住；'
    + '平台规则也可以直接问我，比如「押金怎么退」——'
    + '我只按知识库里写的回答，查不到就直说，不会瞎编。',
}

const QUICK = ['中山区两室一厅', '三千以内的', '押金怎么退', '水电费怎么算']

export default function ChatPanel({ user, onOpenHouse, onClose }) {
  const [sessionId] = useState(loadSessionId)
  const [messages, setMessages] = useState([OPENING])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [context, setContext] = useState('')
  const [error, setError] = useState('')
  const bottomRef = useRef(null)

  // 登录用户把之前存在后端的对话拉回来
  useEffect(() => {
    if (!getToken()) return
    let alive = true
    api
      .chatHistory(sessionId)
      .then((data) => {
        if (!alive || !data.list?.length) return
        setMessages([OPENING, ...data.list.map((m) => ({
          role: m.role, content: m.content,
        }))])
      })
      .catch(() => {})
    return () => { alive = false }
  }, [sessionId, user])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, busy])

  // 未登录时的上下文兜底：把用户自己说过的话再带回去
  function localHistory() {
    return messages.filter((m) => m.role === 'user').map((m) => m.content)
  }

  // 上一轮结果的房源编号，用于消解「第二套」「那套」
  function lastHouseNos() {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].items?.length) {
        return messages[i].items.map((h) => h.house_no)
      }
    }
    return []
  }

  async function send(e, override) {
    if (e) e.preventDefault()
    const content = (override ?? text).trim()
    if (!content || busy) return

    const history = localHistory()
    const houseNos = lastHouseNos()
    setMessages((prev) => [...prev, { role: 'user', content }])
    setText('')
    setBusy(true)
    setError('')

    try {
      const res = await api.agentChat(content, sessionId, history, houseNos)
      setContext(res.context_text || '')
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: res.message || '（没有返回内容）',
        items: res.items || [],
        notes: res.notes || [],
        intent: res.intent,
        ok: res.ok,
        // AI-03 客服：引用来源、猜你想问、转人工线索、日志编号
        citations: res.citations || [],
        alternatives: res.alternatives || [],
        handoff: res.handoff || null,
        logId: res.log_id || null,
        status: res.status || '',
        feedback: 0,
      }])
    } catch (err) {
      setError(err.message)
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: '智能体暂时联系不上，稍后再试。',
        ok: false,
      }])
    } finally {
      setBusy(false)
    }
  }

  /** 一条回答有用 / 没用。「没用」会进管理端的补语料清单。 */
  async function rate(index, value) {
    const message = messages[index]
    if (!message?.logId || message.feedback) return
    setMessages((prev) => prev.map((m, i) => (
      i === index ? { ...m, feedback: value } : m)))
    try {
      await api.supportFeedback(message.logId, value)
    } catch {
      // 评价发不出去不该打断对话，界面上已经给了反馈
    }
  }

  /** 转人工：把这个问题留给管理员，回复走站内消息。 */
  async function handoff(index) {
    const message = messages[index]
    if (!message?.handoff) return
    if (!getToken()) {
      setError('转人工需要先登录，管理员回复时要知道回给谁。')
      return
    }
    try {
      const res = await api.supportHandoff(
        message.handoff.question, message.handoff.reason,
        localHistory().slice(-5).join('\n'))
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: `已转给管理员，工单号 ${res.handoff.handoff_no}。`
          + '回复会通过站内消息通知你。',
        ok: true,
      }])
    } catch (err) {
      setError(err.message)
    }
  }

  async function restart() {
    setMessages([OPENING])
    setContext('')
    setError('')
    if (getToken()) {
      try {
        await api.clearChat(sessionId)
      } catch {
        // 清不掉服务端记录也不拦着用户重新开始，
        // 下一轮的上下文以界面上看到的为准
      }
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer chat">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <h3>租房助手</h3>
        <div className="loc">
          多轮对话 · 会记住你已经说过的条件
          {user ? '' : ' · 登录后换设备也能续上'}
        </div>

        {context && (
          <div className="chat-context">
            <span className="chat-context-k">当前记住</span>
            {context}
            <button className="btn ghost small" onClick={restart}>
              重新开始
            </button>
          </div>
        )}

        <div className="chat-log">
          {messages.map((message, index) => (
            <div className={`bubble ${message.role}`} key={index}>
              <div className="bubble-text">{message.content}</div>

              {message.notes?.length > 0 && (
                <div className="bubble-notes">
                  {message.notes.map((note) => (
                    <span className="note" key={note}>{note}</span>
                  ))}
                </div>
              )}

              {message.citations?.length > 0 && (
                <div className="bubble-cites">
                  <div className="cite-title">依据</div>
                  {message.citations.map((cite) => (
                    <div className="cite" key={cite.id}>
                      <span className={`cite-tag ${cite.source}`}>
                        {cite.source === 'notice' ? '公告' : '常见问题'}
                      </span>
                      <span className="cite-q">{cite.title}</span>
                      <span className="cite-snippet">{cite.snippet}</span>
                    </div>
                  ))}
                </div>
              )}

              {message.alternatives?.length > 0 && (
                <div className="bubble-alts">
                  <span className="alt-label">你可能想问</span>
                  {message.alternatives.map((alt) => (
                    <button
                      className="chip-btn"
                      key={alt}
                      disabled={busy}
                      onClick={() => send(null, alt)}
                    >
                      {alt}
                    </button>
                  ))}
                </div>
              )}

              {message.handoff && (
                <div className="bubble-handoff">
                  <button className="btn ghost small" onClick={() => handoff(index)}>
                    转人工
                  </button>
                  <span className="muted small">
                    我答不上来的问题，可以交给管理员
                  </span>
                </div>
              )}

              {message.logId && message.role === 'assistant' && (
                <div className="bubble-rate">
                  {message.feedback === 0 ? (
                    <>
                      <button className="rate" onClick={() => rate(index, 1)}>有用</button>
                      <button className="rate" onClick={() => rate(index, -1)}>没用</button>
                    </>
                  ) : (
                    <span className="muted small">
                      {message.feedback > 0 ? '谢谢反馈' : '已记下，我们会补充这块内容'}
                    </span>
                  )}
                </div>
              )}

              {message.items?.length > 0 && (
                <div className="bubble-items">
                  {message.items.slice(0, 5).map((house, i) => (
                    <button
                      className="mini-house"
                      key={house.house_no}
                      onClick={() => onOpenHouse(house)}
                    >
                      <span className="mini-no">{i + 1}</span>
                      <span className="mini-title">{house.title}</span>
                      <span className="mini-price">
                        {Math.round(house.rent_price)} 元
                      </span>
                    </button>
                  ))}
                  {message.items.length > 5 && (
                    <div className="mini-more">
                      还有 {message.items.length - 5} 套，说「再多看几套」
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}

          {busy && <div className="bubble assistant"><div className="bubble-text">思考中…</div></div>}
          <div ref={bottomRef} />
        </div>

        {error && <div className="reply failed">{error}</div>}

        <div className="chat-quick">
          {QUICK.map((sample) => (
            <button
              className="chip-btn"
              key={sample}
              disabled={busy}
              onClick={() => send(null, sample)}
            >
              {sample}
            </button>
          ))}
        </div>

        <form className="chat-input" onSubmit={send}>
          <input
            placeholder="说点什么，比如「换成沙河口」"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <button className="btn" type="submit" disabled={busy || !text.trim()}>
            发送
          </button>
        </form>
      </aside>
    </>
  )
}
