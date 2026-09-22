import { useEffect, useState } from 'react'
import ImageUploader from './ImageUploader'
import { FeeTable, VerifyBar } from './VerifyBadge'
import ReportDialog from './ReportDialog'
import HousePhoto from './HousePhoto'
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

export default function HouseDrawer({ house, user, favorited, onToggleFavorite,
                                     onClose, onOpenHouse, onTalk,
                                     onAskAssistant, demandText = '' }) {
  const [tab, setTab] = useState('booking')
  // K-08 举报弹窗。入口放在详情页而不是个人中心——
  // 用户是在看这套房、觉得不对劲的那一刻才想举报的
  const [reporting, setReporting] = useState(false)
  const [booked, setBooked] = useState([])
  const [repairs, setRepairs] = useState([])
  const [reviews, setReviews] = useState({ list: [], average: 0, total: 0 })
  const [text, setText] = useState('')
  const [name, setName] = useState(user?.real_name || '')
  const [phone, setPhone] = useState('')
  const [reply, setReply] = useState(null)
  const [busy, setBusy] = useState(false)
  // 报修照片：已经传到本系统的地址。**在提交之前就带上**，
  // 让模型先看一眼再定故障类型——照片挂到工单上是提交之后的事，
  // 那时候分派早就定完了，看不看都一样。
  const [repairPhotos, setRepairPhotos] = useState([])
  const [picking, setPicking] = useState('')
  const [shot, setShot] = useState(0)
  // A-5：房东概要与相似房源。两者都是「锦上添花」，
  // 取不到就不显示，不能拖累详情页本身
  const [landlord, setLandlord] = useState(null)
  const [similar, setSimilar] = useState([])
  const [talking, setTalking] = useState(false)

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

    // 这两块失败就静默略过——房东概要拉不到不该让整个详情页报错
    api.houseLandlord(house.house_no).then(setLandlord).catch(() => setLandlord(null))
    api.similarHouses(house.house_no)
      .then((d) => setSimilar(d.list || []))
      .catch(() => setSimilar([]))

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
    // 报修允许只给照片不写字——「拍一张」正是这个功能存在的理由。
    // 预约仍然必须有内容：没有时间，预约无从谈起。
    if (busy) return
    if (!content && !(tab === 'repair' && repairPhotos.length)) return

    if (tab === 'booking' && !user) {
      setReply({ status: 'error', message: '请先登录后再预约看房', alternatives: [] })
      return
    }
    if (tab === 'booking' && !/^1[3-9]\d{9}$/.test(phone.trim())) {
      setReply({ status: 'error', message: '请输入正确的 11 位手机号', alternatives: [] })
      return
    }

    setBusy(true)
    try {
      const applicant = { name: name.trim(), phone: phone.trim() }
      const result =
        tab === 'booking'
          ? await api.agentBooking(content, house.house_no, applicant)
          : await api.agentRepair(content, house.house_no, applicant, true, repairPhotos)
      setReply(result)
      if (result.status === 'created') {
        setText('')
        setRepairPhotos([])
        refresh()
      }
    } catch (err) {
      setReply({ status: 'error', message: err.message, alternatives: [] })
    } finally {
      setBusy(false)
    }
  }

  async function pickPhotos(event) {
    const files = Array.from(event.target.files || [])
    // 清空 input，否则连续选同一个文件不会再触发 change
    event.target.value = ''
    if (!files.length) return
    setPicking('正在上传…')
    const urls = []
    for (const file of files.slice(0, 3 - repairPhotos.length)) {
      if (file.size > 5 * 1024 * 1024) {
        setPicking(`${file.name} 超过 5MB，已跳过`)
        continue
      }
      try {
        const { url } = await api.uploadImage(file)
        urls.push(url)
      } catch (err) {
        setPicking(err.message)
      }
    }
    setRepairPhotos((prev) => [...prev, ...urls].slice(0, 3))
    if (urls.length) setPicking('')
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
            <HousePhoto className="album-main" src={photos[shot]}
                        alt={house.title} lazy={false} />
            {photos.length > 1 && (
              <div className="album-thumbs">
                {photos.map((url, i) => (
                  <button
                    key={url}
                    className={`thumb ${i === shot ? 'on' : ''}`}
                    onClick={() => setShot(i)}
                    aria-label={`第 ${i + 1} 张`}
                  >
                    <HousePhoto src={url} alt="" placeholder="✕"
                                placeholderClassName="thumb-none" />
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <h3>{house.title}</h3>
        <div className="loc">
          {/* 房源编号要露出来。它是用户与客服、房东沟通时唯一能准确指认
              这套房的东西——「青泥洼桥那套」在同一个小区有好几套时说不清，
              报修、预约、投诉的记录里存的也都是这个编号。 */}
          <span className="house-no">{house.house_no}</span>
          {house.district} · {house.community} · {house.address}
        </div>

        {/* 核验条紧跟标题。租客要先判断「这房子是不是真的」，
            再看别的——放到页面底部等于没放（K-07） */}
        <VerifyBar house={house} />

        <div className="record-actions">
          <button
            className={`btn ghost small ${favorited ? 'active' : ''}`}
            onClick={() => onToggleFavorite(house.house_no)}
          >
            {favorited ? '★ 已收藏' : '☆ 收藏'}
          </button>

          {/* 咨询入口放在这里，是因为**用户正在看这套房时才会想问**。
              放个人中心里等他想起来去翻，等于没有 */}
          <button
            className="btn small"
            disabled={talking}
            onClick={async () => {
              if (!user) {
                setReply({ status: 'error', message: '登录后才能咨询房东',
                           alternatives: [] })
                return
              }
              setTalking(true)
              try {
                // J-09-a：先把最近那句需求整理成摘要，随咨询一并带过去。
                //
                // **整理失败不能挡住咨询。** 摘要是锦上添花的一段，
                // 智能体挂了就当没有——降级之后咨询照常发起，
                // 只是中介要多问一句（需求 N-07）。
                let summary = ''
                if (demandText && demandText.trim()) {
                  try {
                    const r = await api.brokerHandoff(demandText)
                    // 渲染好的文本由智能体给，前端不再实现一遍——
                    // 两边各写一份，迟早会不一样
                    if (r.ok) summary = r.summary_text || ''
                  } catch {
                    summary = ''
                  }
                }
                const data = await api.startConversation(house.house_no,
                                                         summary)
                onTalk && onTalk(data.conversation.id)
              } catch (err) {
                setReply({ status: 'error', message: err.message,
                           alternatives: [] })
              } finally {
                setTalking(false)
              }
            }}
          >
            {talking ? '打开中…' : '咨询房东'}
          </button>

          {/* 「问助手」和「咨询房东」是两件事，都要留：
              房东要等人回，助手立刻答，而且能直接帮他约看房、收藏。
              点进去会带上这套房——购物软件从商品页点客服也是这么做的，
              用户不该为了问一句「能养猫吗」先把房源编号背下来。 */}
          <button
            className="btn small ghost"
            onClick={() => onAskAssistant && onAskAssistant(house)}
          >
            问助手
          </button>

          {/* 举报入口常驻在详情页（K-08）。贝壳把它放在详情页那条
              「真实存在、真实在租、真实价格」承诺的旁边，是同一个道理：
              **承诺和申诉渠道要摆在一起**，只有承诺没有渠道就是空话。
              样式压到最轻——它是兜底通道，不该和「咨询房东」抢注意力 */}
          <button className="link report-link"
                  onClick={() => setReporting(true)}>
            举报该房源
          </button>
        </div>

        {/* ---------- 房东概要（A-5） ---------- */}
        {landlord && (
          <div className="ld-card">
            <div className="ld-head">
              <b>{landlord.name}</b>
              {landlord.verified && <span className="pill">已实名</span>}
              <span className="muted-note">{landlord.joined_at} 加入</span>
            </div>
            <div className="ld-facts">
              <span>在租 {landlord.listed_count} 套</span>
              {landlord.rented_count > 0 &&
                <span>已出租 {landlord.rented_count} 套</span>}
              {landlord.review_count > 0
                ? <span>评分 {landlord.rating}（{landlord.review_count} 条）</span>
                : <span className="muted">暂无评分</span>}
              {landlord.avg_repair_hours !== null &&
                <span>报修平均 {landlord.avg_repair_hours} 小时处理</span>}
            </div>

            {/* 租客写给这个房东的话。**分数说明不了「他哪里好」**——
                4.5 分和 4.2 分之间没什么可判断的，而「押金一周退到账」
                和「报修拖了三周」是完全不同的两个房东。 */}
            {landlord.comments?.length > 0 && (
              <ul className="ld-says">
                {landlord.comments.map((c, i) => (
                  <li key={i}>
                    <span className="ld-says-who">
                      {c.name} · {c.rating} 分 · {c.created_at}
                    </span>
                    {c.content}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

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
          {/* 楼层和朝向也一样：没填就不摆。
              一个空的「朝向：」看起来像数据丢了，而事实是房东没填 */}
          {house.floor && (
            <dl className="fact">
              <dt>楼层</dt>
              <dd>{house.floor}{house.floor_level ? `（${house.floor_level}）` : ''}</dd>
            </dl>
          )}
          {/* A5：楼栋与单元单独两栏。带看约在楼下时报的就是这两个数，
              埋在地址那一长串自由文本里等于没有 */}
          {house.building && (
            <dl className="fact">
              <dt>楼栋</dt>
              <dd>{house.building}</dd>
            </dl>
          )}
          {house.unit && (
            <dl className="fact">
              <dt>单元</dt>
              <dd>{house.unit}</dd>
            </dl>
          )}
          {house.orientation && (
            <dl className="fact">
              <dt>朝向</dt>
              <dd>{house.orientation}</dd>
            </dl>
          )}

          {/* K-04 新增的那些维度也要在详情页露出来。
              用户刚用「有电梯」筛完，点进详情却看不到这一条，
              等于让他自己再去信一次筛选结果。
              **没填的项直接不显示**，摆一个空的「装修：」比不摆更糟——
              它看起来像数据丢了 */}
          {house.elevator !== undefined && (
            <dl className="fact">
              <dt>电梯</dt>
              <dd>{house.elevator ? '有' : '无'}</dd>
            </dl>
          )}
          {house.decoration && (
            <dl className="fact">
              <dt>装修</dt>
              <dd>{house.decoration}</dd>
            </dl>
          )}
          {house.heating && (
            <dl className="fact">
              <dt>供暖</dt>
              <dd>{house.heating}</dd>
            </dl>
          )}
          {house.available_from && (
            <dl className="fact">
              <dt>可入住</dt>
              <dd>{house.available_from}</dd>
            </dl>
          )}
          {house.min_months > 0 && (
            <dl className="fact">
              <dt>最短租期</dt>
              <dd>{house.min_months} 个月</dd>
            </dl>
          )}
        </div>

        <FeeTable house={house} />

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

        {/* 一条评价都没有时也要有这一块。整块不画的话，详情页上
            「评价」这件事凭空消失，用户不知道是没人评过还是页面没加载出来。 */}
        {reviews.total === 0 && (
          <>
            <div className="section-title">租客评价</div>
            <p className="desc">
              还没有人住完并留下评价。签约前建议实地看看，把押金退还和维修责任写进合同。
            </p>
          </>
        )}

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
                  {/* C1：实拍图。**放在文字下面而不是上面**——
                      先读评价再看图，图是佐证不是标题 */}
                  {r.images?.length > 0 && (
                    <div className="review-shots">
                      {r.images.map((url) => (
                        <a href={url} target="_blank" rel="noreferrer" key={url}>
                          <img src={url} alt="租客实拍" loading="lazy" />
                        </a>
                      ))}
                    </div>
                  )}
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
            <div className="section-title">已约时段</div>
            {booked.length === 0 ? (
              <p className="desc">还没人预约，随时可约。</p>
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
              <p className="desc">报修记录</p>
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
              inputMode="numeric"
              maxLength={11}
            />
          </div>
          <div className="field">
            <input
              placeholder={
                tab === 'booking'
                  ? '直接说时间，比如「这周六下午两点」'
                  : '描述故障，比如「卫生间水管漏水」'
              }
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button
              className="btn"
              type="submit"
              disabled={busy || (!text.trim()
                                 && !(tab === 'repair' && repairPhotos.length > 0))}
            >
              {busy ? '处理中' : tab === 'booking' ? '预约' : '提交'}
            </button>
          </div>
        </form>

        {/* 报修才给拍照。**提交之前**传，照片要参与分派。
            现场拍一张，比让人把「水槽下面那根管子的接口在渗水」
            打出来容易得多，而描述越糊，派的工种越容易错。 */}
        {tab === 'repair' && user && (
          <div className="examples">
            <label className="chip-btn" style={{ cursor: 'pointer' }}>
              📷 拍张照（可选，最多 3 张）
              <input
                type="file"
                accept="image/*"
                multiple
                style={{ display: 'none' }}
                onChange={pickPhotos}
                disabled={busy || repairPhotos.length >= 3}
              />
            </label>
            {repairPhotos.map((url) => (
              <span key={url} className="chip">
                已选 1 张
                <button
                  className="chip-btn"
                  style={{ marginLeft: 6 }}
                  onClick={() => setRepairPhotos((p) => p.filter((x) => x !== url))}
                >
                  ×
                </button>
              </span>
            ))}
            {picking && <span className="chip">{picking}</span>}
          </div>
        )}

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
            {/* 照片经模型变成的那句话要摆在眼前。拿一个用户没看过的
                句子去派单，师傅上门发现不是这回事时谁也说不清。 */}
            {reply.vision && (
              <div className="chips" style={{ marginTop: 9 }}>
                <span className="chip">照片里我看到：{reply.vision}</span>
              </div>
            )}
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

        {/* ---------- 相似房源（A-5） ----------
            看完一套不满意就得退回列表重搜，是最没必要的一次往返。
            同区域 + 租金上下 20%，户型相同的排前面。 */}
        {similar.length > 0 && (
          <section className="cd-sec">
            <h5>看过这套的人，也在看</h5>
            <ul className="sim-list">
              {similar.map((item) => (
                <li key={item.house_no}>
                  <button
                    className="sim-item"
                    onClick={() => onOpenHouse && onOpenHouse(item)}
                  >
                    <HousePhoto src={item.cover_image} alt={item.title}
                                placeholder="暂无图"
                                placeholderClassName="sim-none" />
                    <span className="sim-body">
                      <span className="sim-title">{item.title}</span>
                      <span className="muted-note">
                        {item.layout} · {Math.round(item.area)} 平米
                      </span>
                      <span className="sim-price">
                        {Math.round(item.rent_price)} 元/月
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </aside>

      {reporting && (
        <ReportDialog
          house={house}
          user={user}
          onClose={() => setReporting(false)}
          onNeedLogin={() => {
            setReporting(false)
            setReply({ status: 'error', message: '登录后才能举报房源',
                       alternatives: [] })
          }}
        />
      )}
    </>
  )
}
