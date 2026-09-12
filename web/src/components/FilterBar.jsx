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
  { label: '1500 元以下', min: null, max: 1500 },
  { label: '1500–2500', min: 1500, max: 2500 },
  { label: '2500–3500', min: 2500, max: 3500 },
  { label: '3500–5000', min: 3500, max: 5000 },
  { label: '5000 元以上', min: 5000, max: null },
]

const AREA_RANGES = [
  { label: '50㎡ 以下', min: null, max: 50 },
  { label: '50–80㎡', min: 50, max: 80 },
  { label: '80–120㎡', min: 80, max: 120 },
  { label: '120㎡ 以上', min: 120, max: null },
]

// ---------------------------------------------------------------- K-05
//
// 以下六组维度对应 K-04 新增的独立列。
//
// **值必须和库里存的一模一样。** 库里存「南」而按钮上写「朝南」，
// 点了就永远查不到——这个坑本项目已经踩过一次（种子数据原先存的正是
// 「朝南」「南北通透」）。描述里怎么写是另一回事，筛选走规范值。
const ORIENTATIONS = ['东', '南', '西', '北', '南北']
const FLOOR_LEVELS = ['低楼层', '中楼层', '高楼层']
const DECORATIONS = ['精装', '简装', '毛坯']
const HEATINGS = ['集中供暖', '自采暖']

// 电梯是三态：不限 / 有 / 无。**不能做成一个「有电梯」开关**——
// 那样「我要找没电梯的便宜房」就表达不出来，而这是真实存在的需求。
const ELEVATORS = [
  { key: 'true', label: '有电梯' },
  { key: 'false', label: '无电梯' },
]

const LEASE_TERMS = [
  { key: 'monthly', label: '一月起租' },
  { key: 'quarter', label: '三月内起租' },
  { key: 'half', label: '半年内起租' },
  { key: 'yearly', label: '年租起' },
]

const AVAILABLE = [
  { key: '0', label: '随时入住' },
  { key: '15', label: '15 天内' },
  { key: '30', label: '30 天内' },
]

const SORTS = [
  { key: 'published_at', label: '最新发布' },
  { key: 'rent_price', label: '租金从低到高' },
  { key: '-rent_price', label: '租金从高到低' },
  { key: '-area', label: '面积从大到小' },
  { key: 'view_count', label: '最多人看' },
  // 评分排序。「优选好房」那一栏用的就是它，但排序框里原先没有，
  // 用户进了那一栏改一下别的条件就再也排不回来了。
  { key: 'rating', label: '评分从高到低' },
]

/** 筛选栏不负责、但要跟着一起走的条件键。
 *  这些来自列表页的栏目预设与智能体解析结果。 */
const CARRY_KEYS = ['published_days', 'min_rating', 'keyword', 'features']

const EMPTY = {
  district: '', layout: '', rent_type: '',
  priceIndex: -1, areaIndex: -1, customPrice: null, customArea: null,
  orientation: '', floor_level: '', elevator: '',
  decoration: '', heating: '', lease_term: '', available_days: '',
  sort: 'published_at',
}

// 「更多筛选」里的那几项。收起时若其中有生效条件，要在按钮上标出来，
// 否则用户会看不见自己设过什么，只觉得结果莫名其妙地少
const ADVANCED_KEYS = ['orientation', 'floor_level', 'elevator',
  'decoration', 'heating', 'lease_term', 'available_days']

// 智能体解析出的「租价 ≤ 3000」落不进任何一个固定档位
// （档位是 2500–3500、3500–5000…）。这时 index 为 -1，
// 但**那个条件是真实存在的**，只是这排按钮表达不了它。
//
// 不记下来的话就会出现这个 bug：用一句话搜出「中山区两室三千以内」，
// 再点一下「中楼层」，`apply()` 会按 index=-1 发出空的价格区间，
// **把三千以内这一条抹掉**——正是 K-06 要消灭的那种「一操作就丢条件」。
function customRange(value, minKey, maxKey, index) {
  if (index >= 0) return null
  const min = value[minKey]
  const max = value[maxKey]
  if ((min ?? '') === '' && (max ?? '') === '') return null
  return { min: min ?? '', max: max ?? '' }
}

