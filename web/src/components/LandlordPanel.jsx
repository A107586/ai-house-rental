import { useEffect, useState } from 'react'
import { api } from '../api'
import ImageUploader from './ImageUploader'

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
  rent_type: '整租', floor: '', orientation: '朝南', description: '',
  tags: [],
}

const DISTRICTS = ['中山区', '西岗区', '沙河口区', '甘井子区', '旅顺口区', '金州区']
const LAYOUTS = ['一室', '一室一厅', '两室一厅', '两室两厅', '三室一厅', '三室两厅']
const RENT_TYPES = ['整租', '合租', '公寓']
const ALL_TAGS = ['电梯', '地铁', '近学校', '近医院', '有阳台', '可做饭',
  '有家具', '可养宠物', '采光好']

export default function LandlordPanel({ user, onClose }) {
  const [tab, setTab] = useState('dashboard')
  const [dash, setDash] = useState(null)
  const [houses, setHouses] = useState([])
  const [repairs, setRepairs] = useState([])
  const [tenants, setTenants] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [editing, setEditing] = useState('')
  // L-10 定价建议 / AI-08 描述生成，两者都只在发布表单里用
  const [priceTip, setPriceTip] = useState(null)
  const [descBusy, setDescBusy] = useState(false)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    setError('')
    try {
      const [d, h, r, t] = await Promise.all([
        api.landlordDashboard(),
        api.landlordHouses(),
        api.listRepairs(),
        api.landlordTenants(),
      ])
      setDash(d)
      setHouses(h.list || [])
      setRepairs(r.list || [])
      setTenants(t.list || [])
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
      rent_type: house.rent_type, floor: house.floor,
      orientation: house.orientation, description: house.description,
      tags: house.tags || [],
    })
    setTab('publish')
  }

  function submitHouse(e) {
    e.preventDefault()
    const payload = {
      ...form,
      rent_price: Number(form.rent_price),
      deposit: Number(form.deposit || form.rent_price),
      area: Number(form.area),
    }
    if (!payload.title || !payload.rent_price || !payload.area) {
      setError('标题、租金、面积必须填写')
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
        rent_type: form.rent_type, floor: form.floor,
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
    { key: 'tenants', label: '租客', n: tenants.length },
    { key: 'publish', label: editing ? '编辑房源' : '发布房源' },
  ]

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer wide">
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
                          disabled={busy === `r${item.id}`}
                          onClick={() => run(`r${item.id}`,
                            () => api.updateRepair(item.id, 'done'),
                            '已标记完成并通知租客。')}
                        >
                          标记完成
                        </button>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          ))}

        {/* ---------------- 租客档案 ---------------- */}
        {tab === 'tenants' &&
          (tenants.length === 0 ? (
            <p className="desc">还没有签约租客。</p>
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
        {tab === 'publish' && (
          <form onSubmit={submitHouse} className="house-form">
            {editing && (
              <p className="desc">
                正在编辑 {editing}。<strong>修改后房源会退回待审核状态。</strong>
              </p>
            )}

            <label className="fld">
              <span>标题</span>
              <input value={form.title} onChange={set('title')}
                placeholder="如：中山区桂林街 两室一厅 62平" />
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
                      用的是中位单价而不是平均值——一套定价离谱的房源
                      就能把平均值拉歪。
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
                <span>楼层</span>
                <input value={form.floor} onChange={set('floor')} placeholder="如 11/33层" />
              </label>
            </div>

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
            房源图片在提交发布之后、到「我的房源」里点「编辑」来上传——
            图片要挂到房源编号上，新房源还没有编号。
          </p>
        )}
      </aside>
    </>
  )
}
