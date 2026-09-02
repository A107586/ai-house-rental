import { useEffect, useState } from 'react'
import { api } from '../api'

// 区域列表从接口取，不写死。写死的话新增一个区，筛选器里就永远没有它；
// 顺带能显示每个区有多少套，选之前就知道值不值得点。
// 接口拿不到时退回这份兜底，不能让筛选器整个空掉。
const FALLBACK_DISTRICTS = ['中山区', '西岗区', '沙河口区', '甘井子区',
  '旅顺口区', '金州区']
const LAYOUTS = ['一室', '一室一厅', '两室一厅', '两室两厅', '三室一厅', '三室两厅']
const RENT_TYPES = ['整租', '合租', '公寓']

// 租金档位。用区间而不是滑块——滑块在移动端难操作，
// 而且租房用户脑子里本来就是「三千以内」这种档位思维。
const PRICE_RANGES = [
  { label: '1500 以下', min: null, max: 1500 },
  { label: '1500–2500', min: 1500, max: 2500 },
  { label: '2500–3500', min: 2500, max: 3500 },
  { label: '3500–5000', min: 3500, max: 5000 },
  { label: '5000 以上', min: 5000, max: null },
]

const AREA_RANGES = [
  { label: '50㎡ 以下', min: null, max: 50 },
  { label: '50–80㎡', min: 50, max: 80 },
  { label: '80–120㎡', min: 80, max: 120 },
  { label: '120㎡ 以上', min: 120, max: null },
]

const SORTS = [
  { key: 'published_at', label: '最新发布' },
  { key: 'rent_price', label: '租金从低到高' },
  { key: '-rent_price', label: '租金从高到低' },
  { key: '-area', label: '面积从大到小' },
  { key: 'view_count', label: '最多人看' },
]

const EMPTY = {
  district: '', layout: '', rent_type: '',
  priceIndex: -1, areaIndex: -1, sort: 'published_at',
}

export default function FilterBar({ onApply, disabled }) {
  const [open, setOpen] = useState(false)
  const [f, setF] = useState(EMPTY)
  const [districts, setDistricts] = useState(
    FALLBACK_DISTRICTS.map((d) => ({ district: d, count: null })))

  useEffect(() => {
    let alive = true
    api
      .listDistricts()
      .then((rows) => {
        if (alive && Array.isArray(rows) && rows.length) setDistricts(rows)
      })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  function pick(key, value) {
    // 再点一次同一项即取消选择，省掉一个「全部」按钮
    const next = { ...f, [key]: f[key] === value ? (typeof value === 'number' ? -1 : '') : value }
    setF(next)
    apply(next)
  }

  function apply(next) {
    const price = next.priceIndex >= 0 ? PRICE_RANGES[next.priceIndex] : null
    const area = next.areaIndex >= 0 ? AREA_RANGES[next.areaIndex] : null
    onApply({
      district: next.district,
      layout: next.layout,
      rent_type: next.rent_type,
      rent_price_min: price?.min ?? '',
      rent_price_max: price?.max ?? '',
      area_min: area?.min ?? '',
      area_max: area?.max ?? '',
      sort: next.sort,
    })
  }

  function reset() {
    setF(EMPTY)
    onApply({})
  }

  const activeCount =
    (f.district ? 1 : 0) + (f.layout ? 1 : 0) + (f.rent_type ? 1 : 0) +
    (f.priceIndex >= 0 ? 1 : 0) + (f.areaIndex >= 0 ? 1 : 0)

  return (
    <section className="filter-bar">
      <div className="filter-head">
        <button className="btn ghost small" onClick={() => setOpen(!open)}>
          {open ? '收起筛选' : '按条件筛选'}
          {activeCount > 0 && <span className="tab-n">{activeCount}</span>}
        </button>

        <select
          className="sort-select"
          value={f.sort}
          onChange={(e) => {
            const next = { ...f, sort: e.target.value }
            setF(next)
            apply(next)
          }}
          disabled={disabled}
        >
          {SORTS.map((s) => (
            <option key={s.key} value={s.key}>{s.label}</option>
          ))}
        </select>

        {activeCount > 0 && (
          <button className="link" onClick={reset}>清空筛选</button>
        )}
      </div>

      {open && (
        <div className="filter-body">
          <Row label="区域">
            {districts.map((d) => (
              <Chip key={d.district} on={f.district === d.district}
                onClick={() => pick('district', d.district)}>
                {d.district}
                {d.count != null && <span className="chip-n">{d.count}</span>}
              </Chip>
            ))}
          </Row>

          <Row label="租金">
            {PRICE_RANGES.map((p, i) => (
              <Chip key={p.label} on={f.priceIndex === i}
                onClick={() => pick('priceIndex', i)}>
                {p.label}
              </Chip>
            ))}
          </Row>

          <Row label="户型">
            {LAYOUTS.map((l) => (
              <Chip key={l} on={f.layout === l} onClick={() => pick('layout', l)}>
                {l}
              </Chip>
            ))}
          </Row>

          <Row label="面积">
            {AREA_RANGES.map((a, i) => (
              <Chip key={a.label} on={f.areaIndex === i}
                onClick={() => pick('areaIndex', i)}>
                {a.label}
              </Chip>
            ))}
          </Row>

          <Row label="方式">
            {RENT_TYPES.map((t) => (
              <Chip key={t} on={f.rent_type === t} onClick={() => pick('rent_type', t)}>
                {t}
              </Chip>
            ))}
          </Row>
        </div>
      )}
    </section>
  )
}

function Row({ label, children }) {
  return (
    <div className="filter-row">
      <span className="filter-label">{label}</span>
      <div className="filter-opts">{children}</div>
    </div>
  )
}

function Chip({ on, onClick, children }) {
  return (
    <button className={`chip-btn ${on ? 'on' : ''}`} onClick={onClick}>
      {children}
    </button>
  )
}
