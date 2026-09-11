import { useEffect, useState } from 'react'
import { api } from '../api'
import CloseRepairDialog from './CloseRepairDialog'
import ContractCard from './ContractCard'
import ContractDetail from './ContractDetail'
import DelegationPanel from './DelegationPanel'
import ImageUploader from './ImageUploader'
import SignContractDialog from './SignContractDialog'

const HOUSE_STATUS = {
  available: { label: '在租中', cls: 'status-confirmed' },
  rented: { label: '已出租', cls: 'status-pending' },
  offline: { label: '已下架', cls: 'status-cancelled' },
  pending: { label: '待审核', cls: 'status-pending' },
}

const REPAIR_STATUS = {
  pending: { label: '待处理', cls: 'status-pending' },
  processing: { label: '处理中', cls: 'status-pending' },
  done: { label: '已完成', cls: 'status-confirmed' },
}

const EMPTY_FORM = {
  title: '', district: '中山区', community: '', address: '',
  rent_price: '', deposit: '', area: '', layout: '两室一厅',
  rent_type: '整租', orientation: '朝南', description: '',
  // A5：楼栋、单元、第几层、共几层各占一格。
  // 原先是一个自由文本「如 11/33层」，什么格式都填得进来，
  // 既筛不了，又会和按层数算出来的「低楼层」并排出现
  building: '', unit: '', floor_no: '', floor_total: '',
  tags: [],
}

/** E2：标题按「区域 + 小区 + 户型 + 面积」拼。
 *
 * **这几样表单里都已经填过了**，再让房东自己组一遍标题，
 * 得到的多半是「精装修拎包入住！！」这种——对搜索和比较毫无帮助，
 * 而且每个人的写法都不一样，列表页看上去就很乱。
 *
 * 仍然允许改：拼出来的是默认值不是锁死的值。有人确实想突出
 * 「近地铁 5 号线」，那是他比我们更了解这套房。
 */
function autoTitle(form) {
  const parts = []
  if (form.district) parts.push(form.district + (form.community || ''))
  else if (form.community) parts.push(form.community)
  if (form.layout) parts.push(form.layout)
  const area = Number(form.area)
  if (area > 0) parts.push(Math.round(area) + '平')
  return parts.join(' ')
}

const DISTRICTS = ['中山区', '西岗区', '沙河口区', '甘井子区', '旅顺口区', '金州区']
const LAYOUTS = ['一室', '一室一厅', '两室一厅', '两室两厅', '三室一厅', '三室两厅']
// 朝向。表单原先没有这一栏，却在提交时默认写「朝南」——
// 等于替房东填了他没填的数据。补成可选。
const ORIENTATIONS = ['朝南', '朝东', '朝西', '朝北',
  '东南', '西南', '东北', '西北', '南北通透']

const RENT_TYPES = ['整租', '合租', '公寓']
const ALL_TAGS = ['电梯', '地铁', '近学校', '近医院', '有阳台', '可做饭',
  '有家具', '可养宠物', '采光好']

