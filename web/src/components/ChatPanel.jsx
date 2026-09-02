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
// 整段对话与未完成的代办也要存下来。
//
// 原先只存了会话编号，消息本身放在组件 state 里——**抽屉一关组件就卸载，
// 聊到一半的内容、正在等确认的卡片、追问到一半的槽位全没了**，
// 回来只剩一句开场白。用户反馈「不要退出后消失的 bug」说的就是这个。
//
// 登录用户的对话后端也存了一份，但那份只有文字，没有卡片与待办状态，
// 而且未登录用户根本没有。所以这里另存一份完整的。
const DRAFT_KEY = 'rental_chat_draft'
// 只留最近这些轮。存太多既没意义，也可能撑爆 localStorage 的配额
const DRAFT_LIMIT = 40

function loadDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    if (!raw) return null
    const draft = JSON.parse(raw)
    if (!Array.isArray(draft.messages) || !draft.messages.length) return null
    return draft
  } catch {
    // 存的内容坏了就当没有，重新开始，别让一段坏 JSON 把面板打不开
    return null
  }
}

function saveDraft(messages, pending) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      messages: messages.slice(-DRAFT_LIMIT), pending,
    }))
  } catch {
    // 隐私模式或配额满了都会抛。存不下不影响这一轮对话，忽略即可
  }
}

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
    + '我只按知识库里写的回答，查不到就直说，不会瞎编。'
    + '也可以让我帮你做事——交房租、约看房、报修都行，'
    + '我会先把要做的事列给你看，你点确认我才动手。',
}

const QUICK = ['中山区两室一厅', '三千以内的', '押金怎么退', '水电费怎么算']

// 状态码 -> 中文。卡片里直接显示 available / pending 等同于没显示。
const STATUS_TEXT = {
  available: '在租中', rented: '已出租', offline: '已下架',
  pending: '待处理', processing: '处理中', done: '已完成',
  active: '生效中', ended: '已结束', terminated: '已终止',
  confirmed: '已确认', cancelled: '已取消', rejected: '已拒绝',
  paid: '已缴清', unpaid: '待缴', overdue: '已逾期',
  replied: '已回复', open: '待处理',
}

const statusText = (value) => STATUS_TEXT[value] || value || ''

const money = (value) => `${Math.round(Number(value) || 0)}`

/** 房源类的卡片行都长一样：标题在上，编号与租金在下。 */
const houseRow = (item) => ({
  key: item.house_no,
  main: item.title || item.community || item.house_no,
  sub: `${item.house_no || ''}${item.rent_price ? ` · ${money(item.rent_price)} 元/月` : ''}`,
  house: item,
})

// 每一类查询结果怎么摆成几行。**这张表和智能体那边的工具清单是一一对应的**——
// 后端 `tools/account.py` 里 HANDLERS 有几项，这里就该有几项，
// 少一项就意味着那个工具查到了数据却只回一句话、什么都看不见。
const ACCOUNT_ROWS = {
  bill: (data) => (data.unpaid || []).map((bill) => ({
    key: bill.bill_no,
    main: `${bill.bill_type || '租金'} ${money(bill.amount)} 元`,
    sub: bill.due_date ? `${bill.due_date} 到期` : '',
  })),
  contract: (data) => (data.list || []).map((item) => ({
    key: item.contract_no,
    main: `${item.house_no || ''} ${item.contract_no}`,
    sub: `${item.start_date} 至 ${item.end_date} · ${statusText(item.status)}`,
  })),
  booking: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: item.house_title || item.house_no,
    sub: `${(item.viewing_time || '').slice(0, 16)} · ${statusText(item.status)}`,
  })),
  repair: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: (item.content || '').slice(0, 24),
    sub: `${item.house_no || ''} · ${statusText(item.status)}`,
  })),
  favorite: (data) => (data.list || []).map(houseRow),
  history: (data) => (data.list || []).map(houseRow),
  house: (data) => (data.list || []).map((item) => ({
    ...houseRow(item),
    sub: `${item.house_no || ''} · ${statusText(item.status)}`,
  })),
  message: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: item.title,
    sub: item.created_at,
  })),
  review: (data) => (data.list || []).map((item) => ({
    key: item.contract_no,
    main: `${item.house_no || ''} ${item.contract_no}`,
    sub: `${item.start_date} 至 ${item.end_date} · 待评价`,
  })),
  handoff: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: (item.question || '').slice(0, 24),
    sub: `${item.created_at || ''} · ${statusText(item.status)}`,
  })),
  tenant: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: item.username || item.name || '',
    sub: item.overdue_count
      ? `逾期 ${item.overdue_count} 笔 · 欠 ${money(item.unpaid_total)} 元`
      : `在租 ${item.active_count || 0} 套`,
  })),
  profile: (data) => {
    const me = data.profile || {}
    if (!me.username) return []
    const role = { tenant: '租客', landlord: '房东', admin: '管理员' }
    return [
      { key: 'username', main: me.username, sub: role[me.role] || me.role },
      me.phone ? { key: 'phone', main: '手机号', sub: me.phone } : null,
    ].filter(Boolean)
  },
}

