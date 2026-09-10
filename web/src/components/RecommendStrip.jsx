import { useEffect, useState } from 'react'
import { api } from '../api'
import HouseCard from './HouseCard'

/**
 * AI-07 房源智能推荐。
 *
 * 展示上有两条刻意的设计：
 *
 * 1. **推荐必须说得出理由**。每张卡片下面挂着智能体给出的命中原因，
 *    而不是只甩一个匹配度分数。「算法觉得你喜欢」既不可信也不可辩护。
 * 2. **样本不足时不硬推**。后端返回 status="fallback" 时整个模块隐藏，
 *    而不是退而求其次推一批不相关的房源——那比不推更伤信任。
 */
export default function RecommendStrip({ user, favorites, onToggleFavorite,
  onOpen, onLogin }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [why, setWhy] = useState(false)
  // 「更多推荐」翻到第几组。推荐总共取 12 套，每组 4 套，
  // 点一次换下一组，转完回到第一组。
  //
  // **刻意不重新请求**：推荐的依据是浏览足迹，两次点击之间足迹没变，
  // 再请求一次拿回来的还是同一批，白等一趟网络往返。
  const [group, setGroup] = useState(0)

  useEffect(() => {
    if (!user) {
      setData(null)
      return
    }
    let alive = true
    setBusy(true)
    api
      .agentRecommend(12)
      .then((res) => { if (alive) setData(res) })
      // 推荐是锦上添花，拿不到就安静地不显示，不能打断主流程
      .catch(() => { if (alive) setData(null) })
      .finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [user])

  // 未登录不显示这一栏：没有浏览足迹就没有推荐可言，
  // 摆一个空栏位在首页上只是占地方
  if (!user) return null

  if (busy) {
    return (
      <section className="home-sec rec">
        <div className="home-sec-head">
          <div>
            <h2 className="home-sec-title">为你推荐</h2>
            <p className="home-sec-desc">智能体正在分析你的浏览偏好…</p>
          </div>
        </div>
      </section>
    )
  }

  // fallback / empty / error 都不展示模块，理由见文件头
  if (!data || data.status !== 'ok' || !data.items?.length) return null

  // 每组 4 套，和首页其余三栏对齐（一排四个）。
  // 不足一整组也算一组，否则取回 5 套时第二组只有 1 套却翻不到。
  const PER = 4
  const items = data.items
  const groupCount = Math.max(1, Math.ceil(items.length / PER))
  const start = (group % groupCount) * PER
  const shown = items.slice(start, start + PER)

  return (
    /* 版式跟首页其余三栏统一（home-sec）：标题在左、跳转在右上角。
       原先这一栏自己一套 rec-head，和下面三栏对不齐，
       看起来像是从别的页面粘过来的。 */
    <section className="home-sec rec">
      <div className="home-sec-head">
        <div>
          <h2 className="home-sec-title">
            为你推荐
            <span className="rec-badge">AI 智能体</span>
          </h2>
          <p className="home-sec-desc">{data.reason}</p>
        </div>
        <div className="rec-actions">
          <button className="home-sec-more ghosty" onClick={() => setWhy(!why)}>
            {why ? '收起依据' : '推荐依据'}
          </button>
          {groupCount > 1 && (
            <button
              className="home-sec-more"
              onClick={() => setGroup((g) => (g + 1) % groupCount)}
            >
              更多推荐
              <em>{group + 1}/{groupCount}</em>
              <span className="more-arrow" aria-hidden="true">↻</span>
            </button>
          )}
        </div>
      </div>

      {why && (
        <div className="rec-why">
          智能体读取了你的浏览足迹与收藏，统计出常看的区域、户型、租金区间与标签，
          再按「区域 30 分、租金 25 分、户型 20 分、标签 15 分、出租方式 10 分」
          给每套候选房源打分，看过的房源会被压分让位给新房源。
          分数低于 15 分的不会推给你。
          <b>全过程由规则计算，不调用大模型</b>——推荐理由必须对得上真实数据。
        </div>
      )}

      <div className="grid rec-grid">
        {shown.map((house) => (
          <div className="rec-item" key={house.house_no}>
            <HouseCard
              house={house}
              favorited={favorites.includes(house.house_no)}
              onToggleFavorite={onToggleFavorite}
              onClick={onOpen}
            />
            <div className="rec-reasons">
              <span className="rec-score">匹配 {house.match_score}</span>
              {house.match_reasons.slice(0, 3).map((reason) => (
                <span className="rec-reason" key={reason}>{reason}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
