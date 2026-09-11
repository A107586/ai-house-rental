import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * T-03 地图找房。
 *
 * 这里用的是手绘的大连市区示意图，不是瓦片地图。理由是明确的：
 * 瓦片地图必须实时向第三方地图服务取图，本项目的部署与演示环境都不
 * 保证外网可达，一旦取不到图整块功能就是白板。示意图把「按区域找房」
 * 这件事本身做完整——区域形状、相对方位、房源密度、均价对比、点击筛选
 * 都在，且完全离线可用。
 *
 * 图上的颜色与数字全部来自 `/api/stats`，不是写死的。
 */

// 各区在示意图上的位置。大连主城区沿辽东半岛尖端由东北向西南排布：
// 金州在北，甘井子居中，旅顺口在西南尖端，
// 沙河口—西岗—中山三区自西向东沿南岸一字排开。
const DISTRICTS = [
  {
    name: '金州区',
    path: 'M352 18 L470 12 L520 58 L508 112 L432 132 L358 108 L330 62 Z',
    label: [424, 72],
  },
  {
    name: '甘井子区',
    path: 'M240 96 L330 62 L358 108 L432 132 L438 196 L372 236 L286 228 L228 178 Z',
    label: [330, 168],
  },
  {
    name: '旅顺口区',
    path: 'M52 262 L118 218 L228 178 L286 228 L268 292 L184 336 L96 330 L44 300 Z',
    label: [162, 268],
  },
  {
    name: '沙河口区',
    path: 'M268 292 L286 228 L372 236 L376 300 L348 336 L286 334 Z',
    label: [322, 288],
  },
  {
    name: '西岗区',
    path: 'M376 300 L372 236 L438 196 L452 252 L440 312 L400 330 Z',
    label: [412, 268],
  },
  {
    name: '中山区',
    path: 'M452 252 L438 196 L508 172 L556 214 L548 286 L490 322 L440 312 Z',
    label: [498, 244],
  },
]

// 底图轮廓，画出海岸线的感觉，让六块区域不至于像六个随机色块
const COAST = 'M330 8 L486 4 L540 56 L568 208 L552 296 L486 332 L342 344 '
  + 'L176 344 L60 328 L32 292 L46 252 L120 210 L214 168 L236 92 Z'

const METRICS = [
  { key: 'count', label: '房源数量', unit: '套' },
  { key: 'avg_rent', label: '平均租金', unit: '元' },
]

/**
 * `rows` 传进来时用传进来的，不再去拉全站统计。
 *
 * **这是 A1 的一半。** 工作台的「房源分布」原样复用了这个组件，
 * 而它默认读 `/api/stats` —— 那是全站口径。于是房东打开自己的
 * 房源分布，看到的是「旅顺口区 18 套」，可他在那个区一套也没有。
 * 接口本身是隔离的（实测 landlord1 → 14 套、landlord2 → 9 套），
 * 错的是**这张图问错了人**。
 *
 * 同一个组件服务两种口径，就必须让调用方说清楚要哪一种；
 * 让组件自己猜「我现在是在列表页还是工作台」是更糟的写法。
 */
