import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 工单结案登记（F-09）。
 *
 * 原先房东点「标记完成」只是把状态改成 done，**人、物、工时、费用一概
 * 不记**——于是维修数据统计里只看得到通过接口结案的那部分，界面上完成的
 * 全部漏算。两条路径改同一个状态却产生不同的数据，统计就不可信了。
 *
 * 现在完成必须走这个对话框。工时费和物料都可以不填（有些故障就是拧一下
 * 螺丝），但**至少要经过这一步**，让"这单是谁处理的"有个着落。
 */
export default function CloseRepairDialog({ repair, onDone, onClose }) {
  const [workers, setWorkers] = useState([])
  const [items, setItems] = useState([])
  const [workerId, setWorkerId] = useState('')
  const [hours, setHours] = useState('')
  const [laborCost, setLaborCost] = useState('')
  const [note, setNote] = useState('')
  const [used, setUsed] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([api.repairWorkers(), api.repairItems()])
      .then(([w, i]) => {
        setWorkers((w.list || []).filter((x) => x.status === 'active'))
        setItems(i.list || [])
      })
      .catch((err) => setError(err.message))
  }, [])

  // 按故障类型把对口的师傅排在前面，省得在一长串名字里找
  const sortedWorkers = [...workers].sort((a, b) => {
    const hit = (w) => (w.skill && repair.category
      && w.skill === repair.category ? 0 : 1)
    return hit(a) - hit(b)
  })

  function addItem(id) {
    if (!id || used.some((u) => u.item_id === Number(id))) return
    setUsed([...used, { item_id: Number(id), quantity: 1 }])
  }

  function setQty(id, qty) {
    setUsed(used.map((u) => (u.item_id === id
      ? { ...u, quantity: Math.max(1, Number(qty) || 1) } : u)))
  }

  const itemCost = used.reduce((sum, u) => {
    const it = items.find((x) => x.id === u.item_id)
    return sum + (it ? it.unit_price * u.quantity : 0)
  }, 0)
  const total = itemCost + (Number(laborCost) || 0)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api.closeRepair(repair.id, {
        worker_id: workerId ? Number(workerId) : null,
        hours: Number(hours) || 0,
        labor_cost: Number(laborCost) || 0,
        note,
        items: used,
      })
      onDone()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        <h3>登记维修记录</h3>
        <div className="loc">
          {repair.house_no} · {repair.category || '未分类'} · {repair.urgency}
        </div>

        <div className="reply">{repair.content}</div>

        {error && <div className="reply failed">{error}</div>}

        <form onSubmit={submit} className="house-form">
          <label className="fld">
            <span>维修人员</span>
            <select value={workerId} onChange={(e) => setWorkerId(e.target.value)}>
              <option value="">未指派</option>
              {sortedWorkers.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}{w.skill ? `（${w.skill}）` : ''}
                </option>
              ))}
            </select>
          </label>

          <div className="fld-row">
            <label className="fld">
              <span>工时（小时）</span>
              <input type="number" step="0.5" min="0" value={hours}
                     onChange={(e) => setHours(e.target.value)} placeholder="0" />
            </label>
            <label className="fld">
              <span>工时费（元）</span>
              <input type="number" min="0" value={laborCost}
                     onChange={(e) => setLaborCost(e.target.value)} placeholder="0" />
            </label>
          </div>

          <label className="fld">
            <span>领用物品</span>
            <select value="" onChange={(e) => addItem(e.target.value)}>
              <option value="">选择物品加入…</option>
              {items.map((it) => (
                <option key={it.id} value={it.id} disabled={it.stock === 0}>
                  {it.name} · {it.unit_price} 元/{it.unit} · 库存 {it.stock}
                </option>
              ))}
            </select>
          </label>

          {used.length > 0 && (
            <ul className="use-list">
              {used.map((u) => {
                const it = items.find((x) => x.id === u.item_id)
                if (!it) return null
                return (
                  <li key={u.item_id}>
                    <span className="use-name">{it.name}</span>
                    <input type="number" min="1" max={it.stock} value={u.quantity}
                           onChange={(e) => setQty(u.item_id, e.target.value)} />
                    <span className="use-amount">
                      {(it.unit_price * u.quantity).toFixed(2)} 元
                    </span>
                    <button type="button" className="btn ghost small"
                            onClick={() => setUsed(used.filter(
                              (x) => x.item_id !== u.item_id))}>
                      移除
                    </button>
                  </li>
                )
              })}
            </ul>
          )}

          <label className="fld">
            <span>处理说明</span>
            <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)}
                      placeholder="如：更换水龙头，已回访确认" />
          </label>

          <div className="use-total">
            材料费 {itemCost.toFixed(2)} + 工时费 {(Number(laborCost) || 0).toFixed(2)}
            = <b>{total.toFixed(2)} 元</b>
          </div>

          <p className="rd-note">
            提交后工单转为「已完成」，物料从库存扣减，并计入维修统计。
            <b>一张工单只能登记一次</b>——重复登记会把物料重复扣一遍。
          </p>

          <button className="btn block" type="submit" disabled={busy}>
            {busy ? '提交中…' : '完成并登记'}
          </button>
        </form>
      </aside>
    </>
  )
}
