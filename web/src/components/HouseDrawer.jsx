import { useEffect, useState } from 'react'
import ImageUploader from './ImageUploader'
import { api } from '../api'

// 智能体返回的 status 与展示样式的对应关系。
// created / analyzed 是成功，need_* 需要用户补充信息，其余都是失败。
const REPLY_STYLE = {
  created: 'created',
  analyzed: 'created',
  need_time: 'pending',
  need_choice: 'pending',
  need_house: 'pending',
  need_content: 'pending',
}

const TABS = [
  { key: 'booking', label: '预约看房' },
  { key: 'repair', label: '报修' },
]

export default function HouseDrawer({ house, user, favorited, onToggleFavorite, onClose }) {
  const [tab, setTab] = useState('booking')
  const [booked, setBooked] = useState([])
  const [repairs, setRepairs] = useState([])
  const [reviews, setReviews] = useState({ list: [], average: 0, total: 0 })
  const [text, setText] = useState('')
  const [name, setName] = useState(user?.real_name || '')
  const [phone, setPhone] = useState('')
  const [reply, setReply] = useState(null)
  const [busy, setBusy] = useState(false)
  const [shot, setShot] = useState(0)

  const photos = house.images && house.images.length
    ? house.images
    : (house.cover_image ? [house.cover_image] : [])

  async function refresh() {
    // 必须打一次房源详情接口：浏览量与浏览足迹都由它记录。
    // 卡片列表里已经带了全部字段，看着「不用再查一次」——
    // 但不查的话浏览量永远不增长、足迹永远是空的，
    // 而 AI-07 房源推荐正是靠足迹工作的。
    // 失败不影响抽屉展示，所以单独 catch。
    api.getHouse(house.house_no).catch(() => {})

    try {
      const [appointments, tickets, rated] = await Promise.all([
        api.listAppointments(house.house_no),
        api.listRepairs({ house_no: house.house_no }),
        api.houseReviews(house.house_no),
      ])
      setBooked(appointments.list || [])
      setRepairs(tickets.list || [])
      setReviews(rated)
    } catch {
      setBooked([])
      setRepairs([])
      setReviews({ list: [], average: 0, total: 0 })
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [house.house_no])

  // 按 Esc 关闭抽屉
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 切换标签时清掉上一个功能的回复，避免串台
  useEffect(() => {
    setReply(null)
    setText('')
  }, [tab])

  async function submit(e, useText) {
    // 用 form 的 onSubmit 而不是 onKeyDown，回车与点击走同一条路径
    if (e) e.preventDefault()
    const content = (useText ?? text).trim()
    if (!content || busy) return

    setBusy(true)
    try {
      const applicant = { name, phone }
      const result =
        tab === 'booking'
          ? await api.agentBooking(content, house.house_no, applicant)
          : await api.agentRepair(content, house.house_no, applicant)
      setReply(result)
      if (result.status === 'created') {
        setText('')
        refresh()
      }
    } catch (err) {
      setReply({ status: 'error', message: err.message, alternatives: [] })
    } finally {
      setBusy(false)
    }
  }

  function pickSlot(iso) {
    // 把 ISO 时间还原成用户能读的说法再提交，让智能体重新解析一遍，
    // 保证走的是同一条校验路径
    const d = new Date(iso)
    submit(null, `${d.getMonth() + 1}月${d.getDate()}日${d.getHours()}点`)
  }

  const samples =
    tab === 'booking'
      ? ['这周六下午两点', '明天上午十点', '最近三天都可以']
      : ['卫生间水管漏水了', '空调不制冷', '客厅灯泡坏了，不急']

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>

        {photos.length > 0 && (
          <div className="album">
            <img className="album-main" src={photos[shot]} alt={house.title} />
            {photos.length > 1 && (
              <div className="album-thumbs">
                {photos.map((url, i) => (
                  <button
                    key={url}
                    className={`thumb ${i === shot ? 'on' : ''}`}
                    onClick={() => setShot(i)}
                    aria-label={`第 ${i + 1} 张`}
                  >
                    <img src={url} alt="" />
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <h3>{house.title}</h3>
        <div className="loc">
          {house.district} · {house.community} · {house.address}
        </div>

        <button
          className={`btn ghost small ${favorited ? 'active' : ''}`}
          onClick={() => onToggleFavorite(house.house_no)}
        >
          {favorited ? '★ 已收藏' : '☆ 收藏'}
        </button>

        <div className="facts" style={{ marginTop: 16 }}>
          <dl className="fact">
            <dt>租金</dt>
            <dd>{Math.round(house.rent_price)} 元/月</dd>
          </dl>
          <dl className="fact">
            <dt>押金</dt>
            <dd>{Math.round(house.deposit)} 元</dd>
          </dl>
          <dl className="fact">
            <dt>户型</dt>
            <dd>{house.layout}</dd>
          </dl>
          <dl className="fact">
            <dt>面积</dt>
            <dd>{Math.round(house.area)} 平米</dd>
          </dl>
          <dl className="fact">
            <dt>楼层</dt>
            <dd>{house.floor}</dd>
          </dl>
          <dl className="fact">
            <dt>朝向</dt>
            <dd>{house.orientation}</dd>
          </dl>
        </div>

        <div className="tags">
          {house.tags.map((tag) => (
            <span className="tag" key={tag}>
              {tag}
            </span>
          ))}
        </div>

        <p className="desc" style={{ marginTop: 14 }}>
          {house.description}
        </p>

        {reviews.total > 0 && (
          <>
            <div className="section-title">
              租客评价（{reviews.total}）· 平均 {reviews.average} 分
            </div>
            <ul className="review-list">
              {reviews.list.slice(0, 5).map((r) => (
                <li key={r.id}>
                  <div className="review-head">
                    <span className="stars">{'★'.repeat(r.rating)}
                      <span className="dim">{'★'.repeat(5 - r.rating)}</span></span>
                    <span className="own">{r.tenant_name} · {r.created_at}</span>
                  </div>
                  {r.content && <div className="record-body">{r.content}</div>}
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="tabs">
          {TABS.map((item) => (
            <button
              key={item.key}
              className={`tab ${tab === item.key ? 'active' : ''}`}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>

        {tab === 'booking' ? (
          <>
            <div className="section-title">已被预约的时段</div>
            {booked.length === 0 ? (
              <p className="desc">暂无预约，随时可约。</p>
            ) : (
              <ul className="booked-list">
                {booked.map((item) => (
                  <li key={item.id}>
                    {item.viewing_time.slice(0, 16)} ·{' '}
                    {item.status === 'confirmed' ? '已确认' : '待确认'}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <>
            <div className="section-title">该房源的报修记录</div>
            {repairs.length === 0 ? (
              <p className="desc">暂无报修记录。</p>
            ) : (
              <ul className="booked-list">
                {repairs.map((item) => (
                  <li key={item.id}>
                    <span className={`pill ${item.urgency === '紧急' ? 'urgent' : ''}`}>
                      {item.urgency || '未分类'}
                    </span>{' '}
                    {item.category} · {item.content.slice(0, 18)}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        <div className="section-title">
          {tab === 'booking' ? '智能预约看房' : '智能报修'}
        </div>

        <form onSubmit={submit}>
          <div className="field">
            <input
              placeholder="你的姓名"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <input
              placeholder="手机号"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </div>
          <div className="field">
            <input
              placeholder={
                tab === 'booking'
                  ? '直接说时间，例如「这周六下午两点」'
                  : '描述故障，例如「卫生间水管漏水」'
              }
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn" type="submit" disabled={busy || !text.trim()}>
              {busy ? '处理中' : tab === 'booking' ? '预约' : '提交'}
            </button>
          </div>
        </form>

        <div className="examples">
          <span>试试：</span>
          {samples.map((s) => (
            <button key={s} className="chip-btn" onClick={() => setText(s)}>
              {s}
            </button>
          ))}
        </div>

        {reply && (
          <div className={`reply ${REPLY_STYLE[reply.status] || 'failed'}`}>
            {reply.message}
            {reply.category && (
              <div className="chips" style={{ marginTop: 9 }}>
                <span className="chip">类型：{reply.category}</span>
                <span className={`chip ${reply.urgency === '紧急' ? 'feature' : ''}`}>
                  紧急程度：{reply.urgency}
                </span>
                <span className="chip">派单：{reply.assignee}</span>
              </div>
            )}
            {reply.alternatives?.length > 0 && (
              <div className="slot-row">
                {reply.alternatives.map((iso) => (
                  <button key={iso} className="chip-btn" onClick={() => pickSlot(iso)}>
                    {iso.slice(5, 16).replace('T', ' ')}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 报修提交成功后才让传图：照片要挂到工单上，没有工单号无处可挂。
            登录用户才有权限，未登录时不显示以免点了才报错。 */}
        {tab === 'repair' && reply?.repair_id && user && (
          <ImageUploader mode="repair" repairId={reply.repair_id} />
        )}
      </aside>
    </>
  )
}
