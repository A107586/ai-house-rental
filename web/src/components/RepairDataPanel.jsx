import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * F-09 维修数据：物品、人员、记录与统计。
 *
 * 统计里最有价值的一块是「报修最多的房源」。同一套房反复报修，说明的
 * 往往不是「又坏了一次」，而是上次没修好、或者房子本身有问题——
 * 这是纯看工单列表看不出来的。
 */
const SUB_TABS = [
  { key: 'stats', label: '统计' },
  { key: 'records', label: '维修记录' },
  { key: 'workers', label: '维修人员' },
  { key: 'items', label: '物品库存' },
]

const EMPTY_WORKER = { name: '', phone: '', skill: '' }
const EMPTY_ITEM = { name: '', unit: '个', unit_price: '', stock: '' }

export default function RepairDataPanel() {
  const [sub, setSub] = useState('stats')
  const [stats, setStats] = useState(null)
  const [records, setRecords] = useState([])
  const [workers, setWorkers] = useState([])
  const [items, setItems] = useState([])
  const [worker, setWorker] = useState(EMPTY_WORKER)
  const [item, setItem] = useState(EMPTY_ITEM)
  // 正在改价／补库存的物品。测试用例 P-09 要验证「调价不影响历史成本」，
  // 没有这个入口就只能去调接口。
  const [editing, setEditing] = useState(null)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load() {
    setError('')
    try {
      const [s, r, w, i] = await Promise.all([
        api.repairStats(), api.repairRecords(),
        api.repairWorkers(), api.repairItems(),
      ])
      setStats(s)
      setRecords(r.list || [])
      setWorkers(w.list || [])
      setItems(i.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function addWorker(e) {
    e.preventDefault()
    setError('')
    setOk('')
    try {
      await api.createWorker(worker)
      setWorker(EMPTY_WORKER)
      setOk('维修人员已添加。')
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function addItem(e) {
    e.preventDefault()
    setError('')
    setOk('')
    try {
      await api.createItem({
        ...item,
        unit_price: Number(item.unit_price) || 0,
        stock: Number(item.stock) || 0,
      })
      setItem(EMPTY_ITEM)
      setOk('物品已入库。')
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function saveItem(e) {
    e.preventDefault()
    setError('')
    setOk('')
    try {
      await api.updateItem(editing.id, {
        unit_price: Number(editing.unit_price) || 0,
        stock: Number(editing.stock) || 0,
      })
      setEditing(null)
      setOk('已保存。调价只影响以后的领用，历史记录不受影响。')
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function toggleWorker(row) {
    try {
      await api.updateWorker(row.id,
        { status: row.status === 'active' ? 'off' : 'active' })
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  const maxCat = Math.max(1, ...(stats?.by_category || []).map((c) => c.count))

  return (
    <div className="repairdata">
      <div className="tabs sub">
        {SUB_TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${sub === t.key ? 'active' : ''}`}
            onClick={() => { setSub(t.key); setError(''); setOk('') }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {sub === 'stats' && stats && (
        <>
          <div className="dash-grid">
            <div className="dcell">
              <div className="dk">报修总数</div>
              <div className="dv">{stats.repair_total}</div>
            </div>
            <div className="dcell">
              <div className="dk">待处理</div>
              <div className="dv">{stats.pending}</div>
            </div>
            <div className="dcell">
              <div className="dk">维修总支出</div>
              <div className="dv">{stats.total_cost}</div>
            </div>
            <div className="dcell">
              <div className="dk">单次均价</div>
              <div className="dv">{stats.avg_cost}</div>
            </div>
          </div>

          <section className="rd-sec">
            <h5>故障类型分布</h5>
            {stats.by_category.length === 0 ? (
              <div className="empty">还没有报修记录。房子有问题，可以在房源详情页提交，也可以直接跟租房助手说。</div>
            ) : (
              <div className="rd-bars">
                {stats.by_category.map((row) => (
                  <div className="rd-bar" key={row.category}>
                    <span className="rd-bar-k">{row.category}</span>
                    <span className="rd-bar-track">
                      <span
                        className="rd-bar-fill"
                        style={{ width: `${(row.count / maxCat) * 100}%` }}
                      />
                    </span>
                    <span className="rd-bar-v">{row.count}</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="rd-sec">
            <h5>报修最多的房源</h5>
            {stats.top_houses.length === 0 ? (
              <div className="empty">暂无数据</div>
            ) : (
              <table className="log-table">
                <thead>
                  <tr><th>房源</th><th>标题</th><th>报修次数</th></tr>
                </thead>
                <tbody>
                  {stats.top_houses.map((row) => (
                    <tr key={row.house_no}>
                      <td className="mono">{row.house_no}</td>
                      <td>{row.title}</td>
                      <td><b>{row.count}</b></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="rd-sec">
            <h5>物品消耗</h5>
            {stats.by_item.length === 0 ? (
              <div className="empty">还没有领用记录</div>
            ) : (
              <table className="log-table">
                <thead>
                  <tr><th>物品</th><th>消耗数量</th><th>金额</th></tr>
                </thead>
                <tbody>
                  {stats.by_item.map((row) => (
                    <tr key={row.name}>
                      <td>{row.name}</td>
                      <td>{row.quantity}</td>
                      <td className="accent">{row.amount} 元</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="rd-sec">
            <h5>人员工作量</h5>
            {stats.by_worker.length === 0 ? (
              <div className="empty">还没有维修记录</div>
            ) : (
              <table className="log-table">
                <thead>
                  <tr><th>维修人员</th><th>完成工单</th><th>工时</th><th>产生费用</th></tr>
                </thead>
                <tbody>
                  {stats.by_worker.map((row) => (
                    <tr key={row.name}>
                      <td>{row.name}</td>
                      <td>{row.count}</td>
                      <td>{row.hours} 小时</td>
                      <td className="accent">{row.cost} 元</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      {sub === 'records' && (
        records.length === 0 ? <div className="empty">还没有维修记录</div> : (
          <div className="table-wrap">
            <table className="log-table">
              <thead>
                <tr>
                  <th>完成时间</th><th>房源</th><th>类型</th><th>维修人员</th>
                  <th>用料</th><th>工时费</th><th>材料费</th><th>合计</th>
                </tr>
              </thead>
              <tbody>
                {records.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.finished_at}</td>
                    <td className="mono">{row.house_no}</td>
                    <td>{row.category || '未分类'}</td>
                    <td>{row.worker_name}</td>
                    <td>
                      {row.items.length === 0 ? '—' : row.items.map((u) => (
                        <div key={u.item_name}>
                          {u.item_name} ×{u.quantity}
                          <span className="rd-price"> @{u.unit_price}</span>
                        </div>
                      ))}
                    </td>
                    <td>{row.labor_cost}</td>
                    <td>{row.item_cost}</td>
                    <td className="accent"><b>{row.total_cost}</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {sub === 'workers' && (
        <>
          <form className="rd-form" onSubmit={addWorker}>
            <input placeholder="姓名" required value={worker.name}
                   onChange={(e) => setWorker({ ...worker, name: e.target.value })} />
            <input placeholder="电话" value={worker.phone}
                   onChange={(e) => setWorker({ ...worker, phone: e.target.value })} />
            <input placeholder="擅长类型，如 水电" value={worker.skill}
                   onChange={(e) => setWorker({ ...worker, skill: e.target.value })} />
            <button className="btn small" type="submit">添加</button>
          </form>

          {workers.length === 0 ? <div className="empty">还没有维修人员</div> : (
            <table className="log-table">
              <thead>
                <tr><th>姓名</th><th>电话</th><th>擅长</th><th>状态</th><th /></tr>
              </thead>
              <tbody>
                {workers.map((row) => (
                  <tr key={row.id}>
                    <td>{row.name}</td>
                    <td className="mono">{row.phone || '-'}</td>
                    <td>{row.skill || '-'}</td>
                    <td>{row.status === 'active' ? '在岗' : '停用'}</td>
                    <td>
                      <button className="btn ghost small"
                              onClick={() => toggleWorker(row)}>
                        {row.status === 'active' ? '停用' : '恢复'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {sub === 'items' && (
        <>
          <form className="rd-form" onSubmit={addItem}>
            <input placeholder="物品名称" required value={item.name}
                   onChange={(e) => setItem({ ...item, name: e.target.value })} />
            <input placeholder="单位" value={item.unit}
                   onChange={(e) => setItem({ ...item, unit: e.target.value })} />
            <input placeholder="单价" type="number" min="0" value={item.unit_price}
                   onChange={(e) => setItem({ ...item, unit_price: e.target.value })} />
            <input placeholder="库存" type="number" min="0" value={item.stock}
                   onChange={(e) => setItem({ ...item, stock: e.target.value })} />
            <button className="btn small" type="submit">入库</button>
          </form>

          <p className="rd-note">
            调价只影响以后的领用。历史记录里存的是当时单价，不会被改价改写。
          </p>

          {items.length === 0 ? <div className="empty">还没有物品</div> : (
            <table className="log-table">
              <thead>
                <tr><th>物品</th><th>单位</th><th>单价</th><th>库存</th><th /></tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id} className={row.stock === 0 ? 'bad' : ''}>
                    <td>{row.name}</td>
                    <td>{row.unit}</td>
                    {editing?.id === row.id ? (
                      <>
                        <td>
                          <input type="number" min="0" className="cell-input"
                            value={editing.unit_price}
                            onChange={(e) => setEditing({
                              ...editing, unit_price: e.target.value })} />
                        </td>
                        <td>
                          <input type="number" min="0" className="cell-input"
                            value={editing.stock}
                            onChange={(e) => setEditing({
                              ...editing, stock: e.target.value })} />
                        </td>
                        <td>
                          <button className="btn small" onClick={saveItem}>保存</button>
                          <button className="btn ghost small"
                            onClick={() => setEditing(null)}>取消</button>
                        </td>
                      </>
                    ) : (
                      <>
                        <td className="accent">{row.unit_price} 元</td>
                        <td>{row.stock}</td>
                        <td>
                          <button className="btn ghost small"
                            onClick={() => setEditing({
                              id: row.id,
                              unit_price: String(row.unit_price),
                              stock: String(row.stock) })}>
                            调价／补货
                          </button>
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}
