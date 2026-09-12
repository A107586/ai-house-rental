import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import ChatAvatar from './ChatAvatar'

const MapFinder = lazy(() => import('./MapFinder'))
const LandlordPanel = lazy(() => import('./LandlordPanel'))
const AgentPanel = lazy(() => import('./AgentPanel'))
const AdminPanel = lazy(() => import('./AdminPanel'))

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
 * ## 全部功能都在一级，没有「再打开一个面板」
 *
 * 第一版把事务性功能（发布房源、合同、工单、佣金）留在原面板，
 * 左栏最下方给一个「房源与账目」入口。验收意见明确否了这个做法：
 * **所有功能都要在一级界面**。
 *
 * 现在左栏一次列全：沟通类（会话、看房预约、今日带看、房源分布）
 * 由本组件自己渲染，事务类直接嵌入原面板——
 *
 * **没有把三个面板重写一遍。** 每个页签下的内容都已经调好、
 * 也都有人在用；重写只会得到两套各自漂移的逻辑，改一处忘另一处
 * 会变成常态。面板要脱掉的只是抽屉那层壳（遮罩、关闭键、
 * 标题、横向页签），正文原样复用，页签改由这里的左栏控制。
 *
 * 管理后台同样走这一套：它原来是十七个页签横向排开、多到要横滚，
 * 左栏竖排一屏就能列完，当前位置也一直看得见。
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

const ROLE_TITLE = {
  landlord: '房东工作台', agent: '中介工作台', admin: '管理后台',
}

// 本组件自己渲染的那几栏（沟通类）。其余栏目由嵌入的角色面板渲染，
// 清单由面板通过 onSections 报上来——**计数得是面板算的那一份**，
// 在这里再算一遍迟早会和正文对不上。
//
// 分角色是因为**中介面板自己就有「今日带看与联系」**，那一栏比这里的
// 简单列表多了「房东邀请我接管」和「等我确认的预约」。两边 key 撞在
// 一起时，过滤会把面板那一栏挤掉，中介就再也看不到邀请了——
// 实测就是这么丢的。房东面板没有对应栏目，所以由这里补上。
const COMM_BY_ROLE = {
  landlord: ['chat', 'appt', 'today', 'map'],
  agent: ['chat', 'map'],
  admin: [],
}

