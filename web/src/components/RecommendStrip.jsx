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
export default function RecommendStrip({ user, favorites, onToggleFavorite, onOpen }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [why, setWhy] = useState(false)

  useEffect(() => {
    if (!user) {
      setData(null)
      return
    }
    let alive = true
    setBusy(true)
    api
      .agentRecommend(6)
      .then((res) => { if (alive) setData(res) })
      // 推荐是锦上添花，拿不到就安静地不显示，不能打断主流程
      .catch(() => { if (alive) setData(null) })
      .finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [user])

  if (!user) return null

  if (busy) {
    return (
      <section className="rec">
        <div className="rec-head">
          <h3>为你推荐</h3>
          <span className="rec-tip">智能体正在分析你的浏览偏好…</span>
        </div>
      </section>
    )
  }

  // fallback / empty / error 都不展示模块，理由见文件头
  if (!data || data.status !== 'ok' || !data.items?.length) return null

  return (
    <section className="rec">
      <div className="rec-head">
        <h3>为你推荐</h3>
        <span className="rec-badge">AI 智能体</span>
        <span className="rec-tip">{data.reason}</span>
        <button className="btn ghost small" onClick={() => setWhy(!why)}>
          {why ? '收起依据' : '这些是怎么来的'}
        </button>
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
        {data.items.map((house) => (
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
