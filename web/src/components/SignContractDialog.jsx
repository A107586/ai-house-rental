import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 房东发起签约。
 *
 * 在这之前，创建合同的**唯一入口是接口文档页**——测试文档里明写着
 * 「在 /docs 调 POST /api/contracts」。签约是整个交易链的起点，
 * 主干流程却要去调接口，这条链在界面上就是断的。
 *
 * 表单只收四个必要参数，其余由后端算：
 * 租金取房源当前定价、**押金取房源上填的那个数**、结束日按起始日推算。
 * 让房东手填这些既啰嗦又容易和房源信息对不上——押金这一项就出过这个问题：
 * 详情页显示房源上填的 800，合同却按「租金 × 系统默认月数」签出另一个数（K-02）。
 */
/** 本地日期 YYYY-MM-DD。**不能用 toISOString**：那是 UTC，北京时间零点会变成前一天
 *  16:00，起租日默认值因此一直是「昨天」（2026-10-22） */
function localDay(offsetDays = 0) {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function today() {
  return localDay(0)
}

export default function SignContractDialog({ houses, onDone, onClose }) {
  const [houseNo, setHouseNo] = useState('')
  const [tenant, setTenant] = useState('')
  const [start, setStart] = useState(today())
  const [months, setMonths] = useState(12)
  const [depositMonths, setDepositMonths] = useState('')
  const [payMonths, setPayMonths] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // B5：这套房可以签给谁。选了房源才有意义，所以跟着 houseNo 取
  const [candidates, setCandidates] = useState([])

  useEffect(() => {
    if (!houseNo) { setCandidates([]); return }
    let alive = true
    api.contractCandidates(houseNo)
      .then((d) => alive && setCandidates(d.list || []))
      // 取不到就当没有候选：**手填那条路一直留着**，
      // 不能因为这个辅助功能挂了就签不了约
      .catch(() => alive && setCandidates([]))
    return () => { alive = false }
  }, [houseNo])

  const matched = candidates.find((c) => c.username === tenant.trim())

  // 只有「可租」的房源能签。已租的再签一份会出现两份生效合同，
  // 待审核的还没上架就更不该签
  const signable = (houses || []).filter((h) => h.status === 'available')
  const picked = signable.find((h) => h.house_no === houseNo)

  async function submit(e) {
    e.preventDefault()
    if (!houseNo || !tenant.trim()) {
      setError('请选择房源，并指定合同乙方。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = {
        house_no: houseNo,
        tenant_username: tenant.trim(),
        // 原样传本地零点。原先转成 ISO（UTC）再发：选 10-25 签出来的合同从 10-24 16:00 起，
        // 账单都排在每月 24 号——和默认值的问题叠在一起，默认签出来的合同早了两天
        start_date: `${start}T00:00:00`,
        months: Number(months) || 12,
        pay_months: Number(payMonths) || 1,
      }
      // 不填就不传。押金口径由后端一处裁决：
      // 显式月数 > 房源上填的押金 > 系统配置（K-02）。
      // 在前端补一个默认值，房源上填的押金就永远读不到了
      if (depositMonths !== '') body.deposit_months = Number(depositMonths)
      await api.createContract(body)
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
        <h3>发起签约</h3>
        <div className="loc">合同生成后需要租客在「我的租约」里确认才生效</div>

        {error && <div className="reply failed">{error}</div>}

        {signable.length === 0 ? (
          <div className="empty">
            名下没有「可租」状态的房源。已出租的不能重复签约，待审核的要先过审。
          </div>
        ) : (
          <form onSubmit={submit} className="house-form">
            <label className="fld">
              <span>房源</span>
              <select value={houseNo} onChange={(e) => setHouseNo(e.target.value)}>
                <option value="">选择一套可租房源…</option>
                {signable.map((h) => (
                  <option key={h.house_no} value={h.house_no}>
                    {h.house_no} · {h.title} · {Math.round(h.rent_price)} 元/月
                  </option>
                ))}
              </select>
            </label>

            {/* B5：乙方从候选里选，不靠手打。
                原先这里要房东**手打租客用户名**——打错一个字符得到的是
                「租客不存在」，而他手上多半只有一个微信昵称或者一句
                「小李」，平台用户名压根不知道。
                候选只列在这套房上约过看房或被带看过的人：真要签约的那个人
                一定在这两张表里留过痕迹，而列出全站用户等于做了个通讯录。 */}
            <label className="fld">
              <span>乙方（租客）</span>
              {candidates.length > 0 ? (
                <select value={tenant} onChange={(e) => setTenant(e.target.value)}>
                  <option value="">请选择</option>
                  {candidates.map((c) => (
                    <option key={c.username} value={c.username}>
                      {c.real_name || c.username}（{c.username}）· {c.source}
                      {c.last_at ? ` · ${c.last_at}` : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <input value={tenant} onChange={(e) => setTenant(e.target.value)}
                       placeholder="填租客的平台用户名，如 tenant1" />
              )}
              {houseNo && candidates.length === 0 && (
                <small className="fld-hint">
                  这套房还没有人约过看房，只能手填用户名
                </small>
              )}
              {/* 选中之后把真名摆出来。**合同上的乙方写的就是这个名字**，
                  签之前必须让他核对一眼「我要签的是不是这个人」 */}
              {matched && (
                <small className="fld-hint">
                  合同乙方将写：<strong>{matched.real_name || '（该租客未填真实姓名）'}</strong>
                  {!matched.ready && '　·　该租客的姓名或手机号不全，签约后需补齐'}
                </small>
              )}
            </label>

            <div className="fld-row">
              <label className="fld">
                <span>起租日</span>
                {/* 和后端同一个范围：最多往前补 30 天、往后一年 */}
                <input type="date" value={start} min={localDay(-30)} max={localDay(365)}
                       onChange={(e) => setStart(e.target.value)} />
              </label>
              <label className="fld">
                <span>租期（月）</span>
                <input type="number" min="1" max="60" value={months}
                       onChange={(e) => setMonths(e.target.value)} />
              </label>
            </div>

            <div className="fld-row">
              <label className="fld">
                <span>押金月数</span>
                <input type="number" min="0" max="6" value={depositMonths}
                       onChange={(e) => setDepositMonths(e.target.value)}
                       placeholder="留空按房源押金" />
              </label>
              <label className="fld">
                <span>付款周期（月）</span>
                <input type="number" min="1" max="12" value={payMonths}
                       onChange={(e) => setPayMonths(e.target.value)} />
              </label>
            </div>

            {picked && (
              <div className="assist">
                <div className="assist-body">
                  月租按房源当前定价 <b>{Math.round(picked.rent_price)}</b> 元计，
                  押金默认取房源上填的 <b>{Math.round(picked.deposit)}</b> 元
                  ，和租客在详情页看到的一致。
                  <div className="assist-hint">
                    确认生效时会一次排满整个租期的账单：1 条押金 +
                    {' '}{months} 条租金，不用每月手工补。
                  </div>
                </div>
              </div>
            )}

            <button className="btn block" type="submit" disabled={busy}>
              {busy ? '提交中…' : '生成合同'}
            </button>
          </form>
        )}
      </aside>
    </>
  )
}