export default function MapFinder({ selected, onPick, rows, title, hint }) {
  const [data, setData] = useState(rows || [])
  const [metric, setMetric] = useState('count')
  const [hover, setHover] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    // 外部给了数据就不联网。**用 undefined 判断而不是真值判断**——
    // 空数组是一个有意义的答案（这个房东一套房都没有），
    // 按真值判断会让它掉进「去拉全站数据」的分支，
    // 于是「我没有房源」显示成「全城 95 套」
    if (rows !== undefined) { setData(rows || []); return undefined }
    let alive = true
    api
      .stats()
      .then((s) => { if (alive) setData(s.by_district || []) })
      .catch((err) => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [rows])

  const byName = Object.fromEntries(data.map((d) => [d.district, d]))
  const values = data.map((d) => d[metric] || 0)
  const max = Math.max(1, ...values)
  const min = Math.min(...(values.length ? values : [0]))

  function shade(name) {
    const row = byName[name]
    if (!row) return 'var(--surface-2)'
    // 归一化到 0.12–0.85：留出下限，否则数据最少的区会淡到看不出是个区
    const span = Math.max(max - min, 1)
    const t = 0.12 + ((row[metric] || 0) - min) / span * 0.73
    // 房源数量用智能体色系，租金用房源色系，与全站的配色约定一致
    return metric === 'count'
      ? `rgba(15, 111, 104, ${t.toFixed(2)})`
      : `rgba(168, 69, 47, ${t.toFixed(2)})`
  }

  const active = hover || selected
  const activeRow = active ? byName[active] : null

  return (
    <section className="mapfinder">
      <div className="map-head">
        {/* 标题和说明也要跟着口径走。图上画的是「我的 14 套」，
            标题却写「地图找房」，房东会以为这是全城的图 */}
        <h3>{title || '地图找房'}</h3>
        <span className="map-tip">{hint || '点击区域即可筛选房源'}</span>
        <div className="map-metrics">
          {METRICS.map((m) => (
            <button
              key={m.key}
              className={`tab ${metric === m.key ? 'active' : ''}`}
              onClick={() => setMetric(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
        {selected && (
          <button className="btn ghost small" onClick={() => onPick('')}>
            取消选择
          </button>
        )}
      </div>

      {error && <div className="reply failed">{error}</div>}

      <div className="map-body">
        <svg viewBox="0 0 600 360" className="map-svg" role="img"
             aria-label="大连市区房源分布示意图">
          <path d={COAST} className="map-coast" />

          {DISTRICTS.map((d) => {
            const row = byName[d.name]
            const isActive = selected === d.name
            return (
              <g
                key={d.name}
                className={`map-area ${isActive ? 'on' : ''} ${row ? '' : 'nodata'}`}
                onClick={() => onPick(isActive ? '' : d.name)}
                onMouseEnter={() => setHover(d.name)}
                onMouseLeave={() => setHover('')}
              >
                <title>
                  {row
                    ? `${d.name}：${row.count} 套，均价 ${Math.round(row.avg_rent)} 元`
                    : `${d.name}：暂无房源`}
                </title>
                <path d={d.path} fill={shade(d.name)} />
                <text x={d.label[0]} y={d.label[1]} className="map-name">
                  {d.name.replace('区', '')}
                </text>
                {row && (
                  <text x={d.label[0]} y={d.label[1] + 17} className="map-num">
                    {metric === 'count'
                      ? `${row.count} 套`
                      : `${Math.round(row.avg_rent)} 元`}
                  </text>
                )}
              </g>
            )
          })}
        </svg>

        <div className="map-side">
          {activeRow ? (
            <>
              <div className="map-card-name">{active}</div>
              <div className="map-card-row">
                <span>在租房源</span><b>{activeRow.count} 套</b>
              </div>
              <div className="map-card-row">
                <span>平均租金</span>
                <b className="accent">{Math.round(activeRow.avg_rent)} 元</b>
              </div>
              <div className="map-card-row">
                <span>较全市均价</span>
                <b>{diffText(activeRow.avg_rent, data)}</b>
              </div>
              {/* C3：去掉了「只看 xx 的房源」这个按钮。
                  **点区块本身就已经筛了**——它是同一个动作的第二个入口，
                  摆在这里反而让人以为「点了区块还不算，得再点一下才生效」。 */}
            </>
          ) : (
            <div className="map-hint">
              {/* 不说「把鼠标移到」——手机上没有鼠标，
                  点选是两端都成立的说法 */}
              点选任一区域，即可只看该区的房源。
              <br />
              <br />
              颜色深浅表示
              {metric === 'count' ? '房源数量的多少' : '平均租金的高低'}，
              {rows === undefined ? '数据取自平台实时统计。' : '统计范围仅限你名下的房源。'}
            </div>
          )}

          <ul className="map-rank">
            {[...data]
              .sort((a, b) => (b[metric] || 0) - (a[metric] || 0))
              .map((row) => (
                <li
                  key={row.district}
                  className={selected === row.district ? 'on' : ''}
                  onClick={() => onPick(
                    selected === row.district ? '' : row.district)}
                >
                  <span className="dot" style={{ background: shade(row.district) }} />
                  {row.district}
                  <b>
                    {metric === 'count'
                      ? `${row.count} 套`
                      : `${Math.round(row.avg_rent)} 元`}
                  </b>
                </li>
              ))}
          </ul>
        </div>
      </div>

      <div className="map-foot">
        示意图按各区相对方位绘制，用于区域选择与密度对比，不用于精确定位。
      </div>
    </section>
  )
}

function diffText(value, data) {
  const all = data.filter((d) => d.avg_rent)
  if (!all.length) return '—'
  const avg = all.reduce((sum, d) => sum + d.avg_rent, 0) / all.length
  const delta = Math.round(value - avg)
  if (Math.abs(delta) < 50) return '基本持平'
  return delta > 0 ? `高 ${delta} 元` : `低 ${-delta} 元`
}