export default function LandlordPanel({ user, onClose, onOpenHouse, embedded,
                                       onSections, activeTab, onTabChange }) {
  // 嵌入整页工作台时，当前页签由外面的左栏说了算；独立抽屉时自己管。
  // **两套状态不能并存**——并存就会出现「左栏高亮 A、正文显示 B」，
  // 而这种不一致在切换几次之后才显形，最难查
  const [ownTab, setOwnTab] = useState('dashboard')
  const tab = embedded && activeTab ? activeTab : ownTab
  const setTab = (key) => {
    if (embedded && onTabChange) onTabChange(key)
    else setOwnTab(key)
  }
  const [dash, setDash] = useState(null)
  const [houses, setHouses] = useState([])
  const [repairs, setRepairs] = useState([])
  const [tenants, setTenants] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [editing, setEditing] = useState('')
  // L-10 定价建议 / AI-08 描述生成，两者都只在发布表单里用
  const [priceTip, setPriceTip] = useState(null)
  const [descBusy, setDescBusy] = useState(false)
  // 正在登记结案的工单。走对话框而不是直接改状态，
  // 否则人、物、工时一概不记，维修统计会漏算
  const [closing, setClosing] = useState(null)
  // 出租合同。原先房东只能在「个人中心」看到自己的合同，
  // 而签约根本没有界面入口——唯一办法是去调接口文档。
  // 经营动作该集中在工作台，所以搬到这里并补上发起签约。
  const [contracts, setContracts] = useState([])
  const [signing, setSigning] = useState(false)
  const [detailNo, setDetailNo] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')

  // 合同列表里只有 house_no，抽屉要完整对象，点开时先取一次详情
  async function openHouseByNo(houseNo) {
    if (!houseNo || !onOpenHouse) return
    try {
      onOpenHouse(await api.getHouse(houseNo))
    } catch {
      setError('这套房源已下架或不存在，看不到详情了。')
    }
  }

  async function load() {
    setError('')
    try {
      const [d, h, r, t, c] = await Promise.all([
        api.landlordDashboard(),
        api.landlordHouses(),
        // D3：工作台一律只问「我作为房东」的那一份。
        // 他自己在别处租的房、他自己报的修，都归个人中心
        api.listRepairs({ side: 'landlord' }),
        api.landlordTenants(),
        api.myContracts('', 'landlord'),
      ])
      setDash(d)
      setHouses(h.list || [])
      setRepairs(r.list || [])
      setTenants(t.list || [])
      setContracts(c.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function run(key, fn, message = '') {
    if (busy) return
    setBusy(key)
    setError('')
    setOk('')
    try {
      await fn()
      if (message) setOk(message)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  function toggleTag(tag) {
    setForm({
      ...form,
      tags: form.tags.includes(tag)
        ? form.tags.filter((t) => t !== tag)
        : [...form.tags, tag],
    })
  }

  function startEdit(house) {
    setEditing(house.house_no)
    setForm({
      title: house.title, district: house.district, community: house.community,
      address: house.address, rent_price: house.rent_price,
      deposit: house.deposit, area: house.area, layout: house.layout,
      rent_type: house.rent_type,
      building: house.building || '', unit: house.unit || '',
      floor_no: house.floor_no || '', floor_total: house.floor_total || '',
      orientation: house.orientation, description: house.description,
      tags: house.tags || [],
    })
    setTab('publish')
  }

  function submitHouse(e) {
    e.preventDefault()
    const payload = {
      ...form,
      // 标题没填就现拼一个。**校验之前拼**——否则「标题必填」会拦下
      // 一个我们本来就能替他填好的字段
      title: (form.title || '').trim() || autoTitle(form),
      rent_price: Number(form.rent_price),
      deposit: Number(form.deposit || form.rent_price),
      area: Number(form.area),
      // 空字符串要转成 undefined 而不是 0：0 层不是一个合法楼层，
      // 传 0 上去会被 schema 的 ge=1 拒掉，而房东只是没填
      floor_no: form.floor_no ? Number(form.floor_no) : undefined,
      floor_total: form.floor_total ? Number(form.floor_total) : undefined,
    }
    if (!payload.rent_price || !payload.area) {
      setError('租金和面积必须填写')
      return
    }
    if (!payload.title) {
      setError('请填写区域与小区，标题会自动生成')
      return
    }

    if (editing) {
      run('form', () => api.updateHouse(editing, payload),
        '已保存。修改关键信息后房源需要重新审核。')
        .then(() => { setEditing(''); setForm(EMPTY_FORM) })
    } else {
      run('form', () => api.createHouse(payload),
        '已提交，等待管理员审核通过后上架。')
        .then(() => setForm(EMPTY_FORM))
    }
  }

  // 柱状图的基准值。至少取 1，否则全为 0 时会除出 NaN
  const maxIncome = Math.max(
    1, ...(dash?.monthly_income || []).map((m) => m.amount))

  // 面积和户型齐了才问得出定价——先填面积再折算是这个功能的前提
  async function askPrice() {
    if (!form.area) {
      setPriceTip({ ok: false, message: '先填面积，才能给出定价建议。' })
      return
    }
    try {
      setPriceTip(await api.pricingSuggest({
        district: form.district, layout: form.layout, area: Number(form.area),
      }))
    } catch (err) {
      setPriceTip({ ok: false, message: err.message })
    }
  }

  async function genDescription() {
    setDescBusy(true)
    try {
      const r = await api.agentDescribe({
        district: form.district, community: form.community,
        layout: form.layout, area: Number(form.area) || 0,
        rent_price: Number(form.rent_price) || 0,
        rent_type: form.rent_type,
        floor: form.floor_no && form.floor_total
          ? `${form.floor_no}/${form.floor_total}层` : '',
        orientation: form.orientation, tags: form.tags,
      })
      if (r.ok) {
        setForm((f) => ({ ...f, description: r.text }))
        setError('')
      } else {
        setError(r.message)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setDescBusy(false)
    }
  }

  const TABS = [
    { key: 'dashboard', label: '工作台' },
    { key: 'houses', label: '我的房源', n: houses.length },
    { key: 'repairs', label: '工单', n: repairs.filter((r) => r.status !== 'done').length },
    { key: 'contracts', label: '出租合同',
      n: contracts.filter((c) => c.status === 'pending').length },
    { key: 'tenants', label: '租客', n: tenants.length },
    { key: 'delegations', label: '委托中介' },
    { key: 'publish', label: editing ? '编辑房源' : '发布房源' },
  ]

      {/* 嵌入模式：工作台整页把左栏和标题接管过去了，这里只出正文。
          **没有把三个面板重写一遍**——每个页签下的内容都已经调好、
          也都有人在用，重写只会得到两套各自漂移的逻辑。
          抽屉那层壳（遮罩、关闭键、标题、横向页签）是唯一要脱掉的东西。 */}
  // 把页签清单（含计数）报给外面的左栏。
  //
  // **必须走 useEffect，不能在渲染里直接调**：那是在渲染这个组件的
  // 过程中去改父组件的状态，React 会直接报错。依赖用序列化后的字符串，
  // 数组每次都是新对象，按引用比会无限重渲染。
  const sectionsKey = JSON.stringify(TABS.map((t) => [t.key, t.label, t.n || 0]))
  useEffect(() => {
    if (embedded && onSections) onSections(TABS)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [embedded, sectionsKey])

  return (
    <>
      {!embedded && <div className="drawer-mask" onClick={onClose} />}
      <div className={embedded ? 'wb-embed' : 'drawer wide'}>
        {!embedded && (
          <>
            <button className="drawer-close" onClick={onClose} aria-label="关闭">
              ×
            </button>
            <h3>房东工作台</h3>
            <div className="loc">{user.real_name || user.username}</div>

            <div className="tabs scroll">
              {TABS.map((item) => (
                <button
                  key={item.key}
                  className={`tab ${tab === item.key ? 'active' : ''}`}
                  onClick={() => {
                    setTab(item.key)
                    setOk('')
                    setError('')
                    if (item.key !== 'publish') { setEditing(''); setForm(EMPTY_FORM) }
                  }}
                >
                  {item.label}
                  {item.n > 0 && <span className="tab-n">{item.n}</span>}
                </button>
              ))}
            </div>
          </>
        )}

        {error && <div className="reply failed">{error}</div>}
        {ok && <div className="reply created">{ok}</div>}

        {/* ---------------- 工作台 ---------------- */}
        {tab === 'dashboard' && dash && (
          <>
            <div className="dash-grid">
              <div className="dcell">
                <div className="dk">在租</div>
                <div className="dv">{dash.house_rented}</div>
                <div className="dd">出租率 {dash.occupancy_rate}%</div>
              </div>
              <div className="dcell">
                <div className="dk">空置</div>
                <div className="dv">{dash.house_vacant}</div>
                <div className="dd">共 {dash.house_total} 套</div>
              </div>
              <div className="dcell money">
                <div className="dk">本月收租</div>
                <div className="dv">{dash.month_income}</div>
                <div className="dd">元</div>
              </div>
              <div className={`dcell ${dash.todo.total ? 'warn' : ''}`}>
                <div className="dk">待处理</div>
                <div className="dv">{dash.todo.total}</div>
                <div className="dd">条</div>
              </div>
            </div>

            {dash.monthly_income?.length > 0 && (
              <>
                <div className="section-title">近半年收租</div>
                <div className="bar-chart">
                  {dash.monthly_income.map((m) => (
                    <div className="bar-item" key={m.month}>
                      <div className="bar-val">{Math.round(m.amount)}</div>
                      <div
                        className="bar-fill"
                        style={{
                          // 收入为 0 的月份也留一点高度，
                          // 否则那一格看上去像是数据缺失而不是没收到钱
                          height: `${Math.max((m.amount / maxIncome) * 100, 4)}%`,
                        }}
                      />
                      <div className="bar-lbl">{m.month.slice(5)}月</div>
                    </div>
                  ))}
                </div>
              </>
            )}

            <div className="section-title">待办事项</div>
            {dash.todo.total === 0 ? (
              <p className="desc">暂无待办，一切正常。</p>
            ) : (
              <ul className="todo-list">
                {dash.todo.appointments > 0 && (
                  <li>
                    <span className="dot-mark warn" />
                    {dash.todo.appointments} 条看房预约待确认
                  </li>
                )}
                {dash.todo.urgent_repairs > 0 && (
                  <li>
                    <span className="dot-mark danger" />
                    {dash.todo.urgent_repairs} 条<strong>紧急报修</strong>待处理
                  </li>
                )}
                {dash.todo.repairs - dash.todo.urgent_repairs > 0 && (
                  <li>
                    <span className="dot-mark" />
                    {dash.todo.repairs - dash.todo.urgent_repairs} 条一般报修待处理
                  </li>
                )}
                {dash.todo.expiring_contracts > 0 && (
                  <li>
                    <span className="dot-mark warn" />
                    {dash.todo.expiring_contracts} 份租约 30 天内到期
                  </li>
                )}
              </ul>
            )}

            <div className="section-title">收租情况</div>
            <div className="sum-row">
              <div className="sum">
                <div className="sum-k">待收租金</div>
                <div className="sum-v money">{dash.income_pending}</div>
                <div className="sum-d">元</div>
              </div>
              <div className="sum">
                <div className="sum-k">逾期</div>
                <div className={`sum-v ${dash.overdue_count ? 'danger' : ''}`}>
                  {dash.overdue_count}
                </div>
                <div className="sum-d">笔</div>
              </div>
            </div>
          </>
        )}

        {/* ---------------- 我的房源 ---------------- */}
        {tab === 'houses' &&
          (houses.length === 0 ? (
            <p className="desc">还没有发布房源，切到「发布房源」开始。</p>
          ) : (
            <ul className="record-list">
              {houses.map((house) => {
                const st = HOUSE_STATUS[house.status] || { label: house.status, cls: '' }
                return (
                  <li key={house.house_no}>
                    <div className="record-head">
                      <strong>{house.title}</strong>
                      <span className={`pill ${st.cls}`}>{st.label}</span>
                    </div>
                    <div className="record-body">
                      {house.house_no} · <span className="amount">{house.rent_price} 元</span>
                      {' '}· {house.layout} · {Math.round(house.area)}㎡ · 浏览 {house.view_count}
                    </div>
                    <div className="record-actions">
                      <button className="btn ghost small" onClick={() => startEdit(house)}>
                        编辑
                      </button>
                      {house.status === 'available' && (
                        <button
                          className="btn ghost small"
                          onClick={() => run(house.house_no,
                            () => api.setHouseStatus(house.house_no, 'offline'))}
                        >
                          下架
                        </button>
                      )}
                      {house.status === 'offline' && (
                        <button
                          className="btn small"
                          onClick={() => run(house.house_no,
                            () => api.setHouseStatus(house.house_no, 'available'))}
                        >
                          上架
                        </button>
                      )}
                      {house.status === 'pending' && (
                        <span className="own">等待管理员审核</span>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          ))}

        {/* ---------------- 工单处理 ---------------- */}
        {tab === 'repairs' &&
          (repairs.length === 0 ? (
            <p className="desc">名下房源暂无报修工单。</p>
          ) : (
            <ul className="record-list">
              {repairs.map((item) => {
                const st = REPAIR_STATUS[item.status] || { label: item.status, cls: '' }
                return (
                  <li key={item.id}>
                    <div className="record-head">
                      <strong>{item.house_no}</strong>
                      {item.urgency === '紧急' && <span className="pill urgent">紧急</span>}
                      <span className={`pill ${st.cls}`}>{st.label}</span>
                    </div>
                    <div className="record-body">
                      {item.category && `${item.category} · `}{item.content}
                    </div>
                    <div className="record-body">
                      {item.contact_name} {item.contact_phone} · {item.created_at}
                      {item.assignee && ` · 建议派单：${item.assignee}`}
                    </div>
                    {/* 超时标记（C-4）。原先判定为紧急之后就没有下文了，
                        没人处理也不会催 */}
                    {item.overdue && (
                      <div className="record-body overdue-note">
                        已挂 {item.waited_hours} 小时，超过 {item.sla_hours} 小时时限
                      </div>
                    )}
                    {item.analysis && (
                      <div className="record-body ai-note">{item.analysis}</div>
                    )}
                    {item.status !== 'done' && (
                      <div className="record-actions">
                        {item.status === 'pending' && (
                          <button
                            className="btn ghost small"
                            disabled={busy === `r${item.id}`}
                            onClick={() => run(`r${item.id}`,
                              () => api.updateRepair(item.id, 'processing'))}
                          >
                            开始处理
                          </button>
                        )}
                        <button
                          className="btn small"
                          onClick={() => setClosing(item)}
                        >
                          完成并登记
                        </button>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          ))}

        {/* ---------------- 租客档案 ---------------- */}
        {tab === 'contracts' && (
          <>
            <div className="record-actions" style={{ marginBottom: 12 }}>
              <button className="btn small" onClick={() => setSigning(true)}>
                发起签约
              </button>
            </div>

            {contracts.length === 0 ? (
              <p className="desc">
                还没有出租合同。看房满意后可以为租客发起签约，
                合同需要租客确认才生效。
              </p>
            ) : (
              <ul className="record-list">
                {contracts.map((contract) => (
                  <ContractCard
                    onOpenHouse={openHouseByNo}
                    onOpenDetail={setDetailNo}
                    key={contract.contract_no}
                    contract={contract}
                    role={user.role}
                    onChanged={load}
                  />
                ))}
              </ul>
            )}
          </>
        )}

        {tab === 'tenants' &&
          (tenants.length === 0 ? (
            <p className="desc">还没有签约租客</p>
          ) : (
            <ul className="record-list">
              {tenants.map((t) => (
                <li key={t.id} className={t.overdue_count ? 'unread' : ''}>
                  <div className="record-head">
                    <strong>{t.real_name || t.username}</strong>
                    {t.overdue_count > 0 && (
                      <span className="pill urgent">逾期 {t.overdue_count} 笔</span>
                    )}
                    <span className="pill">{t.active_count} 份在租</span>
                  </div>
                  <div className="record-body">
                    {t.phone} · 房源 {t.houses.join('、') || '—'}
                  </div>
                  <div className="record-body">
                    已收 <span className="amount">{t.paid_total} 元</span> ·
                    待收 <span className="amount">{t.unpaid_total} 元</span> ·
                    累计 {t.contract_count} 份合同
                  </div>
                </li>
              ))}
            </ul>
          ))}

        {/* ---------------- 发布 / 编辑 ---------------- */}
        {/* ---------------- 委托中介（J-04） ---------------- */}
        {tab === 'delegations' && <DelegationPanel houses={houses} />}

        {tab === 'publish' && (
          <form onSubmit={submitHouse} className="house-form">
            {editing && (
              <p className="desc">
                正在编辑 {editing}。<strong>修改后房源会退回待审核状态。</strong>
              </p>
            )}

            {/* E2：标题不再要求手填。placeholder 里实时显示会拼成什么，
                房东一眼看得到结果，想改再改——**比一个空框加一句
                「如：……」有用得多**，后者他还得自己套格式。 */}
            <label className="fld">
              <span>标题</span>
              <input value={form.title} onChange={set('title')}
                placeholder={autoTitle(form) || '填完区域、小区、户型和面积后自动生成'} />
              <small className="fld-hint">
                留空就按「区域 + 小区 + 户型 + 面积」自动生成，想突出别的再自己写
              </small>
            </label>

            <div className="fld-row">
              <label className="fld">
                <span>区域</span>
                <select value={form.district} onChange={set('district')}>
                  {DISTRICTS.map((d) => <option key={d}>{d}</option>)}
                </select>
              </label>
              <label className="fld">
                <span>小区</span>
                <input value={form.community} onChange={set('community')} />
              </label>
            </div>

            <label className="fld">
              <span>详细地址</span>
              <input value={form.address} onChange={set('address')} />
            </label>

            {priceTip && (
              <div className={`assist ${priceTip.ok ? '' : 'warn'}`}>
                <div className="assist-body">
                  {priceTip.message}
                  {priceTip.ok && (
                    <div className="assist-hint">
                      这里用中位单价而不是平均值，避免个别定价离谱的房源拉偏结果。
                    </div>
                  )}
                </div>
                {priceTip.ok && (
                  <button
                    type="button"
                    className="btn ghost small"
                    onClick={() => setForm({
                      ...form, rent_price: String(priceTip.median),
                    })}
                  >
                    用 {priceTip.median} 元
                  </button>
                )}
              </div>
            )}

            <div className="fld-row">
              <label className="fld">
                <span>月租金（元）</span>
                <input type="number" value={form.rent_price} onChange={set('rent_price')} />
                <button type="button" className="btn ghost small"
                        onClick={askPrice}>
                  看看同类房源定价
                </button>
              </label>
              <label className="fld">
                <span>押金（元）</span>
                <input type="number" value={form.deposit} onChange={set('deposit')}
                  placeholder="留空同租金" />
              </label>
            </div>

            <div className="fld-row">
              <label className="fld">
                <span>面积（㎡）</span>
                <input type="number" value={form.area} onChange={set('area')} />
              </label>
              <label className="fld">
                <span>户型</span>
                <select value={form.layout} onChange={set('layout')}>
                  {LAYOUTS.map((l) => <option key={l}>{l}</option>)}
                </select>
              </label>
            </div>

            <div className="fld-row">
              <label className="fld">
                <span>出租方式</span>
                <select value={form.rent_type} onChange={set('rent_type')}>
                  {RENT_TYPES.map((t) => <option key={t}>{t}</option>)}
                </select>
              </label>
              <label className="fld">
                <span>楼栋</span>
                <input value={form.building} onChange={set('building')}
                       placeholder="如 12号楼" />
              </label>
            </div>

            {/* A5：楼栋、单元、楼层各占一格。
                原先是一个自由文本框「如 11/33层」——什么格式都填得进来
                （「11层」「十一楼」），筛选用不上，而且卡片上会和按层数
                算出来的「低楼层」并排出现，同一套房两个楼层说法。
                带看时要报「几号楼几单元」，从一行自由文本里也取不出来。 */}
            <div className="fld-row">
              <label className="fld">
                <span>单元</span>
                <input value={form.unit} onChange={set('unit')}
                       placeholder="如 3单元" />
              </label>
              <label className="fld">
                <span>所在楼层</span>
                <input value={form.floor_no} onChange={set('floor_no')}
                       inputMode="numeric" placeholder="第几层" />
              </label>
              <label className="fld">
                <span>总楼层</span>
                <input value={form.floor_total} onChange={set('floor_total')}
                       inputMode="numeric" placeholder="共几层" />
              </label>
            </div>

            <label className="fld">
              <span>朝向</span>
              <select value={form.orientation} onChange={set('orientation')}>
                {ORIENTATIONS.map((o) => <option key={o}>{o}</option>)}
              </select>
            </label>

            <label className="fld">
              <span>房源特色</span>
              <div className="tag-pick">
                {ALL_TAGS.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    className={`chip-btn ${form.tags.includes(tag) ? 'on' : ''}`}
                    onClick={() => toggleTag(tag)}
                  >
                    {tag}
                  </button>
                ))}
              </div>
            </label>

            <label className="fld">
              <span>房源描述</span>
              <textarea rows={5} value={form.description} onChange={set('description')} />
            </label>

            <div className="assist">
              <div className="assist-body">
                写不出来？智能体可以按你已填的字段生成一段。
                <div className="assist-hint">
                  只组织语言，<b>不添加任何字段里没有的信息</b>——
                  写了「步行五分钟到地铁」而实际没有，那是虚假宣传。
                  生成后可以自己改。
                </div>
              </div>
              <button type="button" className="btn ghost small"
                      disabled={descBusy} onClick={genDescription}>
                {descBusy ? '生成中…' : '帮我写一段'}
              </button>
            </div>

            <button className="btn block" type="submit" disabled={busy === 'form'}>
              {busy === 'form' ? '提交中…' : editing ? '保存修改' : '提交发布'}
            </button>
          </form>
        )}

        {/* 相册管理只在编辑已有房源时出现：图片要挂到某个 house_no 上，
            新房源还没提交，没有编号可挂。 */}
        {tab === 'publish' && editing && (
          <ImageUploader mode="house" houseNo={editing} onChanged={load} />
        )}

        {tab === 'publish' && !editing && (
          <p className="desc">
            房源图片在发布后上传：到「我的房源」点「编辑」。图片需挂在房源编号上，新房源还没有编号。
          </p>
        )}
      </div>

      {detailNo && (
        <ContractDetail
          contractNo={detailNo}
          onClose={() => setDetailNo('')}
          onOpenHouse={(no) => {
            setDetailNo('')
            openHouseByNo(no)
          }}
        />
      )}

      {signing && (
        <SignContractDialog
          houses={houses}
          onClose={() => setSigning(false)}
          onDone={() => {
            setSigning(false)
            setOk('合同已生成，等待租客确认后生效。')
            load()
          }}
        />
      )}

      {closing && (
        <CloseRepairDialog
          repair={closing}
          onClose={() => setClosing(null)}
          onDone={() => {
            setClosing(null)
            setOk('已完成并登记维修记录，租客会收到通知。')
            load()
          }}
        />
      )}
    </>
  )
}