/** 经营数据是四个数字，摆成行不如摆成格子。 */
function IncomeCard({ data }) {
  const cells = [
    { label: '本月已收', value: `${money(data.month_income)} 元` },
    { label: '待收', value: `${money(data.income_pending)} 元` },
    { label: '出租率', value: `${data.occupancy_rate || 0}%` },
    { label: '已出租', value: `${data.house_rented || 0}/${data.house_total || 0} 套` },
  ]
  return (
    <div className="account-card">
      <div className="account-grid">
        {cells.map((cell) => (
          <div className="account-cell" key={cell.label}>
            <span className="account-cell-v">{cell.value}</span>
            <span className="account-cell-k">{cell.label}</span>
          </div>
        ))}
      </div>
      {data.overdue_count > 0 && (
        <div className="account-row alert">
          <span className="account-main">{data.overdue_count} 笔账单已逾期</span>
        </div>
      )}
    </div>
  )
}

/** 查库结果的小卡片。
 *
 * 「你还有 35 笔待缴账单」看得见，但看不见是哪几笔——助手查到的东西
 * 应当**摆在对话里**，而不是只留一句话让用户自己再去页面上找一遍。
 * 房源类的行可以点开，点了就跳到那套房，省掉「回列表里翻」这一步。
 */
function AccountCard({ topic, data, onOpenHouse }) {
  if (!topic || !data) return null
  if (topic === 'income') return <IncomeCard data={data} />

  const build = ACCOUNT_ROWS[topic]
  if (!build) return null
  const rows = build(data).slice(0, 4)
  if (!rows.length) return null

  return (
    <div className="account-card">
      {rows.map((row) => (
        row.house && onOpenHouse ? (
          <button
            className="account-row clickable"
            key={row.key}
            onClick={() => onOpenHouse(row.house)}
          >
            <span className="account-main">{row.main}</span>
            {row.sub && <span className="account-sub">{row.sub}</span>}
          </button>
        ) : (
          <div className="account-row" key={row.key}>
            <span className="account-main">{row.main}</span>
            {row.sub && <span className="account-sub">{row.sub}</span>}
          </div>
        )
      ))}
    </div>
  )
}

