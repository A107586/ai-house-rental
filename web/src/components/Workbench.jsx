import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import ChatAvatar from './ChatAvatar'

const MapFinder = lazy(() => import('./MapFinder'))

/**
 * 房东 / 中介工作台（F1）。
 *
 * ## 为什么另写一个页面，而不是改原来那两个
 *
 * 原来的 `LandlordPanel` 和 `AgentPanel` 是**抽屉 + 一排横向页签**。
 * 抽屉是给「看一眼就关」的东西用的——个人中心、房源详情都合适。
 * 可工作台是一个人**一天要待在里面几个小时**的地方：他在这里回消息、
 * 确认预约、安排带看。给这种页面套一个盖在首页上的浮层，
 * 意味着背景里永远压着一屏他不看的房源，而且横向页签一多就要横滚，
 * 「我现在在哪一栏」永远看不全。
 *
 * 左栏竖排能一屏列完全部功能，当前位置一直可见——这是所有后台
 * 都长成这样的原因，不是审美偏好。
 *
 * ## 「只有沟通管理」这条边界怎么落
 *
 * 验收意见是「中介和房东应只有沟通管理功能」。按字面把发布房源、
 * 合同、工单、佣金全删掉，房东就没法上架房子了——那不是收窄，是拆功能。
 *
 * 所以这里的做法是：**新工作台只装「与人打交道」的四件事**——
 * 会话、预约、带看、客户；其余的事务性功能仍留在原来的面板里，
 * 从最下面那个入口进。工作台答「谁在等我回话」，
 * 事务面板答「我的资产和账目怎么样」，两件事分开。
 */

const APPT_STATUS = {
  pending: { label: '待确认', cls: 'status-pending' },
  confirmed: { label: '已确认', cls: 'status-confirmed' },
  rejected: { label: '已拒绝', cls: 'status-rejected' },
  cancelled: { label: '已取消', cls: 'status-cancelled' },
}