function stateFromValue(value = {}) {
  const priceIndex = PRICE_RANGES.findIndex((range) =>
    String(range.min ?? '') === String(value.rent_price_min ?? '') &&
    String(range.max ?? '') === String(value.rent_price_max ?? ''))
  const areaIndex = AREA_RANGES.findIndex((range) =>
    String(range.min ?? '') === String(value.area_min ?? '') &&
    String(range.max ?? '') === String(value.area_max ?? ''))
  return {
    customPrice: customRange(value, 'rent_price_min', 'rent_price_max',
                             priceIndex),
    customArea: customRange(value, 'area_min', 'area_max', areaIndex),
    district: value.district || '',
    layout: value.layout || '',
    rent_type: value.rent_type || '',
    priceIndex,
    areaIndex,
    orientation: value.orientation || '',
    floor_level: value.floor_level || '',
    elevator: value.elevator === undefined ? '' : String(value.elevator ?? ''),
    decoration: value.decoration || '',
    heating: value.heating || '',
    lease_term: value.lease_term || '',
    available_days: value.available_days === undefined || value.available_days === ''
      ? '' : String(value.available_days),
    sort: value.sort || 'published_at',
  }
}

export default function FilterBar({ value, onApply, disabled, chips }) {
  const [open, setOpen] = useState(false)
  const [more, setMore] = useState(false)
  const [f, setF] = useState(() => stateFromValue(value))
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

  // 地图选区和条件筛选由父组件维护同一份值。地图点击改变 value 后，
  // 在这里同步按钮高亮，形成真正的双向联动。
  useEffect(() => {
    setF(stateFromValue(value))
  }, [value])

  function pick(key, next) {
    // 再点一次同一项即取消选择，省掉一个「全部」按钮
    const cleared = typeof next === 'number' ? -1 : ''
    const state = { ...f, [key]: f[key] === next ? cleared : next }
    // 用户亲手点了价格档位，就以档位为准，此前那个表达不了的自定义值作废
    if (key === 'priceIndex') state.customPrice = null
    if (key === 'areaIndex') state.customArea = null
    setF(state)
    apply(state)
  }

  function apply(next) {
    const price = next.priceIndex >= 0 ? PRICE_RANGES[next.priceIndex] : null
    const area = next.areaIndex >= 0 ? AREA_RANGES[next.areaIndex] : null
    onApply({
      district: next.district,
      layout: next.layout,
      rent_type: next.rent_type,
      // 档位表达不了的自定义区间原样带走，不能被一个空值覆盖掉
      rent_price_min: price ? (price.min ?? '') : (next.customPrice?.min ?? ''),
      rent_price_max: price ? (price.max ?? '') : (next.customPrice?.max ?? ''),
      area_min: area ? (area.min ?? '') : (next.customArea?.min ?? ''),
      area_max: area ? (area.max ?? '') : (next.customArea?.max ?? ''),
      orientation: next.orientation,
      floor_level: next.floor_level,
      elevator: next.elevator,
      decoration: next.decoration,
      heating: next.heating,
      lease_term: next.lease_term,
      available_days: next.available_days,
      sort: next.sort,

      // **把本组件管不着的条件原样带回去。**
      //
      // 列表页的四个入口各自带一组预设：「最新发布」是
      // published_days=30，「优选好房」是 min_rating=4。这两个键
      // 不是筛选栏的字段，此前 apply 只回传自己认识的那些，
      // 于是用户一改排序，预设就被整组丢掉——
      // **「优选好房」当场变成「全部房源按评分排」，标题还挂着，
      // 内容已经不是那一栏了。**
      //
      // 筛选栏改的是它负责的那几项，不该顺手清掉别人的条件。
      ...carryOver(value),
    })
  }

  /** 取出本组件不负责、但必须原样保留的条件。 */
  function carryOver(v) {
    const keep = {}
    CARRY_KEYS.forEach((k) => {
      if (v?.[k] !== undefined && v[k] !== '') keep[k] = v[k]
    })
    return keep
  }

  function reset() {
    setF(EMPTY)
    // 重置的是「筛选条件」，不是「我在哪一栏」——
    // 把 published_days / min_rating 留下，用户仍在优选好房里
    onApply(carryOver(value))
  }

  const basicCount =
    (f.district ? 1 : 0) + (f.layout ? 1 : 0) + (f.rent_type ? 1 : 0) +
    (f.priceIndex >= 0 || f.customPrice ? 1 : 0) +
    (f.areaIndex >= 0 || f.customArea ? 1 : 0)
  const advancedCount = ADVANCED_KEYS.filter((k) => f[k] !== '').length
  const activeCount = basicCount + advancedCount

  return (
    <section className="filter-bar">
      <div className="filter-head">
        <button className="btn ghost small" onClick={() => setOpen(!open)}>
          {open ? '收起筛选' : '按条件筛选'}
          {activeCount > 0 && <span className="tab-n">{activeCount}</span>}
        </button>

        {/* C4：「已生效条件」原来单占一整块，摆在筛选栏上面。
            **可它们本来就是同一份东西**——筛选栏是「怎么改」，
            这排 chip 是「现在是什么」，中间隔着一条分割线和一段说明文字，
            用户要在两个框之间来回看才能确认自己筛的是什么。
            并进同一行之后，改和看在一个地方完成。 */}
        {chips}

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

          {/* 五组常用条件之外的收进「更多」。一次摊开十一组，
              手机上要滚三屏才看得到房源列表 */}
          <div className="filter-more-head">
            <button className="link" onClick={() => setMore(!more)}>
              {more ? '收起更多筛选' : '更多筛选'}
              {!more && advancedCount > 0 && (
                <span className="tab-n">{advancedCount}</span>
              )}
            </button>
          </div>

          {more && (
            <>
              <Row label="朝向">
                {ORIENTATIONS.map((o) => (
                  <Chip key={o} on={f.orientation === o}
                    onClick={() => pick('orientation', o)}>
                    {o}
                  </Chip>
                ))}
              </Row>

              <Row label="楼层">
                {FLOOR_LEVELS.map((l) => (
                  <Chip key={l} on={f.floor_level === l}
                    onClick={() => pick('floor_level', l)}>
                    {l}
                  </Chip>
                ))}
              </Row>

              <Row label="电梯">
                {ELEVATORS.map((e) => (
                  <Chip key={e.key} on={f.elevator === e.key}
                    onClick={() => pick('elevator', e.key)}>
                    {e.label}
                  </Chip>
                ))}
              </Row>

              <Row label="装修">
                {DECORATIONS.map((d) => (
                  <Chip key={d} on={f.decoration === d}
                    onClick={() => pick('decoration', d)}>
                    {d}
                  </Chip>
                ))}
              </Row>

              <Row label="供暖">
                {HEATINGS.map((h) => (
                  <Chip key={h} on={f.heating === h}
                    onClick={() => pick('heating', h)}>
                    {h}
                  </Chip>
                ))}
              </Row>

              <Row label="租期">
                {LEASE_TERMS.map((t) => (
                  <Chip key={t.key} on={f.lease_term === t.key}
                    onClick={() => pick('lease_term', t.key)}>
                    {t.label}
                  </Chip>
                ))}
              </Row>

              <Row label="入住">
                {AVAILABLE.map((a) => (
                  <Chip key={a.key} on={f.available_days === a.key}
                    onClick={() => pick('available_days', a.key)}>
                    {a.label}
                  </Chip>
                ))}
              </Row>
            </>
          )}
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