export default function ChatPanel({ user, onOpenHouse, onClose }) {
  const [sessionId] = useState(loadSessionId)
  // 优先恢复上次没聊完的，没有才从开场白开始
  const [messages, setMessages] = useState(() => loadDraft()?.messages || [OPENING])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [context, setContext] = useState('')
  // 未完成的代办：助手问了哪个槽、已经填了什么、或者已经摆出的确认卡片。
  // 服务端不存会话态，这份状态由前端带着走。
  const [pending, setPending] = useState(() => loadDraft()?.pending || null)
  const [error, setError] = useState('')
  const bottomRef = useRef(null)

  // 登录用户把之前存在后端的对话拉回来
  useEffect(() => {
    if (!getToken()) return
    let alive = true
    // 本地草稿比后端历史更完整——后端只存文字，没有确认卡片与待办状态。
    // 已经从草稿恢复过就别再覆盖，否则聊到一半的卡片会被纯文本冲掉。
    if (loadDraft()) return undefined
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

  // 消息或待办一变就落盘。抽屉关掉、页面刷新、甚至换个标签页回来，
  // 都还在上次那个位置。
  useEffect(() => {
    saveDraft(messages, pending)
  }, [messages, pending])

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
      const res = await api.agentChat(content, sessionId, history, houseNos, pending)
      setContext(res.context_text || '')
      // 助手还在等一个答案就留着，否则清掉——不清会让下一句无关的话
      // 被硬塞进上一轮的槽位里
      setPending(res.pending || null)
      // 用户口头说了「好的」，且这一步不是高风险——直接替他执行，
      // 不必再让他去点一次按钮。高风险的仍然要求点，见 agent 里的说明。
      if (res.auto_confirm && res.action) {
        setMessages((prev) => [...prev, {
          role: 'assistant', content: res.message, ok: true,
        }])
        try {
          const done = await api.supportConfirm(res.action.name, res.action.params)
          setMessages((prev) => [...prev, {
            role: 'assistant', content: done.message, ok: done.ok,
          }])
        } catch (err) {
          setError(err.message)
        }
        setPending(null)
        setBusy(false)
        return
      }

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
        // 代办确认卡片。有它就说明这一轮是「要我帮你做吗」，
        // 在用户点确认之前，后端什么都还没做。
        action: res.action || null,
        acted: '',
        // 可点选项。让用户点，而不是猜该怎么打字
        options: res.options || [],
        // 查库结果。有它就渲染成一张小卡片，而不是只留一句话——
        // 「你还有 35 笔待缴账单」看得见，但看不见是哪几笔。
        account: res.account || null,
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

  /**
   * 用户点了确认，才真正执行这个操作。
   *
   * 对话接口 `/agent/support` 永远只返回卡片，执行走的是另一个接口。
   * 这个函数是两者之间唯一的桥，**只能由按钮点击触发**——
   * 不要为了「更顺畅」在收到卡片时自动调用它。
   */
  async function runAction(index) {
    const message = messages[index]
    if (!message?.action || message.acted || busy) return

    setBusy(true)
    setError('')
    try {
      const res = await api.supportConfirm(message.action.name, message.action.params)
      setMessages((prev) => prev.map((m, i) => (
        i === index ? { ...m, acted: res.ok ? 'done' : 'failed' } : m)))
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: res.message,
        ok: res.ok,
      }])
      setPending(null)
    } catch (err) {
      setError(err.message)
      setMessages((prev) => prev.map((m, i) => (
        i === index ? { ...m, acted: 'failed' } : m)))
    } finally {
      setBusy(false)
    }
  }

  function cancelAction(index) {
    setMessages((prev) => prev.map((m, i) => (
      i === index ? { ...m, acted: 'cancelled' } : m)))
    setPending(null)
  }

  async function restart() {
    setMessages([OPENING])
    setContext('')
    setError('')
    setPending(null)
    try {
      localStorage.removeItem(DRAFT_KEY)
    } catch {
      // 清不掉也不拦着用户重新开始
    }
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

              {message.account?.data && (
                <AccountCard
                  topic={message.account.tool || message.account.topic}
                  data={message.account.data}
                  onOpenHouse={onOpenHouse}
                />
              )}

              {message.options?.length > 0 && (
                <div className="bubble-options">
                  {message.options.map((option) => (
                    <button
                      className="chip-btn"
                      key={option.value}
                      disabled={busy}
                      onClick={() => send(null, option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              )}

              {message.action && (
                <div className={`action-card risk-${message.action.risk}`}>
                  <div className="action-head">
                    <span className="action-label">{message.action.label}</span>
                    {message.action.risk === 'high' && (
                      <span className="action-risk">需谨慎</span>
                    )}
                  </div>
                  <div className="action-summary">{message.action.summary}</div>
                  {message.action.warning && (
                    <div className="action-warning">{message.action.warning}</div>
                  )}
                  {!message.acted ? (
                    <div className="action-buttons">
                      <button
                        className="btn"
                        disabled={busy}
                        onClick={() => runAction(index)}
                      >
                        确认执行
                      </button>
                      <button
                        className="btn ghost"
                        disabled={busy}
                        onClick={() => cancelAction(index)}
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <div className="action-done">
                      {message.acted === 'done' && '已执行'}
                      {message.acted === 'failed' && '未能执行'}
                      {message.acted === 'cancelled' && '已取消，什么都没做'}
                    </div>
                  )}
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
