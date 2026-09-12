import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import ChatAvatar from './ChatAvatar'

/**
 * 租客与房东的会话面板（待完善清单 C-1）。
 *
 * 租客侧与房东侧共用这一个组件——两边要做的事完全一样（挑一个会话、
 * 看消息、回一句），只是列表里显示的对方角色不同。做成两套会立刻开始
 * 各自长歪。
 *
 * **用轮询而不是 WebSocket。** WebSocket 要改部署形态（单 worker 撑不住
 * 长连接）、要处理断线重连、要做在线状态，而租房不是即时通讯——对方隔
 * 十分钟回复是常态。面板打开时每十秒拉一次增量（after_id），关掉就停。
 */
const POLL_MS = 10000

export default function ChatWithLandlord({ me, onClose, onOpenHouse, initialId }) {
  const [list, setList] = useState([])
  const [activeId, setActiveId] = useState(initialId || 0)
  const [lines, setLines] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const bottomRef = useRef(null)
  // 最后一条消息的 id，轮询时只取比它新的
  const lastIdRef = useRef(0)

  async function loadList() {
    try {
      const data = await api.conversations()
      setList(data.list || [])
      if (!activeId && data.list?.length) setActiveId(data.list[0].id)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    loadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 切换会话：整段重拉，并把未读清零
  useEffect(() => {
    if (!activeId) return
    let alive = true
    lastIdRef.current = 0
    api
      .conversationLines(activeId)
      .then((data) => {
        if (!alive) return
        setLines(data.list || [])
        if (data.list?.length) lastIdRef.current = data.list[data.list.length - 1].id
        return api.readConversation(activeId)
      })
      .then(() => alive && loadList())
      .catch((err) => alive && setError(err.message))
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  // 轮询增量。面板关掉时 effect 清理会把定时器停掉
  useEffect(() => {
    if (!activeId) return undefined
    const timer = setInterval(async () => {
      try {
        const data = await api.conversationLines(activeId, lastIdRef.current)
        if (!data.list?.length) return
        setLines((prev) => [...prev, ...data.list])
        lastIdRef.current = data.list[data.list.length - 1].id
        api.readConversation(activeId).then(loadList).catch(() => {})
      } catch {
        // 轮询失败不打扰用户——下一轮自然会重试
      }
    }, POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [lines])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function send(e) {
    e.preventDefault()
    const content = text.trim()
    if (!content || busy || !activeId) return
    setBusy(true)
    setError('')
    try {
      const data = await api.sendLine(activeId, content)
      setLines((prev) => [...prev, data.line])
      lastIdRef.current = data.line.id
      setText('')
      loadList()
    } catch (err) {
      // 被内容审核拦下时，**保留输入框里的原文**——让用户自己改，
      // 而不是清空了事再让他重打一遍
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const active = list.find((c) => c.id === activeId)

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer wide chat-drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        <h3>咨询</h3>
        <div className="loc">和房东就这套房沟通，消息双方都能看到</div>

        {error && <div className="reply failed">{error}</div>}

        {list.length === 0 ? (
          <div className="empty">
            还没有咨询记录。在房源详情页点「咨询房东」就能开始。
          </div>
        ) : (
          <div className="cw-body">
            {/* ---------- 会话列表 ---------- */}
            <ul className="cw-list">
              {list.map((item) => (
                <li key={item.id}>
                  <button
                    className={`cw-item ${item.id === activeId ? 'on' : ''}`}
                    onClick={() => setActiveId(item.id)}
                  >
                    <span className="cw-house">{item.house_no}</span>
                    <span className="cw-title">{item.house_title}</span>
                    <span className="cw-last">{item.last_message || '还没有消息。预约结果、账单和合同变动都会通知到这里。'}</span>
                    {item.unread > 0 && (
                      <span className="cw-badge">{item.unread}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>

            {/* ---------- 消息流 ---------- */}
            <div className="cw-thread">
              {active && (
                <div className="cw-head">
                  <button
                    className="link-title"
                    onClick={() => onOpenHouse && onOpenHouse(active.house_no)}
                  >
                    {active.house_title}
                  </button>
                  <span className="muted-note">
                    {active.peer_role} {active.peer_name} ·
                    {Math.round(active.rent_price)} 元/月
                  </span>
                </div>
              )}

              <div className="cw-lines">
                {lines.length === 0 ? (
                  <div className="empty">
                    还没有消息。可以问问能不能养宠物、几月能入住、押付方式能不能商量。
                  </div>
                ) : (
                  lines.map((line) => (line.kind === 'summary' ? (
                    /* 需求摘要（J-09-a）渲染成一张卡片而不是聊天气泡。
                       它不是谁「说」的一句话，是随咨询附上的一份材料；
                       做成气泡会让人以为租客真的打了这么一段字过来 */
                    <div className="demand-card" key={line.id}>
                      <pre className="demand-body">{line.content}</pre>
                      <div className="cw-at">{line.created_at}</div>
                    </div>
                  ) : (
                    <div className={`bubble ${line.mine ? 'user' : 'assistant'}`}
                         key={line.id}>
                      {/* E1：头像取发言人姓氏首字，底色由名字哈希定死。
                          委托期间房东一侧坐着两个人（房东本人和受托中介），
                          名字那行滚过去容易略过，**颜色是能一眼跟住的**——
                          分不清刚才那句承诺是谁做的，是这里最要紧的事 */}
                      /* A2：对方的头像用**这个会话里对方的名字**，不是一个占位词。

   `sender_label` 只在「同侧但不是我」时才有值（那是委托期间中介
   代答的情形），普通两人会话里它永远是空串。于是这里原来回退到
   「对方」「租客」这种字面量——头像取首字，结果每一个会话的对方
   都长着同一个「对」或「租」。**换个人说话，画面上没有任何变化**，
   而头像的全部作用就是让人一眼认出说话的是谁。 */
                      <ChatAvatar
                        kind={line.mine ? 'me' : 'other'}
                        name={line.mine
                          ? (me?.real_name || me?.username || '我')
                          : (line.sender_label || active?.peer_name || '对方')}
                      />
                      {/* 同一侧里不是自己发的才标名字。 */}
                      {line.sender_label && (
                        <div className="cw-who">{line.sender_label}</div>
                      )}
                      <div className="bubble-text">{line.content}</div>
                      <div className="cw-at">{line.created_at}</div>
                    </div>
                  )))
                )}
                <div ref={bottomRef} />
              </div>

              <form className="chat-input" onSubmit={send}>
                <input
                  placeholder="说点什么…"
                  value={text}
                  maxLength={500}
                  onChange={(e) => setText(e.target.value)}
                />
                <button className="btn" type="submit" disabled={busy || !text.trim()}>
                  发送
                </button>
              </form>
              <div className="muted-note">
                请在平台内沟通。交换微信、电话等联系方式会被拦截——
                平台无法为私下成交提供任何保障。
              </div>
            </div>
          </div>
        )}
      </aside>
    </>
  )
}
