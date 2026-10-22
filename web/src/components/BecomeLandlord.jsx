import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 成为房东（K-13）。
 *
 * `奇思妙想.docx` 第 2 条：房东和租客本该是同一个账号的两个身份，
 * 而不是两个账号。改造前要演示完整链路得开两个账号来回切换。
 *
 * 界面上要解决的其实是**顾虑**，不是操作。操作就一个按钮；
 * 用户真正犹豫的是「我点了这一下，收藏和租约会不会没了」。
 * 所以这里把三件事明说：
 *
 * - 原有数据一条都不会少（它们本来就挂在账号上，与身份无关）；
 * - 还差什么资料，在点之前就告诉他，而不是点完弹一个错误；
 * - 这一步不可逆，以及为什么——名下有房源和合同的账号降回租客，
 *   那些数据就没有主人了。
 */
export default function BecomeLandlord({ onUpgraded }) {
  const [info, setInfo] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  useEffect(() => {
    let alive = true
    api
      .landlordEligibility()
      .then((data) => alive && setInfo(data))
      .catch((err) => alive && setError(err.message))
    return () => { alive = false }
  }, [])

  // 经纪人也整个藏掉：他点下去只会得到一句「不能直接转为房东」，
  // 而那句话解释的是一件他此刻并不想做的事
  if (!info || info.is_agent) return null
  // 房东这一侧给「退回租客」。接口早就有，页面上一直没有入口（2026-10-22 补）
  if (info.already_landlord) {
    return info.role === 'landlord'
      ? <RevertToTenant onReverted={onUpgraded} />
      : null
  }

  async function upgrade() {
    setBusy(true)
    setError('')
    try {
      const data = await api.becomeLandlord()
      setDone(data.message)
      // 申请制以后这个接口不再返回 user（审核通过前身份不变）。原先照样把 data.user
      // 交上去，等于 setUser(undefined)：个人中心正开着，读 user.real_name 当场崩掉
      if (data.user && onUpgraded) onUpgraded(data.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="section-title">我也要出租房子</div>

      {done ? (
        <div className="reply created">{done}</div>
      ) : (
        <>
          <p className="desc">
            同一个账号可以同时是租客和房东，不用再注册。提交申请、管理员审核通过后，顶部会多出「工作台」，
            可以发布和管理房源；<b>现有的收藏、足迹、租约和账单，一条都不会少</b>。
          </p>

          {error && <div className="reply failed">{error}</div>}

          {info.request?.status === 'pending' && (
            <div className="reply pending">
              房东申请已于 {info.request.created_at} 提交，正在等管理员审核，通过后会站内通知你。
              审核期间找房、收藏、预约都不受影响。
            </div>
          )}
          {info.request?.status === 'rejected' && (
            <div className="assist warn">
              <div className="assist-body">
                上次的申请（{info.request.created_at}）没有通过：{info.request.review_note || '未写原因'}
                <div className="assist-hint">按上面说的补齐之后可以再申请一次。</div>
              </div>
            </div>
          )}

          {info.missing.length > 0 && (
            <div className="assist warn">
              <div className="assist-body">
                还差 <b>{info.missing.join('、')}</b> 没填。
                <div className="assist-hint">
                  签约时甲方要用真实姓名，租客也要靠手机号联系你，所以这两项必填。在上面的「个人资料」里补一下就行。
                </div>
              </div>
            </div>
          )}

          <button
            className="btn ghost block"
            disabled={busy || !info.can_upgrade}
            onClick={upgrade}
          >
            {busy ? '处理中…' : info.request?.status === 'pending' ? '申请审核中' : '申请成为房东'}
          </button>

          {/* 原先写「成为房东后不能再改回租客」——退回租客早就有了，只是要先把房源下架、合同结清 */}
          <div className="fld-hint">
            以后不想出租了，下架全部房源、结清合同后，可以在这里退回租客身份。
          </div>
        </>
      )}
    </>
  )
}


/** 不再出租了：退回租客。前置条件由后端判断并列出（在架房源、没结束的合同），这里只管问一句 */
function RevertToTenant({ onReverted }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  async function revert() {
    if (busy) return
    if (!window.confirm('退回租客后，工作台和发布房源的入口会收起来；收藏、租约、账单都还在。'
                        + '以后想再出租，需要重新申请。确定吗？')) return
    setBusy(true)
    setError('')
    try {
      const data = await api.revertToTenant()
      setDone(data.message)
      if (data.user && onReverted) onReverted(data.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="section-title">不再出租了</div>
      {done ? (
        <div className="reply created">{done}</div>
      ) : (
        <>
          {error && <div className="reply failed">{error}</div>}
          <p className="desc">
            名下房源全部下架、合同都结束之后，可以退回租客身份。还没处理完的，点了会列出来。
          </p>
          <button className="btn ghost block" disabled={busy} onClick={revert}>
            {busy ? '处理中…' : '退回租客'}
          </button>
        </>
      )}
    </>
  )
}
