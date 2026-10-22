import { useEffect, useRef, useState } from 'react'
import { api, getToken } from '../api'
import {
  readJSON, readText, remove, scopedKey, writeJSON, writeText,
} from '../storage'
import { useDelayedFlag, useExitTransition } from '../useMotion'
import ChatAvatar from './ChatAvatar'
import Thinking, { guessIntent } from './Thinking'

/**
 * AI-09 多轮对话面板。
 *
 * 会话编号存在 localStorage 里，刷新页面能接着上一次聊。
 * 登录用户的对话存在后端，换设备也能续上；未登录用户由前端把自己
 * 说过的话带给智能体——换设备记不住，但当前这轮对话照样是多轮的。
 */
// 这两个键都要按账号分开存，理由见 storage.js。
// 同一台电脑换个人登录，恢复出来的不能是上一个人的对话。
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

function newSessionId() {
  return 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
}

function loadDraft(user) {
  // 存的内容坏了就当没有，重新开始，别让一段坏 JSON 把面板打不开
  const draft = readJSON(scopedKey(DRAFT_KEY, user))
  if (!draft || !Array.isArray(draft.messages) || !draft.messages.length) {
    return null
  }
  return draft
}

function saveDraft(user, messages, pending) {
  // 存不下（隐私模式、配额满）不影响这一轮对话，writeJSON 里已经吞掉了
  writeJSON(scopedKey(DRAFT_KEY, user), {
    messages: messages.slice(-DRAFT_LIMIT), pending,
  })
}

function loadSessionId(user) {
  const key = scopedKey(SESSION_KEY, user)
  const saved = readText(key)
  if (saved) return saved
  const id = newSessionId()
  // 隐私模式下写不进去，那就退化成一次性会话：这一轮照样是多轮的，
  // 只是刷新之后接不上。比整个面板打不开好。
  writeText(key, id)
  return id
}

const OPENING = {
  role: 'assistant',
  content: '你好，我是租房助手。找房可以一句一句补条件，我都记得住；'
    + '押金怎么退、退租要提前多久这类平台规定也可以问我，查不到的我会直说。'
    + '交房租、约看房、报修也能帮你办，办之前会先让你确认。',
}

const QUICK = ['中山区两室一厅', '三千以内的', '押金怎么退', '水电费怎么算']

// 一句话最多带几张图。和智能体那边读图的上限一致（support/tools/photo.py MAX_IMAGES）
const MAX_ATTACH = 3

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

// 账单类型同样是英文枚举（库里迁移成了 rent / deposit），
// 不翻的话卡片上会写「rent 2200 元」。字段名新旧两种都认。
const BILL_TYPE_TEXT = {
  rent: '租金', deposit: '押金', utility: '水电费',
  property: '物业费', penalty: '违约金', other: '其他费用',
}

const billType = (bill) => {
  const raw = bill.bill_type || bill.type || ''
  if (!raw) return '租金'
  return BILL_TYPE_TEXT[raw] || raw
}

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
// 查库卡片里哪几类是「一列房源」。用户对着它们说「第一套」时，
// 指的就是卡片上第一行——见 lastHouseNos()。
const HOUSE_LIST_TOPICS = new Set(['favorite', 'history', 'house'])

