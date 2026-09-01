import { useEffect, useState } from 'react'
import { api } from '../api'

const ROLE_LABEL = { tenant: '租客', landlord: '房东', admin: '管理员' }

export default function AdminPanel({ user, onClose }) {
  const [tab, setTab] = useState('dashboard')
  const [stats, setStats] = useState(null)
  const [pending, setPending] = useState([])
  const [users, setUsers] = useState([])
  const [keyword, setKeyword] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    setError('')
    try {
      const [s, p, u] = await Promise.all([
        api.stats(),
        api.adminHouses({ status: 'pending' }),
        api.adminUsers({ keyword }),
      ])
      setStats(s)
      setPending(p.list || [])
      setUsers(u.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  function reject(houseNo) {
    const reason = window.prompt('驳回理由（会通知房东）', '房源描述与实际不符')
    if (reason === null) return
    run(houseNo, () => api.reviewHouse(houseNo, false, reason), '已驳回并通知房东。')
  }

  // 月度收入柱状图的最大值，用于算每根柱子的高度
  const maxIncome = Math.max(
    1, ...(stats?.monthly_income || []).map((m) => m.amount))

  const TABS = [
    { key: 'dashboard', label: '数据看板' },
    { key: 'review', label: '房源审核', n: pending.length },
    { key: 'users', label: '用户管理', n: users.length },
  ]

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer wide">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <h3>管理后台</h3>
        <div className="loc">{user.real_name || user.username} · 管理员</div>

        <div className="tabs scroll">
          {TABS.map((item) => (
            <button
              key={item.key}
              className={`tab ${tab === item.key ? 'active' : ''}`}
              onClick={() => { setTab(item.key); setOk(''); setError('') }}
            >
              {item.label}
              {item.n > 0 && <span className="tab-n">{item.n}</span>}
            </button>
          ))}
        </div>

        {error && <div className="reply failed">{error}</div>}
        {ok && <div className="reply created">{ok}</div>}

        {/* ---------------- 数据看板 ---------------- */}
        {tab === 'dashboard' && stats && (
          <>
            <div className="dash-grid">
              <div className="dcell">
                <div className="dk">房源总数</div>
                <div className="dv">{stats.house_total}</div>
                <div className="dd">在租 {stats.house_rented} · 可租 {stats.house_available}</div>
              </div>
              <div className="dcell">
                <div className="dk">出租率</div>
                <div className="dv">{stats.occupancy_rate}%</div>
                <div className="dd">均价 {Math.round(stats.avg_rent)} 元</div>
              </div>
              <div className="dcell money">
                <div className="dk">已收租金</div>
                <div className="dv">{Math.round(stats.income_received)}</div>
                <div className="dd">待收 {Math.round(stats.income_pending)} 元</div>
              </div>
              <div className={`dcell ${stats.repair_urgent ? 'warn' : ''}`}>
                <div className="dk">紧急工单</div>
                <div className="dv">{stats.repair_urgent}</div>
                <div className="dd">待处理 {stats.repair_pending} 条</div>
              </div>
            </div>

            <div className="section-title">月度收租</div>
            {stats.monthly_income.length === 0 ? (
              <p className="desc">还没有收租记录。</p>
            ) : (
              <div className="bar-chart">
                {stats.monthly_income.map((m) => (
                  <div className="bar-item" key={m.month}>
                    <div className="bar-val">{Math.round(m.amount)}</div>
                    <div
                      className="bar-fill"
                      style={{ height: `${Math.max((m.amount / maxIncome) * 100, 4)}%` }}
                    />
                    <div className="bar-lbl">{m.month.slice(5)}月</div>
                  </div>
                ))}
              </div>
            )}

            <div className="section-title">各区房源分布</div>
            <ul className="dist-list">
              {stats.by_district.map((d) => (
                <li key={d.district}>
                  <span className="dist-name">{d.district}</span>
                  <span className="dist-bar">
                    <span
                      style={{
                        width: `${(d.count / stats.by_district[0].count) * 100}%`,
                      }}
                    />
                  </span>
                  <span className="dist-n">{d.count} 套</span>
                  <span className="dist-avg">均 {Math.round(d.avg_rent)}</span>
                </li>
              ))}
            </ul>

            <div className="section-title">其它</div>
            <div className="sum-row">
              <div className="sum">
                <div className="sum-k">生效合同</div>
                <div className="sum-v">{stats.contract_active}</div>
                <div className="sum-d">共 {stats.contract_total} 份</div>
              </div>
              <div className="sum">
                <div className="sum-k">逾期账单</div>
                <div className={`sum-v ${stats.overdue_count ? 'danger' : ''}`}>
                  {stats.overdue_count}
                </div>
                <div className="sum-d">笔</div>
              </div>
            </div>
          </>
        )}

        {/* ---------------- 房源审核 ---------------- */}
        {tab === 'review' &&
          (pending.length === 0 ? (
            <p className="desc">没有待审核的房源。房东发布或修改房源后会出现在这里。</p>
          ) : (
            <ul className="record-list">
              {pending.map((house) => (
                <li key={house.house_no}>
                  <div className="record-head">
                    <strong>{house.title}</strong>
                    <span className="pill status-pending">待审核</span>
                  </div>
                  <div className="record-body">
                    {house.house_no} · {house.district} {house.community} ·
                    <span className="amount"> {house.rent_price} 元</span> ·
                    {' '}{house.layout} · {Math.round(house.area)}㎡
                  </div>
                  <div className="record-body">房东：{house.landlord_name}</div>
                  {house.description && (
                    <div className="record-body desc-clip">{house.description}</div>
                  )}
                  <div className="record-actions">
                    <button
                      className="btn small"
                      disabled={busy === house.house_no}
                      onClick={() => run(house.house_no,
                        () => api.reviewHouse(house.house_no, true),
                        '已通过审核并上架。')}
                    >
                      通过
                    </button>
                    <button
                      className="btn ghost small"
                      disabled={busy === house.house_no}
                      onClick={() => reject(house.house_no)}
                    >
                      驳回
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ))}

        {/* ---------------- 用户管理 ---------------- */}
        {tab === 'users' && (
          <>
            <form
              className="field"
              onSubmit={(e) => { e.preventDefault(); load() }}
            >
              <input
                placeholder="搜索用户名或姓名"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
              <button className="btn small" type="submit">搜索</button>
            </form>

            <ul className="record-list">
              {users.map((u) => (
                <li key={u.id}>
                  <div className="record-head">
                    <strong>{u.real_name || u.username}</strong>
                    <span className={`pill ${u.status === 'active'
                      ? 'status-confirmed' : 'status-rejected'}`}>
                      {u.status === 'active' ? '正常' : '已禁用'}
                    </span>
                  </div>
                  <div className="record-body">
                    {u.username} · {ROLE_LABEL[u.role] || u.role} · {u.phone}
                  </div>
                  {u.id !== user.id ? (
                    <div className="record-actions">
                      <button
                        className="btn ghost small"
                        disabled={busy === `u${u.id}`}
                        onClick={() => run(`u${u.id}`, () => api.updateUser(u.id, {
                          status: u.status === 'active' ? 'disabled' : 'active',
                        }))}
                      >
                        {u.status === 'active' ? '禁用' : '启用'}
                      </button>
                      {u.role !== 'admin' && (
                        <button
                          className="btn ghost small"
                          disabled={busy === `u${u.id}`}
                          onClick={() => run(`u${u.id}`, () => api.updateUser(u.id, {
                            role: u.role === 'tenant' ? 'landlord' : 'tenant',
                          }))}
                        >
                          转为{u.role === 'tenant' ? '房东' : '租客'}
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="own" style={{ marginTop: 8 }}>
                      当前登录账号，不可修改自身状态
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
    </>
  )
}
