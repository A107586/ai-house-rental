import { useState } from 'react'

// 对比维度。用「越大越好」标记，用来决定哪个值算优胜。
const FIELDS = [
  { key: 'rent_price', label: '月租金', unit: '元', better: 'low' },
  { key: 'area', label: '面积', unit: '㎡', better: 'high' },
  { key: 'layout', label: '户型' },
  { key: 'district', label: '区域' },
  { key: 'rent_type', label: '出租方式' },
  { key: 'orientation', label: '朝向' },
  { key: 'floor', label: '楼层' },
  { key: 'deposit', label: '押金', unit: '元', better: 'low' },
  { key: 'rating', label: '评分', better: 'high' },
]

export default function CompareBar({ items, onRemove, onClear }) {
  const [open, setOpen] = useState(false)

  if (items.length === 0) return null

  // 每个可比字段找出优胜值，渲染时高亮
  const best = {}
  FIELDS.filter((f) => f.better).forEach((f) => {
    const values = items
      .map((h) => Number(h[f.key]))
      .filter((v) => Number.isFinite(v) && v > 0)
    if (values.length > 1) {
      best[f.key] = f.better === 'low' ? Math.min(...values) : Math.max(...values)
    }
  })

  return (
    <>
      <div className="compare-bar">
        <span className="compare-count">已选 {items.length} / 4 套</span>
        <div className="compare-thumbs">
          {items.map((h) => (
            <span className="compare-chip" key={h.house_no}>
              {h.title.slice(0, 10)}
              <button onClick={() => onRemove(h.house_no)} aria-label="移除">×</button>
            </span>
          ))}
        </div>
        <button className="btn small" disabled={items.length < 2}
          onClick={() => setOpen(true)}>
          对比
        </button>
        <button className="btn ghost small" onClick={onClear}>清空</button>
      </div>

      {open && (
        <>
          <div className="drawer-mask" onClick={() => setOpen(false)} />
          <div className="compare-modal">
            <button className="drawer-close" onClick={() => setOpen(false)}
              aria-label="关闭">×</button>
            <h3>房源对比</h3>

            <div className="tw">
              <table className="compare-table">
                <thead>
                  <tr>
                    <th>对比项</th>
                    {items.map((h) => (
                      <th key={h.house_no}>{h.title}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {FIELDS.map((f) => (
                    <tr key={f.key}>
                      <td className="cmp-label">{f.label}</td>
                      {items.map((h) => {
                        const raw = h[f.key]
                        const num = Number(raw)
                        const isBest = f.better && Number.isFinite(num)
                          && best[f.key] === num && items.length > 1
                        const shown = f.key === 'rating'
                          ? (num > 0 ? `${num} 分` : '暂无评价')
                          : `${raw ?? '—'}${f.unit || ''}`
                        return (
                          <td key={h.house_no} className={isBest ? 'cmp-best' : ''}>
                            {shown}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                  <tr>
                    <td className="cmp-label">房源特色</td>
                    {items.map((h) => (
                      <td key={h.house_no}>
                        <div className="tags">
                          {(h.tags || []).map((t) => (
                            <span className="tag" key={t}>{t}</span>
                          ))}
                        </div>
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>

            <p className="desc">
              绿色底表示该项在所选房源中更优。租金与押金越低越好，面积与评分越高越好。
            </p>
          </div>
        </>
      )}
    </>
  )
}
