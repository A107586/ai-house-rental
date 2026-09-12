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
  if (!info || info.already_landlord || info.is_agent) return null

  async function upgrade() {
    setBusy(true)
    setError('')
    try {
      const data = await api.becomeLandlord()
      setDone(data.message)
      onUpgraded && onUpgraded(data.user)
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
            同一个账号可以同时是租客和房东，不用再注册。升级后顶部会多出「工作台」，可以发布和管理房源；
            <b>现有的收藏、足迹、租约和账单，一条都不会少</b>。
          </p>

          {error && <div className="reply failed">{error}</div>}

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
            {busy ? '处理中…' : '成为房东'}
          </button>

          <div className="fld-hint">
            这一步不可逆。名下有房源和合同后再降回租客，那些数据就没了主人——真要退出出租，应当先下架房源、结清合同。
          </div>
        </>
      )}
    </>
  )
}