export default function Workbench({ user, onClose, onOpenHouse }) {
  const isAdmin = user.role === 'admin'
  // 管理员没有「与租客沟通」这一路，直接从后台的第一栏开始
  const [nav, setNav] = useState(
    { admin: 'review', agent: 'chat', landlord: 'chat' }[user.role] || 'chat')
  const [sections, setSections] = useState([])
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

  const title = ROLE_TITLE[user.role] || '工作台'

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
    // 管理员不进会话这一路，别白拉一遍
    if (isAdmin) return
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

  // 左栏 = 沟通类（本组件渲染）+ 面板报上来的业务类。
  //
  // **两段拼成一条竖列，不是两条**：分成两块的话，用户要先判断
  // 「我要找的东西属于哪一块」才知道往哪看，而他并不关心这个分界。
  const commKeys = COMM_BY_ROLE[user.role] || []
  const ALL_COMM = {
    chat: { key: 'chat', label: '会话', n: unread, hint: '与租客直接沟通',
            group: '要做的事' },
    appt: { key: 'appt', label: '看房预约', n: pending.length,
            hint: '待我确认的申请', group: '要做的事' },
    today: { key: 'today', label: '今日带看', n: today.length,
             hint: '今天要去的场次', group: '要做的事' },
    map: { key: 'map', label: '房源分布', hint: '在地图上按区域挑，位置和周边都看得见',
           group: '要做的事' },
  }
  const COMM = commKeys.map((k) => ALL_COMM[k])
  const NAV = [...COMM, ...sections.filter((x) => !commKeys.includes(x.key))]

  const active = convs.find((c) => c.id === activeId)

  // 按区聚合自己的房源，喂给地图。
  //
  // **在前端算而不是再开一个接口**：数据已经在手上（houses 就是
  // 名下全部房源），再往返一次只会多一次加载态；而且这样两处
  // 一定一致——地图说「中山区 3 套」，下面列出来的就一定是那 3 套。
  const myDistricts = (() => {
    const acc = {}
    houses.forEach((h) => {
      const key = h.district || '未填写'
      if (!acc[key]) acc[key] = { district: key, count: 0, _rent: 0 }
      acc[key].count += 1
      acc[key]._rent += Number(h.rent_price) || 0
    })
    return Object.values(acc).map((d) => ({
      district: d.district,
      count: d.count,
      // 地图的「平均租金」那一档要用到。除以 0 不会发生——
      // 能进这个分支说明至少有一套
      avg_rent: Math.round(d._rent / d.count),
    }))
  })()

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
          {NAV.map((item, i) => (
            <span className="wb-nav-slot" key={item.key}>
              {/* 组名只在换组时画一次。管理后台十七项分三组，
                  不分组的话一整列同样大小的按钮，扫下去找不到边界 */}
              {item.group && item.group !== NAV[i - 1]?.group && (
                <span className="wb-rail-group">{item.group}</span>
              )}
              <button
                className={`wb-nav${nav === item.key ? ' on' : ''}`}
                onClick={() => { setNav(item.key); setOk('') }}
              >
                <span className="wb-nav-main">
                  {item.label}
                  {item.n > 0 && <span className="wb-nav-n">{item.n}</span>}
                </span>
                {item.hint && <span className="wb-nav-hint">{item.hint}</span>}
              </button>
            </span>
          ))}
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
                          还没有消息。可以先问一句「方便什么时候看房」。
                        </div>
                      )}
                      {lines.map((line) => (
                        <div className={`bubble ${line.mine ? 'user' : 'assistant'}`}
                             key={line.id}>
                          /* A2：对方的头像用**这个会话里对方的名字**，不是一个占位词。

   `sender_label` 只在「同侧但不是我」时才有值（那是委托期间中介
   代答的情形），普通两人会话里它永远是空串。于是这里原来回退到
   「对方」「租客」这种字面量——头像取首字，结果每一个会话的对方
   都长着同一个「对」或「租」。**换个人说话，画面上没有任何变化**，
   而头像的全部作用就是让人一眼认出说话的是谁。 */
                          <ChatAvatar
                            kind={line.mine ? 'me' : 'other'}
                            name={line.mine
                              ? (user.real_name || user.username)
                              : (line.sender_label || active?.peer_name || '租客')}
                          />
                          {line.sender_label && (
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
                      请在平台内沟通。交换微信、电话会被拦截——私下成交，平台无法提供保障。
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
                ? '没有待确认的看房申请'
                : '今天没有已确认的带看。'}
              showActions={nav === 'appt'}
              busy={busy}
              onReply={reply}
              onTalk={talkTo}
              onOpenHouse={onOpenHouse}
            />
          )}

          {nav === 'map' && (
            <div className="wb-houses">
              {/* 地图按验收意见保留。它在这里的用处和列表页不同：
                  房东看的是**自己的房子铺在哪几个区**，
                  哪个区空置多就该往哪个区使劲 */}
              <Suspense fallback={<div className="map-loading">地图加载中…</div>}>
                {/* A1：口径必须是「我的房源」，不是全站。
                    默认的 MapFinder 读 /api/stats，那是全城统计——
                    房东在这里看到「旅顺口区 18 套」，而他在那个区
                    一套也没有。统计由这里按 houses 算好传进去。 */}
                <MapFinder
                  selected={district}
                  onPick={setDistrict}
                  rows={myDistricts}
                  title="我的房源分布"
                  hint={houses.length
                    ? '点击区域只看该区的房源'
                    : '名下还没有房源'}
                />
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

          {/* 业务类栏目交给原面板渲染。
              **面板始终挂着，不随栏目切换而卸载**——卸载会让它每次
              重新拉一遍数据，切回来还得再等一次；而且它自己维护的
              表单草稿（发布房源填到一半）会全部丢掉。
              不显示的时候用 hidden 藏起来就够了。 */}
          <div className="wb-embed-host" hidden={commKeys.includes(nav)}>
            <Suspense fallback={<div className="wb-empty">加载中…</div>}>
              {user.role === 'landlord' && (
                <LandlordPanel
                  user={user} embedded
                  onSections={setSections}
                  activeTab={commKeys.includes(nav) ? '' : nav}
                  onTabChange={setNav}
                  onOpenHouse={onOpenHouse}
                  onClose={onClose}
                />
              )}
              {user.role === 'agent' && (
                <AgentPanel
                  user={user} embedded
                  onSections={setSections}
                  activeTab={commKeys.includes(nav) ? '' : nav}
                  onTabChange={setNav}
                  onOpenHouse={onOpenHouse}
                  onClose={onClose}
                />
              )}
              {user.role === 'admin' && (
                <AdminPanel
                  user={user} embedded
                  onSections={setSections}
                  activeTab={commKeys.includes(nav) ? '' : nav}
                  onTabChange={setNav}
                  onClose={onClose}
                />
              )}
            </Suspense>
          </div>

          {nav === 'tenants-unused' && (
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
