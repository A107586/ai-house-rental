import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 房东发起签约，或者修改一份还没生效的合同（传 `contract` 就是修改）。
 *
 * 在这之前，创建合同的**唯一入口是接口文档页**——测试文档里明写着
 * 「在 /docs 调 POST /api/contracts」。签约是整个交易链的起点，
 * 主干流程却要去调接口，这条链在界面上就是断的。
 *
 * 月租、押金、中介佣金都**预填房源与委托上的数，可以改**（2026-10-22）。
 * 原先这三样锁死：议了价只能先去改挂牌价再签，而改挂牌价又会让别的
 * 看房人看到一个专为某人谈的价。不改就按原样走——押金这一项出过
 * 详情页显示 800、合同签出另一个数的问题（K-02），所以「按房源」仍是默认。
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

function monthsBetween(start, end) {
  const [sy, sm] = start.split('-').map(Number)
  const [ey, em] = end.split('-').map(Number)
  return Math.max((ey - sy) * 12 + (em - sm), 1)
}

// 修改时押金一栏回填成哪一档：整月数就落在「押几」上，改月租时押金跟着走；
// 不成整月的数（月租 2800、押金 800）只能按自定义金额原样留着
function depositModeOf(contract) {
  if (!contract) return ''
  const n = contract.deposit / contract.rent
  return Number.isInteger(n) && n >= 0 && n <= 3 ? String(n) : 'custom'
}

const MONTHS_CN = ['免押', '押一', '押二', '押三', '押四', '押五', '押六']

const DEPOSIT_OPTIONS = [
  ['0', '免押'], ['1', '押一'], ['2', '押二'], ['3', '押三'], ['custom', '自定义金额'],
]

