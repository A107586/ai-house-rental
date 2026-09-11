import { useEffect, useState } from 'react'
import { api } from '../api'
import HouseCard from './HouseCard'

/**
 * 首页的分栏。
 *
 * 改版前首页只有一条「全部房源」的长列表，用户一进来面对的是
 * 九十多套房，没有任何切入点。国内平台的首页都是分栏的——
 * 每栏回答一个不同的问题：
 *
 *   为你推荐  你可能想要什么（要登录、要有浏览过的记录）
 *   最新发布  这两天新上了什么
 *   优选好房  别人都在看什么
 *   全部房源  我想自己翻
 *
 * **每栏只出 4 套**，右上角一个「全部 N 套」跳到完整列表页。
 * 首页的职责是给入口，不是把所有房子摆完。
 */

/** 每栏在首页展示几套。4 套刚好一行，多一行首页就太长了。 */
const PREVIEW = 4

/** 四栏的定义。`query` 直接是列表接口的参数，`kind` 用作路由参数。 */
export const SECTIONS = [
  {
    kind: 'latest',
    title: '最新发布',
    desc: '最近 30 天上架的新房源',
    // **筛选 + 排序，不是只排序。**
    // 只按时间倒序的话，翻到第十页仍然叫「最新发布」——名不副实。
    // published_days 把范围收在 30 天内，卡片上的「N 天前维护」才对得上。
    query: { published_days: 30, sort: 'published_at' },
    more: '全部最新房源',
  },
  {
    kind: 'featured',
    title: '优选好房',
    desc: '租客评分 4 星以上',
    // 按真实评分筛。min_rating 走的是评价表的聚合（排除已软删除的），
    // 不是某个人工标记的「精选」字段——那种字段谁都能改，说服不了人。
    query: { min_rating: 4, sort: 'rating' },
    more: '全部优选房源',
  },
  {
    kind: 'all',
    title: '全部房源',
    desc: '自由浏览，可用筛选和地图收窄范围',
    query: {},
    more: '进入全部房源',
  },
]

function Section({ section, favorites, onToggleFavorite, onOpen, onMore }) {
  const [list, setList] = useState([])
  const [total, setTotal] = useState(0)
  const [state, setState] = useState('loading')   // loading | ok | fail

  useEffect(() => {
    let alive = true
    setState('loading')
    api.listHouses({ ...section.query, limit: PREVIEW, page: 1 })
      .then((data) => {
        if (!alive) return
        setList(data.list || [])
        setTotal(data.total || 0)
        setState('ok')
      })
      .catch(() => { if (alive) setState('fail') })
    // 组件卸载后到达的响应直接丢掉，避免对已卸载组件 setState
    return () => { alive = false }
  }, [section.kind])

  // 这一栏取不到数就整块不渲染。**一栏挂了不该拖累其他栏**——
  // 摆一个「加载失败」的空框在首页上，比少一栏更难看。
  if (state === 'fail') return null

  return (
    <section className="home-sec">
      <div className="home-sec-head">
        <div>
          <h2 className="home-sec-title">{section.title}</h2>
          <p className="home-sec-desc">{section.desc}</p>
        </div>
        {/* 跳转放右上角，和标题同一行——国内平台一致的位置 */}
        {/* 四栏的跳转文案各不相同。原先三栏都写「全部 95 套」——
            同一个数字出现三遍，用户根本分不出点哪个会去哪。 */}
        <button className="home-sec-more" onClick={() => onMore(section.kind)}>
          {section.more}
          {total ? <em>{total} 套</em> : null}
          <span className="more-arrow" aria-hidden="true">›</span>
        </button>
      </div>

      {state === 'loading' ? (
        <div className="grid home-grid" aria-hidden="true">
          {Array.from({ length: PREVIEW }).map((_, i) => (
            <div className="card skeleton" key={i}>
              <div className="sk-photo" />
              <div className="sk-line wide" />
              <div className="sk-line" />
              <div className="sk-line short" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid home-grid">
          {list.map((house) => (
            <HouseCard
              key={house.house_no}
              house={house}
              favorited={favorites.includes(house.house_no)}
              onToggleFavorite={onToggleFavorite}
              onClick={onOpen}
            />
          ))}
        </div>
      )}
    </section>
  )
}

export default function HomeSections({ favorites, onToggleFavorite, onOpen, onMore }) {
  return (
    <>
      {SECTIONS.map((section) => (
        <Section
          key={section.kind}
          section={section}
          favorites={favorites}
          onToggleFavorite={onToggleFavorite}
          onOpen={onOpen}
          onMore={onMore}
        />
      ))}
    </>
  )
}