/** 今天零点。判断「今天要去的带看」用它，比拿字符串前十位比更稳。 */
function startOfToday() {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

function isToday(text) {
  if (!text) return false
  const t = new Date(String(text).replace(' ', 'T'))
  if (Number.isNaN(t.getTime())) return false
  const start = startOfToday()
  return t >= start && t < new Date(start.getTime() + 86400000)
}

export default function Workbench({ user, onClose, onOpenHouse, onOpenLegacy }) {
  const [nav, setNav] = useState('chat')
  const [convs, setConvs] = useState([])
  const [activeId, setActiveId] = useState(0)
  const [lines, setLines] = useState([])
  const [draft, setDraft] = useState('')
  const [appts, setAppts] = useState([])
  const [houses, setHouses] = useState([])
  const [tenants, setTenants] = useState([])
  const [district, setDistrict] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const bottomRef = useRef(null)
  const lastIdRef = useRef(0)

  const isAgent = user.role === 'agent'
  const title = isAgent ? '中介工作台' : '房东工作台'

  const loadConvs = useCallback(async () => {
    try {
      const d = await api.conversations()
      setConvs(d.list || [])
      return d.list || []
    } catch (err) {
      setError(err.message)
      return []
    }
  }, [])

  async function loadRest() {
    // **四个请求各自 catch。** 并进一个 Promise.all 的话，
    // 中介账号没有名下房源（landlordHouses 返回 403）就会把会话也一起
    // 拖垮——而会话恰恰是这个页面最重要的一栏
    api.myAppointments('landlord')
      .then((d) => setAppts(d.list || [])).catch(() => setAppts([]))
    api.landlordHouses()
      .then((d) => setHouses(d.list || [])).catch(() => setHouses([]))
    api.landlordTenants()
      .then((d) => setTenants(d.list || [])).catch(() => setTenants([]))
  }

  useEffect(() => {
    loadConvs().then((list) => {
      if (list.length && !activeId) setActiveId(list[0].id)
    })
    loadRest()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 打开某个会话：先整段取回，之后只增量拉新消息。
  // 每次都整段取的话，对方一直在打字这边就一直重渲染整个列表，
  // 滚动位置也保不住。
  useEffect(() => {
    if (!activeId) { setLines([]); return undefined }
    let alive = true
    lastIdRef.current = 0
    api.conversationLines(activeId, 0).then((d) => {
      if (!alive) return
      const rows = d.list || []
      setLines(rows)
      lastIdRef.current = rows.length ? rows[rows.length - 1].id : 0
      api.readConversation(activeId).then(loadConvs).catch(() => {})
    }).catch((err) => alive && setError(err.message))

    const timer = setInterval(async () => {
      try {
        const d = await api.conversationLines(activeId, lastIdRef.current)
        const rows = d.list || []
        if (!alive || !rows.length) return
        setLines((prev) => [...prev, ...rows])
        lastIdRef.current = rows[rows.length - 1].id
        api.readConversation(activeId).then(loadConvs).catch(() => {})
      } catch {
        // 轮询失败不弹错。**网络抖一下就在屏幕上留一条红字**，
        // 而下一次轮询多半就好了——真正断了的时候发送会报错
      }
    }, 5000)

    return () => { alive = false; clearInterval(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [lines.length])

  async function send(e) {
    e.preventDefault()
    const content = draft.trim()
    if (!content || !activeId || busy) return
    setBusy('send')
    setError('')
    try {
      const d = await api.sendLine(activeId, content)
      setDraft('')
      if (d?.line) {
        setLines((prev) => [...prev, d.line])
        lastIdRef.current = d.line.id
      }
      loadConvs()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  async function reply(appt, status) {
    if (busy) return
    setBusy(`a${appt.id}`)
    setError('')
    setOk('')
    try {
      await api.updateAppointment(appt.id, status)
      setOk(status === 'confirmed'
        ? `已确认 ${appt.house_no} 的看房，租客会收到通知。`
        : `已拒绝 ${appt.house_no} 的看房申请。`)
      const d = await api.myAppointments('landlord')
      setAppts(d.list || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  /** 从某条预约直接开一个会话。**这是这一页存在的理由**——
   *  看到「有人约了周六」之后，下一步就是跟他说句话。 */
  async function talkTo(appt) {
    if (!appt.tenant_username || busy) return
    setBusy(`t${appt.id}`)
    setError('')
    try {
      const d = await api.contactTenant(appt.house_no, appt.tenant_username)
      const list = await loadConvs()
      const id = d?.conversation?.id
        || list.find((c) => c.house_no === appt.house_no)?.id
      if (id) { setActiveId(id); setNav('chat') }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  const unread = convs.reduce((n, c) => n + (c.unread || 0), 0)
  const pending = appts.filter((a) => a.status === 'pending')
  const today = appts.filter(
    (a) => a.status === 'confirmed' && isToday(a.viewing_time))

  const NAV = [
    { key: 'chat', label: '会话', n: unread, hint: '与租客直接沟通' },
    { key: 'appt', label: '看房预约', n: pending.length, hint: '待我确认的申请' },
    { key: 'today', label: '今日带看', n: today.length, hint: '今天要去的场次' },
    { key: 'houses', label: '房源分布', hint: '名下房源与区域' },
    { key: 'tenants', label: '客户', n: tenants.length, hint: '在租与历史租客' },
  ]

  const active = convs.find((c) => c.id === activeId)

  return (
    <div className="wb">
      <header className="wb-top">
        <button className="back-round" onClick={onClose}
                aria-label="返回首页" title="返回首页">‹</button>
        <h1>{title}</h1>
        <span className="wb-who">{user.real_name || user.username}</span>
      </header>

      <div className="wb-body">
        {/* 左栏。竖排、带计数、当前项高亮——**一屏能列完，
            所以任何时候都看得见「我在哪、还有几件事等着」** */}
        <nav className="wb-rail" aria-label="工作台功能">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`wb-nav${nav === item.key ? ' on' : ''}`}
              onClick={() => { setNav(item.key); setOk('') }}
            >
              <span className="wb-nav-main">
                {item.label}
                {item.n > 0 && <span className="wb-nav-n">{item.n}</span>}
              </span>
              <span className="wb-nav-hint">{item.hint}</span>
            </button>
          ))}

          {/* 事务性功能仍在原面板里。**入口摆出来，不藏**——
              房东总要发布房源、看合同、处理工单，
              把它们从工作台移走不等于让他找不到 */}
          <button className="wb-nav legacy" onClick={onOpenLegacy}>
            <span className="wb-nav-main">房源与账目</span>
            <span className="wb-nav-hint">发布、合同、工单、佣金</span>
          </button>
        </nav>

        <main className="wb-main">
          {error && <div className="reply failed">{error}</div>}
          {ok && <div className="reply created">{ok}</div>}

          {nav === 'chat' && (
            <div className="wb-chat">
              <ul className="wb-conv">
                {convs.length === 0 && (
                  <li className="wb-empty">
                    还没有会话。租客在房源页点「咨询房东」就会出现在这里，
                    你也可以从「看房预约」里直接找他。
                  </li>
                )}
                {convs.map((c) => (
                  <li key={c.id}>
                    <button
                      className={`wb-conv-item${c.id === activeId ? ' on' : ''}`}
                      onClick={() => setActiveId(c.id)}
                    >
                      <ChatAvatar kind="other" name={c.peer_name || c.house_no} />
                      <span className="wb-conv-text">
                        <span className="wb-conv-top">
                          <b>{c.peer_name || '租客'}</b>
                          <em>{c.house_no}</em>
                        </span>
                        <span className="wb-conv-last">
                          {c.last_message || '还没有消息'}
                        </span>
                      </span>
                      {c.unread > 0 && <span className="wb-badge">{c.unread}</span>}
                    </button>
                  </li>
                ))}
              </ul>

              <section className="wb-thread">
                {!active ? (
                  <div className="wb-empty">选一个会话开始。</div>
                ) : (
                  <>
                    <div className="wb-thread-head">
                      <button className="link-title"
                              onClick={() => onOpenHouse?.(active.house_no)}>
                        {active.house_title || active.house_no}
                      </button>
                      <span className="muted-note">{active.house_no}</span>
                    </div>

                    <div className="wb-lines">
                      {lines.length === 0 && (
                        <div className="wb-empty">
                          还没有消息。先问一句「方便什么时候看房」，
                          比等着对方开口有用。
                        </div>
                      )}
                      {lines.map((line) => (
                        <div className={`bubble ${line.mine ? 'user' : 'assistant'}`}
                             key={line.id}>
                          <ChatAvatar
                            kind={line.mine ? 'me' : 'other'}
                            name={line.mine
                              ? (user.real_name || user.username)
                              : (line.sender_label || '租客')}
                          />
                          {line.sender_label && !line.mine && (
                            <div className="cw-who">{line.sender_label}</div>
                          )}
                          <div className="bubble-text">{line.content}</div>
                          <div className="cw-at">{line.created_at}</div>
                        </div>
                      ))}
                      <div ref={bottomRef} />
                    </div>

                    <form className="chat-input" onSubmit={send}>
                      <input
                        placeholder="回一句…"
                        value={draft}
                        maxLength={500}
                        onChange={(e) => setDraft(e.target.value)}
                      />
                      <button className="btn" type="submit"
                              disabled={busy === 'send' || !draft.trim()}>
                        发送
                      </button>
                    </form>
                    <p className="muted-note">
                      请在平台内沟通。交换微信、电话会被拦截——
                      平台无法为私下成交提供任何保障。
                    </p>
                  </>
                )}
              </section>
            </div>
          )}

          {(nav === 'appt' || nav === 'today') && (
            <ApptList
              rows={nav === 'appt' ? pending : today}
              emptyText={nav === 'appt'
                ? '没有待确认的看房申请。'
                : '今天没有已确认的带看。'}
              showActions={nav === 'appt'}
              busy={busy}
              onReply={reply}
              onTalk={talkTo}
              onOpenHouse={onOpenHouse}
            />
          )}

          {nav === 'houses' && (
            <div className="wb-houses">
              {/* 地图按验收意见保留。它在这里的用处和列表页不同：
                  房东看的是**自己的房子铺在哪几个区**，
                  哪个区空置多就该往哪个区使劲 */}
              <Suspense fallback={<div className="map-loading">地图加载中…</div>}>
                <MapFinder selected={district} onPick={setDistrict} />
              </Suspense>

              <ul className="record-list">
                {houses.length === 0 && (
                  <li className="wb-empty">名下还没有房源。</li>
                )}
                {houses
                  .filter((h) => !district || h.district === district)
                  .map((h) => (
                    <li key={h.house_no}>
                      <div className="record-head">
                        <button className="link-title"
                                onClick={() => onOpenHouse?.(h.house_no)}>
                          {h.title}
                        </button>
                        <span className="pill">{h.status === 'available' ? '在架'
                          : h.status === 'rented' ? '已出租' : '已下架'}</span>
                      </div>
                      <div className="record-body">
                        {h.house_no} · {h.district} · {h.layout} ·
                        {' '}{Math.round(h.rent_price)} 元/月
                      </div>
                    </li>
                  ))}
              </ul>
            </div>
          )}

          {nav === 'tenants' && (
            <ul className="record-list">
              {tenants.length === 0 && (
                <li className="wb-empty">还没有租客。签约之后会出现在这里。</li>
              )}
              {tenants.map((t) => (
                <li key={`${t.contract_no || t.username}`}>
                  <div className="record-head">
                    <ChatAvatar kind="other" name={t.real_name || t.username} />
                    <strong>{t.real_name || t.username}</strong>
                    {t.status && <span className="pill">{t.status}</span>}
                  </div>
                  <div className="record-body">
                    {t.house_no}
                    {t.phone && ` · ${t.phone}`}
                    {t.end_date && ` · 租至 ${t.end_date}`}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </main>
      </div>
    </div>
  )
}

function ApptList({ rows, emptyText, showActions, busy, onReply, onTalk,
                   onOpenHouse }) {
  if (rows.length === 0) return <div className="wb-empty">{emptyText}</div>
  return (
    <ul className="record-list">
      {rows.map((a) => {
        const st = APPT_STATUS[a.status] || { label: a.status, cls: '' }
        return (
          <li key={a.id}>
            <div className="record-head">
              <button className="link-title"
                      onClick={() => onOpenHouse?.(a.house_no)}>
                {a.house_title || a.house_no}
              </button>
              <span className={`pill ${st.cls}`}>{st.label}</span>
            </div>
            <div className="record-body">
              <span className="house-no">{a.house_no}</span>
              看房时间 {a.viewing_time.slice(0, 16)}
            </div>
            <div className="record-body">
              {a.contact_name || '租客'}
              {/* 号码只在看房窗口内给（J-07）。窗口外说清楚**什么时候能看到**，
                  比空着一栏强——否则中介会以为是系统出了问题 */}
              {a.contact_visible ? ` · ${a.contact_phone}` : ' · 号码看房当天可见'}
            </div>
            {a.remark && <div className="record-body">备注：{a.remark}</div>}

            <div className="record-actions">
              {showActions && (
                <>
                  <button className="btn small" disabled={!!busy}
                          onClick={() => onReply(a, 'confirmed')}>
                    {busy === `a${a.id}` ? '处理中…' : '确认看房'}
                  </button>
                  <button className="btn ghost small" disabled={!!busy}
                          onClick={() => onReply(a, 'rejected')}>
                    拒绝
                  </button>
                </>
              )}
              {a.tenant_username && (
                <button className="btn ghost small" disabled={!!busy}
                        onClick={() => onTalk(a)}
                        title={a.accept_contact === false
                          ? '该租客关闭了主动联系' : ''}>
                  {busy === `t${a.id}` ? '打开中…' : '找他说'}
                </button>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