export default function SignContractDialog({ houses, contract, onDone, onClose }) {
  const editing = Boolean(contract)
  const [houseNo, setHouseNo] = useState(contract?.house_no || '')
  const [tenant, setTenant] = useState('')
  const [start, setStart] = useState(contract?.start_date || today())
  const [months, setMonths] = useState(
    contract ? monthsBetween(contract.start_date, contract.end_date) : 12)
  const [rent, setRent] = useState(contract ? String(contract.rent) : '')
  const [depositMode, setDepositMode] = useState(depositModeOf(contract))
  const [depositAmount, setDepositAmount] = useState(contract ? String(contract.deposit) : '')
  const [payMonths, setPayMonths] = useState(contract?.pay_months || 1)
  const [rate, setRate] = useState(
    contract?.commission_rate != null ? String(contract.commission_rate) : '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // B5：这套房可以签给谁。选了房源才有意义，所以跟着 houseNo 取
  const [candidates, setCandidates] = useState([])
  // 这套房的受托中介。有才显示佣金那一栏——没委托的房子不涉及中介费
  const [delegation, setDelegation] = useState(null)
  // 修改时房源不在 houses 里（合同卡片那边没有房源列表），单独取一次挂牌信息
  const [listing, setListing] = useState(null)

  useEffect(() => {
    if (!houseNo || editing) { setCandidates([]); return }
    let alive = true
    api.contractCandidates(houseNo)
      .then((d) => alive && setCandidates(d.list || []))
      // 取不到就当没有候选：**手填那条路一直留着**，
      // 不能因为这个辅助功能挂了就签不了约
      .catch(() => alive && setCandidates([]))
    return () => { alive = false }
  }, [houseNo, editing])

  useEffect(() => {
    if (!houseNo) { setDelegation(null); return }
    let alive = true
    api.delegations({ status: 'active' })
      .then((d) => {
        if (!alive) return
        const found = (d.list || []).find((x) => x.house_no === houseNo) || null
        setDelegation(found)
        // 没约定过就预填委托时的比例
        if (found) setRate((r) => (r === '' ? String(found.commission_rate) : r))
      })
      .catch(() => alive && setDelegation(null))
    return () => { alive = false }
  }, [houseNo])

  useEffect(() => {
    if (!editing) return
    let alive = true
    api.getHouse(houseNo).then((h) => alive && setListing(h)).catch(() => {})
    return () => { alive = false }
  }, [editing, houseNo])

  const matched = candidates.find((c) => c.username === tenant.trim())

  // 只有「可租」的房源能签。已租的再签一份会出现两份生效合同，
  // 待审核的还没上架就更不该签
  const signable = (houses || []).filter((h) => h.status === 'available')
  const house = signable.find((h) => h.house_no === houseNo) || listing

  function pickHouse(no) {
    setHouseNo(no)
    const h = signable.find((x) => x.house_no === no)
    setRent(h ? String(Math.round(h.rent_price)) : '')
    setDepositMode('')
    setRate('')
  }

  const rentNum = Number(rent) || (house ? house.rent_price : 0)
  const listRent = house ? Math.round(house.rent_price) : null
  const deposit = depositMode === 'custom' ? Number(depositAmount) || 0
    : depositMode !== '' ? rentNum * Number(depositMode)
      : house?.deposit_months != null ? rentNum * house.deposit_months
        // 房源上押金是 0 又没选押付方式，那是「没填」，后端按系统默认月数算
        : house?.deposit || null
  const rateNum = rate === '' ? null : Number(rate)
  const agreedRate = contract?.commission_rate ?? delegation?.commission_rate ?? null

  async function submit(e) {
    e.preventDefault()
    if (!houseNo || (!editing && !tenant.trim())) {
      setError('请选择房源，并指定合同乙方。')
      return
    }
    if (depositMode === 'custom' && depositAmount === '') {
      setError('选了自定义押金，请填金额。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = {
        // 原样传本地零点。原先转成 ISO（UTC）再发：选 10-25 签出来的合同从 10-24 16:00 起，
        // 账单都排在每月 24 号——和默认值的问题叠在一起，默认签出来的合同早了两天
        start_date: `${start}T00:00:00`,
        months: Number(months) || 12,
        pay_months: Number(payMonths) || 1,
      }
      if (rent !== '') body.rent = Number(rent)
      // 押金「按房源」就不传。押金口径由后端一处裁决：
      // 显式金额 > 显式月数 > 房源上填的押金 > 系统配置（K-02）。
      // 在前端补一个默认值，房源上填的押金就永远读不到了
      if (depositMode === 'custom') body.deposit = Number(depositAmount)
      else if (depositMode !== '') body.deposit_months = Number(depositMode)
      // 佣金和原先约定的一样就不传，合同上留空表示「按委托走」
      if (delegation && rateNum !== null && rateNum !== agreedRate) {
        body.commission_rate = rateNum
      }
      if (editing) {
        await api.updateContract(contract.contract_no, body)
      } else {
        await api.createContract({ ...body, house_no: houseNo,
                                   tenant_username: tenant.trim() })
      }
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
        <h3>{editing ? '修改合同条款' : '发起签约'}</h3>
        <div className="loc">
          {editing
            ? `合同 ${contract.contract_no} 还没生效，改完会通知租客重新查看`
            : '合同生成后需要租客在「我的租约」里确认才生效'}
        </div>

        {error && <div className="reply failed">{error}</div>}

        {!editing && signable.length === 0 ? (
          <div className="empty">
            名下没有「可租」状态的房源。已出租的不能重复签约，待审核的要先过审。
          </div>
        ) : (
          <form onSubmit={submit} className="house-form">
            {editing ? (
              <div className="fld">
                <span>房源</span>
                <div>{contract.house_no} · {contract.house_title}</div>
              </div>
            ) : (
              <label className="fld">
                <span>房源</span>
                <select value={houseNo} onChange={(e) => pickHouse(e.target.value)}>
                  <option value="">选择一套可租房源…</option>
                  {signable.map((h) => (
                    <option key={h.house_no} value={h.house_no}>
                      {h.house_no} · {h.title} · {Math.round(h.rent_price)} 元/月
                    </option>
                  ))}
                </select>
              </label>
            )}

            {/* B5：乙方从候选里选，不靠手打。
                原先这里要房东**手打租客用户名**——打错一个字符得到的是
                「租客不存在」，而他手上多半只有一个微信昵称或者一句
                「小李」，平台用户名压根不知道。
                候选只列在这套房上约过看房或被带看过的人：真要签约的那个人
                一定在这两张表里留过痕迹，而列出全站用户等于做了个通讯录。
                修改时不能换人——换人签是另一份合同 */}
            {!editing && (
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
            )}

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
                <span>月租（元）</span>
                <input type="number" min="1" step="1" value={rent}
                       onChange={(e) => setRent(e.target.value)}
                       placeholder={listRent ? `挂牌 ${listRent}` : '先选房源'} />
                {listRent && rent !== '' && Number(rent) !== listRent && (
                  <small className="fld-hint">
                    挂牌 {listRent} 元，议价 {Number(rent) > listRent ? '+' : ''}
                    {Math.round(Number(rent) - listRent)} 元
                  </small>
                )}
              </label>
              <label className="fld">
                <span>付款周期（月）</span>
                <input type="number" min="1" max="12" value={payMonths}
                       onChange={(e) => setPayMonths(e.target.value)} />
              </label>
            </div>

            <div className="fld-row">
              <label className="fld">
                <span>押金</span>
                <select value={depositMode} onChange={(e) => setDepositMode(e.target.value)}>
                  <option value="">
                    按房源{house?.deposit_months != null ? `（${MONTHS_CN[house.deposit_months]}）`
                      : house?.deposit ? `（${Math.round(house.deposit)} 元）` : ''}
                  </option>
                  {DEPOSIT_OPTIONS.map(([v, label]) => (
                    <option key={v} value={v}>{label}</option>
                  ))}
                </select>
              </label>
              {depositMode === 'custom' && (
                <label className="fld">
                  <span>押金金额（元）</span>
                  <input type="number" min="0" step="1" value={depositAmount}
                         onChange={(e) => setDepositAmount(e.target.value)} />
                </label>
              )}
            </div>

            {/* 佣金是房东付给中介的，**不写进租赁合同正文**——那是房东和租客两方的
                文本。这里约定的比例只决定成交时记给中介的那笔佣金 */}
            {delegation && (
              <label className="fld">
                <span>中介佣金（月租的 %）</span>
                <input type="number" min="0" max="100" step="1" value={rate}
                       onChange={(e) => setRate(e.target.value)} />
                <small className="fld-hint">
                  受托中介 {delegation.agent_name} · 委托时约定 {delegation.commission_rate}%
                  {rateNum !== null && ` · 按月租计 ${Math.round(rentNum * rateNum / 100)} 元，成交后由你支付`}
                  {rateNum !== null && rateNum !== agreedRate && '，改动会通知中介'}
                </small>
              </label>
            )}

            {house && (
              <div className="assist">
                <div className="assist-body">
                  月租 <b>{Math.round(rentNum)}</b> 元
                  {deposit !== null && <>，押金 <b>{Math.round(deposit)}</b> 元</>}
                  {payMonths > 1 && `，每 ${payMonths} 个月一付`}。
                  <div className="assist-hint">
                    租客确认生效时会一次排满整个租期的账单：1 条押金 +
                    {' '}{months} 条租金，不用每月手工补。
                  </div>
                </div>
              </div>
            )}

            <button className="btn block" type="submit" disabled={busy}>
              {busy ? '提交中…' : editing ? '保存修改' : '生成合同'}
            </button>
          </form>
        )}
      </aside>
    </>
  )
}