const ACCOUNT_ROWS = {
  bill: (data) => (data.unpaid || []).map((bill) => ({
    key: bill.bill_no,
    main: `${billType(bill)} ${money(bill.amount)} 元`,
    sub: bill.due_date ? `${bill.due_date} 到期` : '',
  })),
  contract: (data) => (data.list || []).map((item) => ({
    key: item.contract_no,
    main: `${item.house_no || ''} ${item.contract_no}`,
    sub: `${item.start_date} 至 ${item.end_date} · ${statusText(item.status)}`,
  })),
  // 预约和报修上面就是一条轨迹，行里不再重复状态——
  // 轨迹说「维修中」、同一张卡的行里却写「待处理」，看着像系统在自相矛盾。
  booking: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: item.house_title || item.house_no,
    sub: (item.viewing_time || '').slice(0, 16),
  })),
  repair: (data) => (data.list || []).map((item) => ({
    key: item.id,
    main: (item.content || '').slice(0, 24),
    sub: item.house_no || '',
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
    // agent 是中介（J-02）。少写一个，中介看到的就是「身份：agent」。
    const role = {
      tenant: '租客', landlord: '房东', agent: '中介', admin: '管理员',
    }
    return [
      { key: 'username', main: me.username, sub: role[me.role] || me.role },
      me.phone ? { key: 'phone', main: '手机号', sub: me.phone } : null,
    ].filter(Boolean)
  },
}

/** 对话里的图片上传。
 *
 * 助手问「要拍张照片吗」的时候，用户面前不该只有一个输入框——他没法
 * 把一张图打进去。所以这一步渲染成选文件按钮 + 一个跳过。
 *
 * 图片直接传给业务后端换成地址，**不经过智能体**：智能体不碰二进制，
 * 它只收一串地址；后端那边还会再校验一次「是不是本系统传的」。
 */
function UploadStep({ onPicked, busy }) {
  const inputRef = useRef(null)
  const [error, setError] = useState('')
  const [working, setWorking] = useState(false)

  async function pick(event) {
    const files = [...(event.target.files || [])].slice(0, 6)
    event.target.value = ''
    if (!files.length) return
    setWorking(true)
    setError('')
    try {
      const urls = []
      for (const file of files) {
        const data = await api.uploadImage(file)
        if (data.url) urls.push(data.url)
      }
      if (urls.length) onPicked(urls.join(','))
      else setError('没拿到图片地址，再试一次')
    } catch (err) {
      // 传图失败不该把这一步堵死——用户随时可以跳过继续报修
      setError(err.message || '上传失败')
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="upload-step">
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={pick}
      />
      <button
        className="btn small"
        disabled={busy || working}
        onClick={() => inputRef.current?.click()}
      >
        {working ? '上传中…' : '选照片（最多 6 张）'}
      </button>
      {error && <span className="upload-err">{error}</span>}
    </div>
  )
}

/** 长表单的填写进度。
 *
 * 「（第 6/11 项）」写在文字里也能看见，但十一项填下来，用户真正想知道的是
 * **已经填了什么**——那份清单只有摆出来才看得清，也才好在提交前发现填错了。
 * 中途跑题问了别的，回来一眼就能接上。
 */
/** 一套房的讲解卡：亮点、要留意的、价位。
 *
 * 助手那段话已经把结论说了，这张卡负责**让人扫一眼就看完**——
 * 亮点和短板分两栏摆着，比一段话里用分号串起来好读得多。
 *
 * 短板必须和亮点同样显眼。只列好处的卡片，用户到现场就知道被骗了，
 * 而看房是要花半天时间的；反过来，敢把短板写出来，亮点那一栏才有人信。
 */
function PitchCard({ data, onOpenHouse }) {
  const house = data?.house
  if (!house) return null
  const price = data.price || {}
  return (
    <div className="pitch-card">
      <button
        className="pitch-head"
        onClick={() => onOpenHouse && onOpenHouse(house)}
      >
        <span className="pitch-title">{house.title || house.house_no}</span>
        <span className="pitch-rent">{money(house.rent_price)} 元/月</span>
      </button>

      {price.unit != null && (
        <div className={`pitch-price ${price.diff <= -8 ? 'good' : ''}${price.diff >= 12 ? 'warn' : ''}`}>
          每平米 {price.unit} 元
          {price.diff === 0
            ? `，正好是${price.scope}的中位价`
            : `，${price.diff < 0 ? '低于' : '高于'}${price.scope}中位价 ${Math.abs(price.diff)}%`}
          <span className="pitch-sample">（同类 {price.sample} 套）</span>
        </div>
      )}

      {data.highlights?.length > 0 && (
        <ul className="pitch-list good">
          {data.highlights.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {data.cautions?.length > 0 && (
        <ul className="pitch-list warn">
          {data.cautions.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {data.suits && <div className="pitch-suits">{data.suits}</div>}
    </div>
  )
}

/** 两套房的对比表。
 *
 * `best` 是后端算好的「这一项哪一列更优」，只在**分得出高下**的项上给：
 * 户型、朝向、位置没有优劣之分，评分在没人评过时也不算数。
 * 前端不再自己判一遍——两处各判一次，早晚会判得不一样。
 */
function CompareCard({ data, onOpenHouse }) {
  const houses = data?.houses || []
  if (houses.length < 2) return null
  return (
    <div className="compare-card">
      <table className="compare-table">
        <thead>
          <tr>
            <th />
            {houses.map((house) => (
              <th key={house.house_no}>
                <button
                  className="link"
                  onClick={() => onOpenHouse && onOpenHouse(house)}
                >
                  {house.house_no}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(data.rows || []).map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              {row.values.map((value, i) => (
                <td
                  key={`${row.label}-${i}`}
                  className={row.best === i ? 'best' : ''}
                >
                  {value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FormProgress({ form }) {
  if (!form || !form.total) return null
  const pct = Math.round((form.done / form.total) * 100)
  return (
    <div className="form-card">
      <div className="form-head">
        <span className="form-label">{form.label}</span>
        <span className="form-count">{form.done}/{form.total}</span>
      </div>
      <div className="form-bar"><span style={{ width: `${pct}%` }} /></div>
      {form.filled?.length > 0 && (
        <div className="form-filled">
          {form.filled.map((item) => (
            <span className="form-chip" key={item.label}>
              <b>{item.label}</b> {item.value}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 办事进度轨迹。
 *
 * 「当前状态：待处理」说的是对的，但用户看完仍然不知道「还要等什么、
 * 下一步该谁动」，于是还是要再问一次人。京东客服把物流整条轨迹摆出来，
 * 就是为了省掉这一次追问——报修和预约是租房里最像物流的两件事。
 */
function Timeline({ timeline }) {
  const steps = timeline?.steps || []
  if (!steps.length) return null
  return (
    <div className="timeline">
      {steps.map((step) => (
        <div className={`tl-step tl-${step.state}`} key={step.label}>
          <span className="tl-dot" />
          <div className="tl-body">
            <span className="tl-label">{step.label}</span>
            {step.at && <span className="tl-at">{step.at}</span>}
            {step.note && <div className="tl-note">{step.note}</div>}
          </div>
        </div>
      ))}
    </div>
  )
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
      {data.timeline && <Timeline timeline={data.timeline} />}
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

/**
 * 注意：调用方给这个组件挂了 `key={identity}`（见 App.jsx）。
 * 换账号时它**整个重挂**，下面这几个 `useState` 初始化函数会带着
 * 新身份重新跑一遍——否则 React 会保留旧 state，
 * 上一个人的对话就留在屏幕上了。
 */
export default function ChatPanel({ user, houseNo = '', onOpenHouse, onClose }) {
  const [sessionId] = useState(() => loadSessionId(user))
  // 优先恢复上次没聊完的，没有才从开场白开始
  const [messages, setMessages] = useState(() => loadDraft(user)?.messages || [OPENING])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [thinkIntent, setThinkIntent] = useState('default')
  // 流式接口推来的：此刻真实在做的那一步（「在查你的账单」）和已经生成的那部分回答
  const [streamStep, setStreamStep] = useState('')
  const [streamText, setStreamText] = useState('')

  // ---- 以下三个只管动效，不参与任何业务判断 ----
  //
  // 思考气泡的退场。原来是 `{busy && <thinking-bubble>}`，busy 一转 false
  // 气泡就从 DOM 里消失，而正式回答同一刻在它上方出现——一帧之内
  // 「转圈的没了、答案冒出来了」。延迟 160ms 卸载，配合气泡自己的进场，
  // 读起来是思考气泡原地变成了回答。
  const { mounted: thinkingOn, closing: thinkingClosing } = useExitTransition(busy)

  // 退场那 160ms 里 busy 已经 false、streamText 也被 send 的 finally 清空了，
  // 照原样渲染会**闪回转圈再淡出**。所以退场期间改用这份快照。
  //
  // 只在 busy 期间跟踪、不在退场期间更新：busy 还是 true 的时候一律用真实的
  // streamText。auto_confirm 那条路上流式文字清空后还要 await 一次执行接口，
  // 那段时间界面该继续转圈（它确实还在办），不能被冻住的文字盖住。
  const exitSnapshot = useRef('')
  if (busy) exitSnapshot.current = streamText
  const thinkingText = thinkingClosing ? exitSnapshot.current : streamText

  // 发送按钮的转圈。忙过 400ms 才显示，原因见 useMotion.js：
  // 快请求闪一下的转圈比完全不转更让人心慌。
  const sendBusy = useDelayedFlag(busy)
  const [context, setContext] = useState('')
  // 未完成的代办：助手问了哪个槽、已经填了什么、或者已经摆出的确认卡片。
  // 服务端不存会话态，这份状态由前端带着走。
  const [pending, setPending] = useState(() => loadDraft(user)?.pending || null)
  const [error, setError] = useState('')
  // 开场白给的快捷入口。它是「这个人现在有什么没办完」算出来的，
  // 跟固定的 QUICK 不是一回事，所以单独存一份。
  const [chips, setChips] = useState([])
  const bottomRef = useRef(null)
  // 随下一句话一起发的照片。选中就先传到业务后端换成地址，这里只存地址——
  // 智能体只认本系统上传的 /static/ 路径，不收二进制，也不收外链。
  const [attachments, setAttachments] = useState([])
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  // 开场白：先问一次「这个人现在有什么没办完」。
  //
  // 原先是一段写死的自我介绍，讲完把「该问点什么」原样丢回给用户。
  // 购物软件的客服不这么做——点开就是「你有一笔待付款、一个包裹在派送」，
  // 每条后面跟一个能点的按钮。接口不调大模型，几十毫秒就回来了。
  useEffect(() => {
    // 聊到一半关掉又打开，不该把上下文冲掉换成开场白。
    //
    // **但从房源页点「问助手」进来是例外。** 原先这一行无差别地
    // 早退，而草稿几乎总是存在（聊过一次就有了）——于是用户在房源页
    // 点了「问助手」，面板打开还是上一场对话，一个字都没提这套房，
    // 他自然会觉得「房源根本没发过去」。
    // 这种时候不是恢复现场，是**开一段关于这套房的新对话**。
    const draft = loadDraft(user)
    if (draft && !houseNo) return undefined
    let alive = true
    api
      .supportGreeting(houseNo)
      .then((data) => {
        if (!alive || !data.message) return
        setMessages((prev) => {
          const opening = { role: 'assistant', content: data.message }
          if (!houseNo) return [opening]
          // 带着房源进来：保留之前聊过的，加一条分隔，再接开场白。
          // 直接清空等于把他刚才问到一半的事情抹掉。
          const kept = (draft?.messages || prev || []).slice(-DRAFT_LIMIT)
          return [...kept,
                  { role: 'assistant', kind: 'divider',
                    content: `以下关于 ${houseNo}` },
                  opening]
        })
        setChips(data.chips || [])
      })
      .catch(() => {
        // 取不到就用组件里那句静态开场白，不影响使用
      })
    return () => { alive = false }
  }, [houseNo, user])

  // 登录用户把之前存在后端的对话拉回来
  useEffect(() => {
    if (!getToken()) return
    let alive = true
    // 本地草稿比后端历史更完整——后端只存文字，没有确认卡片与待办状态。
    // 已经从草稿恢复过就别再覆盖，否则聊到一半的卡片会被纯文本冲掉。
    if (loadDraft(user)) return undefined
    api
      .chatHistory(sessionId)
      .then((data) => {
        if (!alive || !data.list?.length) return
        // 后端存的是**纯文字**，卡片、可点选项、查库小卡片一个都没有。
        // 直接当成普通气泡铺出来，用户看到的是一屏白底黑字，
        // 里面还夹着「要取消收藏哪一套？」这种问到一半、却点不动的句子——
        // 看上去就像这些功能根本没做。所以明确标成「以前的记录」，
        // 并在末尾画一条线，把历史和这一轮分开。
        setMessages([
          OPENING,
          ...data.list.map((m) => ({
            role: m.role, content: m.content, history: true,
          })),
          {
            role: 'assistant', kind: 'divider',
            content: '以上是之前聊过的记录，只存了文字。'
              + '卡片和选项不会保留，继续办理请重新说一句。',
          },
        ])
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
    saveDraft(user, messages, pending)
  }, [user, messages, pending])

  // 未登录时的上下文兜底：把对话再带回去。
  //
  // **带署名，双方的话都带。** 服务端 user_turns() 只挑 role=user 的去重放
  // 找房条件，助手的话不会被当成条件解析；而「我上一条问了什么」「总结一下」
  // 这类追问，答案在助手说过的话里——原来只传用户的话，它们无从回答。
  function localHistory() {
    return messages
      .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content)
      .slice(-16)
      .map((m) => ({ role: m.role, content: String(m.content).slice(0, 600) }))
  }

  // 上一轮结果的房源编号，用于消解「第二套」「那套」。
  //
  // 从房源详情页进来时，当前这套排在最前面——用户说「收藏这套」
  // 指的显然是他正在看的那套，而不是上一轮搜索结果里的第一条。
  function lastHouseNos() {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      // **屏幕上最近一份房源列表，不管它是用哪种卡片摆出来的。**
      //
      // 原来只认 `items`（找房结果的样式）。可「我的浏览记录」「我的收藏」
      // 是查库卡片，房源放在 account.data.list 里——于是用户刚看完浏览记录
      // 问「第一套评价怎么样」，助手数的是**更早那一份**列表的第一套，
      // 答了一套他根本没在屏幕上看见的房。
      const acc = messages[i].account
      const accNos = acc && HOUSE_LIST_TOPICS.has(acc.tool || acc.topic)
        ? (acc.data?.list || []).map((h) => h.house_no).filter(Boolean)
        : []
      if (accNos.length && !messages[i].items?.length) {
        return houseNo && !accNos.includes(houseNo) ? [...accNos, houseNo] : accNos
      }
      if (messages[i].items?.length) {
        const nos = messages[i].items.map((h) => h.house_no)
        // **不能把当前房源插到队首。**
        //
        // 原来是 [houseNo, ...其余]，用意是「从房源页打开助手时，
        // 『这套』该指它」。可这么一插，整份列表的下标全体后移一位：
        // 屏幕上标着 1 的那套，助手数出来是第 2 套。用户说「介绍下第一套」，
        // 拿到的是他压根没在列表里看见的另一套房——
        // **这正是「名字和序号对不上」的来源。**
        //
        // 「这套」指当前房源这件事另有出路：`house_no` 已经随请求
        // 单独传给了后端，指代解析在那边处理，不该靠篡改列表顺序实现。
        return houseNo && !nos.includes(houseNo) ? [...nos, houseNo] : nos
      }
    }
    return houseNo ? [houseNo] : []
  }

  // 最近一轮**单讲一套房**的回答讲的是哪套。
  //
  // 实测：列出 10 套之后问「DL0103 怎么样」，助手介绍完 DL0103，
  // 用户接着说「这套能约周六吗」——约成了列表第一套 DL0074。
  // 「这套」原来只认页面上打开的房源，其次就是列表第一条，
  // 刚刚才聊过的那一套反而不在候选里。往回找，碰到更晚的列表就停：
  // 那之后屏幕上是一整列房，「这套」指哪条又回到了序号的问题。
  function focusHouse() {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i]
      if (m.items?.length) return ''
      if (m.account && HOUSE_LIST_TOPICS.has(m.account.tool || m.account.topic)) return ''
      if (m.focus) return m.focus
    }
    return ''
  }

  /** 选图 / 粘贴进来的图：传到业务后端，拿回地址挂在输入框上方。 */
  async function attach(files) {
    const picked = [...(files || [])].filter((f) => f.type.startsWith('image/'))
    if (!picked.length) return
    // 上传接口要登录——匿名上传等于把服务器磁盘开放给全网（见后端 /api/upload）
    if (!getToken()) {
      setError('发图片要先登录。')
      return
    }
    const room = MAX_ATTACH - attachments.length
    if (room <= 0) {
      setError(`一次最多带 ${MAX_ATTACH} 张图。`)
      return
    }
    setUploading(true)
    setError('')
    try {
      for (const file of picked.slice(0, room)) {
        const data = await api.uploadImage(file)
        if (data.url) setAttachments((prev) => [...prev, data.url])
      }
    } catch (err) {
      setError(err.message || '图片上传失败')
    } finally {
      setUploading(false)
    }
  }

  function onPaste(event) {
    const files = [...(event.clipboardData?.files || [])]
    if (files.some((f) => f.type.startsWith('image/'))) {
      event.preventDefault()
      attach(files)
    }
  }

  async function send(e, override) {
    if (e) e.preventDefault()
    const content = (override ?? text).trim()
    // 快捷按钮、选项、上传步骤走 override，不带输入框上方挂着的图
    const images = override === undefined ? attachments : []
    if ((!content && !images.length) || busy || uploading) return

    const history = localHistory()
    const houseNos = lastHouseNos()
    const focus = focusHouse()
    setMessages((prev) => [...prev, { role: 'user', content, images }])
    setText('')
    if (images.length) setAttachments([])
    setBusy(true)
    // H1：等待期间显示哪一份阶段清单。猜错只是短语不贴切，
    // 真正的路由在服务端
    setThinkIntent(guessIntent(content))
    setError('')

    try {
      const extra = { seen: houseNos.length, houseNo: focus || houseNo, images }
      let res
      try {
        // 流式：「在查你的账单」这类真实进度先出来，回答一个字一个字显示在等待气泡里；
        // 收到最终结果后照原样处理，等待气泡换成正式的那条消息。
        // 一开始就给一句真实的话：不给的话 Thinking 会轮播固定清单，跑到「组织回答」
        // 时真实的「在查平台规则」「在看查到的结果」才到，看着像先答后查
        setStreamStep('在理解你的问题')
        res = await api.agentChatStream(content, sessionId, history, houseNos, pending, extra,
          (event) => {
            if (event.type === 'step') setStreamStep(event.label || '')
            else if (event.type === 'delta') setStreamText((cur) => cur + event.text)
            else if (event.type === 'reset') setStreamText('')
          })
      } catch (err) {
        // 只有「流式接口用不了」才退回普通接口；模型答错、服务报错照常往外抛
        if (!err.streamUnsupported) throw err
        setStreamStep('')
        res = await api.agentChat(content, sessionId, history, houseNos, pending, extra)
      } finally {
        setStreamText('')
        setStreamStep('')
      }
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

      // 翻页：接在上一批后面，不另起一列。
      //
      // 另起一列的话序号会从 1 重新数——用户刚记住的「第 3 套」
      // 换成了另一套房，而 `last_house_nos` 也只剩新的这一批，
      // 「介绍下第二套」会指到错的地方。
      if (res.append && (res.items || []).length) {
        setMessages((prev) => {
          const next = [...prev]
          for (let i = next.length - 1; i >= 0; i -= 1) {
            if (next[i].items?.length) {
              const have = new Set(next[i].items.map((h) => h.house_no))
              next[i] = {
                ...next[i],
                items: [...next[i].items,
                        ...res.items.filter((h) => !have.has(h.house_no))],
                total: res.total || next[i].total,
              }
              break
            }
          }
          return [...next, { role: 'assistant', content: res.message || '' }]
        })
        setBusy(false)
        return
      }

      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: res.message || '（没有返回内容）',
        items: res.items || [],
        // 库里一共有多少套。「还有几套」要拿它算，不能拿手上这批算
        total: res.total || 0,
        // 助手挑出来的那一套，列表里给它一个标记——
        // 光在文字里说「我推荐第 6 套」，用户还得自己往下数
        recommended: res.recommended || null,
        // 这一轮单讲的是哪套房，下一句「这套」先指它，见 focusHouse()
        focus: (res.items || []).length ? '' : (res.house_no || ''),
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
        // 长表单进度。非表单的轮次是 null，不渲染
        form: res.form || null,
        // 讲解某一套 / 对比两套。两者互斥，各自渲染成一张卡片。
        pitch: res.pitch || null,
        compare: res.compare || null,
        // 这一轮问的是什么。kind === 'upload' 时要渲染选图按钮，
        // 而不是让用户对着输入框发愣
        ask: res.pending?.ask || null,
        logId: res.log_id || null,
        status: res.status || '',
        feedback: 0,
      }])
    } catch (err) {
      setError(err.message)
      setMessages((prev) => [...prev, {
        role: 'assistant',
        // 这句话组员看了两次都没看出问题在哪，所以把排查方向直接写进去。
        // 「联系不上」最常见的原因不是网络，是 8000 端口那个服务压根没起来——
        // 智能体有自己的 requirements.txt，只装后端依赖的话它启动即退出。
        content: '助手暂时连不上，请稍后再试。',
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
      await api.supportFeedback(message.logId, value, sessionId)
    } catch {
      // 评价发不出去不该打断对话，界面上已经给了反馈
    }
  }

  /** 转人工：把这个问题留给管理员，回复走站内消息。 */
  async function handoff(index) {
    const message = messages[index]
    if (!message?.handoff) return
    if (!getToken()) {
      setError('转人工要先登录，管理员才知道回给谁。')
      return
    }
    try {
      const res = await api.supportHandoff(
        message.handoff.question, message.handoff.reason,
        localHistory().slice(-6)
          .map((m) => `${m.role === 'user' ? '用户' : '助手'}：${m.content}`)
          .join('\n'))
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
    // 只清这个账号自己那份。`remove` 里已经吞掉了隐私模式下的异常，
    // 清不掉也不拦着用户重新开始。
    remove(scopedKey(DRAFT_KEY, user))
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
          找房、问规定、办事都可以问我
        </div>

        {context && (
          <div className="chat-context">
            <span className="chat-context-k">当前记得</span>
            {context}
            <button className="btn ghost small" onClick={restart}>
              重新开始
            </button>
          </div>
        )}

        <div className="chat-log">
          {messages.map((message, index) => (
            message.kind === 'divider' ? (
              <div className="chat-divider" key={index}>{message.content}</div>
            ) : (
            <div
              className={`bubble ${message.role}${message.history ? ' history' : ''}`}
              key={index}
            >
              {/* E1：头像。助手是固定标记，用户取姓氏首字——
                  一屏滚下去，靠标记就能扫出哪几段是它说的 */}
              <ChatAvatar
                kind={message.role === 'user' ? 'me' : 'ai'}
                name={message.role === 'user' ? (user?.real_name || user?.username || '我') : ''}
              />
              {message.images?.length > 0 && (
                <div className="bubble-images">
                  {message.images.map((url) => (
                    <a href={url} target="_blank" rel="noreferrer" key={url}>
                      <img src={url} alt="发送的照片" />
                    </a>
                  ))}
                </div>
              )}
              {message.content && <div className="bubble-text">{message.content}</div>}

              {message.notes?.length > 0 && (
                <div className="bubble-notes">
                  {message.notes.map((note) => (
                    <span className="note" key={note}>{note}</span>
                  ))}
                </div>
              )}

              {message.form && <FormProgress form={message.form} />}

              {message.pitch && (
                <PitchCard data={message.pitch} onOpenHouse={onOpenHouse} />
              )}
              {message.compare && (
                <CompareCard data={message.compare} onOpenHouse={onOpenHouse} />
              )}

              {message.account?.data && (
                <AccountCard
                  topic={message.account.tool || message.account.topic}
                  data={message.account.data}
                  onOpenHouse={onOpenHouse}
                />
              )}

              {message.ask?.kind === 'upload' && (
                <UploadStep busy={busy} onPicked={(urls) => send(null, urls)} />
              )}

              {message.options?.length > 0 && (
                <div className="bubble-options">
                  {/* 选项两种形态都要认：追问给的是 {value,label}，
                      而讲解/对比之后的下一步只是几句现成的话。
                      在这里统一，比要求每个产出方都包一层可靠——
                      少包一次的后果是按钮整排消失，且不报错。 */}
                  {message.options.map((option) => {
                    const value = typeof option === 'string' ? option : option.value
                    const label = typeof option === 'string' ? option : option.label
                    return (
                      <button
                        className="chip-btn"
                        key={value}
                        disabled={busy}
                        onClick={() => send(null, value)}
                      >
                        {label}
                      </button>
                    )
                  })}
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
                    交给管理员处理，回复会发到站内消息
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
                      {message.feedback > 0 ? '谢谢反馈' : '已记下，我们会补上这块内容'}
                    </span>
                  )}
                </div>
              )}

              {message.items?.length > 0 && (
                <div className="bubble-items">
                  {/* **列出智能体给的全部，不再只切前 5 条。**
                      它说「我最推荐第 6 套」，界面上却只有 5 行——
                      用户找不到第 6 套，也点不开它。序号是助手和用户之间
                      唯一的共同坐标，一边数 10 个一边显示 5 个，
                      这个坐标就失效了。 */}
                  {message.items.map((house, i) => (
                    <button
                      className={`mini-house${
                        message.recommended
                        && message.recommended.house_no === house.house_no
                          ? ' picked' : ''}`}
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
                  {/* 「还有几套」算的是**库里还剩多少**，不是「我藏了几套」。
                      原来写的是 items.length - 5：那 5 套早就传过来了，
                      说「还有 5 套」既不准（实际还有 51 套），
                      也让人以为要再点一次才看得到已经在手上的东西。 */}
                  {message.total > message.items.length && (
                    <div className="mini-more">
                      还有 {message.total - message.items.length} 套，说「再多看几套」
                    </div>
                  )}
                </div>
              )}
            </div>
            )
          ))}

          {thinkingOn && (
            <div className={`bubble assistant thinking-bubble${thinkingClosing ? ' is-closing' : ''}`}>
              <ChatAvatar kind="ai" />
              {thinkingText
                ? <div className="bubble-text streaming">{thinkingText}</div>
                : <Thinking intent={thinkIntent} label={streamStep} />}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {error && <div className="reply failed">{error}</div>}

        <div className="chat-quick">
          {(chips.length ? chips : QUICK).map((sample) => (
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

        {(attachments.length > 0 || uploading) && (
          <div className="chat-attachments">
            {attachments.map((url) => (
              <button
                type="button"
                key={url}
                title="移除这张"
                onClick={() => setAttachments((prev) => prev.filter((u) => u !== url))}
              >
                <img src={url} alt="待发送的照片" />
                <span aria-hidden="true">×</span>
              </button>
            ))}
            {uploading && <span className="chat-attach-hint">上传中…</span>}
          </div>
        )}

        <form className="chat-input" onSubmit={send}>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => { attach(e.target.files); e.target.value = '' }}
          />
          <button
            className="btn ghost chat-attach"
            type="button"
            title={getToken() ? '发图片（也可以直接粘贴截图）' : '登录后可以发图片'}
            disabled={busy || uploading || attachments.length >= MAX_ATTACH}
            onClick={() => fileRef.current?.click()}
          >
            图片
          </button>
          <input
            placeholder={attachments.length ? '说说这张图，或者直接发送' : '说点什么，比如「换成沙河口」'}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onPaste={onPaste}
          />
          <button
            className={`btn${sendBusy ? ' is-busy' : ''}`}
            type="submit"
            disabled={busy || uploading || (!text.trim() && !attachments.length)}
          >
            发送
          </button>
        </form>
      </aside>
    </>
  )
}
