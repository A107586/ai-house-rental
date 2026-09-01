import { useEffect, useState } from 'react'
import { api } from '../api'

const HOUSE_STATUS = {
  available: { label: '在租中', cls: 'status-confirmed' },
  rented: { label: '已出租', cls: 'status-pending' },
  offline: { label: '已下架', cls: 'status-cancelled' },
  pending: { label: '待审核', cls: 'status-pending' },
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
  const [form, setForm] = useState(EMPTY_FORM)
  const [editing, setEditing] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    setError('')
    try {
      const [d, h] = await Promise.all([
        api.landlordDashboard(),
        api.landlordHouses(),
      ])
      setDash(d)
      setHouses(h.list || [])
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

  const TABS = [
    { key: 'dashboard', label: '工作台' },
    { key: 'houses', label: '我的房源', n: houses.length },
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

            <div className="fld-row">
              <label className="fld">
                <span>月租金（元）</span>
                <input type="number" value={form.rent_price} onChange={set('rent_price')} />
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
              <textarea rows={4} value={form.description} onChange={set('description')} />
            </label>

            <button className="btn block" type="submit" disabled={busy === 'form'}>
              {busy === 'form' ? '提交中…' : editing ? '保存修改' : '提交发布'}
            </button>
          </form>
        )}
      </aside>
    </>
  )
}
