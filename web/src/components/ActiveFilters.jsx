/**
 * 已生效的检索条件（K-06）。
 *
 * 这一条是整个改造的落点。原先智能体解析出的条件只是**展示**在结果上方
 * 的一排灰底文字，用户改不了；想调整就得重新说一整句话。而一旦去点筛选
 * 栏，AI 的结果又会被整个清空——两条检索路径互斥，谁也接不住谁。
 *
 * 现在筛选条件只有一份，无论它来自哪里：
 *
 *     一句话  ──► 智能体只做「翻译」──┐
 *     筛选栏  ────────────────────────┼──► filters ──► /api/houses
 *     搜索框  ────────────────────────┘
 *
 * 所以这排 chip 要能**删**。删掉「租价 ≤ 3000」就是把这一条从 filters 里
 * 拿走再查一次，和自己去筛选栏取消选中完全等价。用户由此看得见
 * 「系统以为我要什么」，也改得动。
 *
 * 顺带解决了一个说不清的问题：智能体解析出「租价 ≤ 3000」这种自定义数值，
 * 筛选栏的固定档位（1500–2500 / 2500–3500）根本高亮不出来。
 * 与其把 AI 的结果硬塞进档位，不如另开一行如实显示。
 */

// 每个筛选键怎么显示、删掉它要清哪几个键。
// 价格和面积是成对的，删「租价 ≤ 3000」时若只清 max，
// 档位选择会残留一半，筛选栏的高亮就和实际条件对不上了。
const LABELS = {
  keyword: { text: (v) => `包含「${v}」`, clear: ['keyword'] },
  district: { text: (v) => `区域：${v}`, clear: ['district'] },
  layout: { text: (v) => `户型：${v}`, clear: ['layout'] },
  rent_type: { text: (v) => `方式：${v}`, clear: ['rent_type'] },
  rent_price_min: {
    text: (v) => `租价 ≥ ${v} 元`,
    clear: ['rent_price_min', 'rent_price_max'],
  },
  rent_price_max: {
    text: (v) => `租价 ≤ ${v} 元`,
    clear: ['rent_price_min', 'rent_price_max'],
  },
  area_min: { text: (v) => `面积 ≥ ${v} 平`, clear: ['area_min', 'area_max'] },
  area_max: { text: (v) => `面积 ≤ ${v} 平`, clear: ['area_min', 'area_max'] },
  orientation: { text: (v) => `朝向：${v}`, clear: ['orientation'] },
  floor_level: { text: (v) => v, clear: ['floor_level'] },
  elevator: {
    text: (v) => (String(v) === 'true' ? '有电梯' : '无电梯'),
    clear: ['elevator'],
  },
  decoration: { text: (v) => `装修：${v}`, clear: ['decoration'] },
  heating: { text: (v) => v, clear: ['heating'] },
  lease_term: {
    text: (v) => ({
      monthly: '一月起租', quarter: '三月内起租',
      half: '半年内起租', yearly: '年租起',
    }[v] || v),
    clear: ['lease_term'],
  },
  available_days: {
    text: (v) => (String(v) === '0' ? '随时入住' : `${v} 天内可入住`),
    clear: ['available_days'],
  },
}

// 展示顺序固定，不跟着对象键序走——同一组条件每次刷新排列不同，
// 看着像是变了
const ORDER = ['keyword', 'district', 'rent_price_min', 'rent_price_max',
  'layout', 'area_min', 'area_max', 'rent_type', 'orientation',
  'floor_level', 'elevator', 'decoration', 'heating', 'lease_term',
  'available_days']

export default function ActiveFilters({ value = {}, onChange, note,
                                       onSave, saveState }) {
  const chips = []

  ORDER.forEach((key) => {
    const raw = value[key]
    if (raw === '' || raw === null || raw === undefined) return
    const spec = LABELS[key]
    if (!spec) return
    chips.push({ key, label: spec.text(raw), clear: spec.clear })
  })

  // 设施偏好是一串，每个单独成 chip、单独可删
  const features = String(value.features || '').split(',').filter(Boolean)
  features.forEach((feature) => {
    chips.push({
      key: 'features:' + feature,
      label: feature,
      remove: () => {
        const rest = features.filter((f) => f !== feature)
        return { features: rest.join(',') }
      },
    })
  })

  if (chips.length === 0) return null

  function drop(chip) {
    if (chip.remove) {
      onChange({ ...value, ...chip.remove() })
      return
    }
    const next = { ...value }
    chip.clear.forEach((k) => { next[k] = '' })
    onChange(next)
  }

  return (
    <section className="active-filters">
      <span className="active-filters-label">
        已生效条件
        <span className="tab-n">{chips.length}</span>
      </span>

      <div className="active-filters-chips">
        {chips.map((chip) => (
          <button
            key={chip.key}
            className="chip removable"
            onClick={() => drop(chip)}
            title="点击移除这个条件"
          >
            {chip.label}
            <span aria-hidden="true">×</span>
          </button>
        ))}
        <button className="link" onClick={() => onChange({})}>
          全部清空
        </button>

        {/* 保存这组条件（K-09）。入口放在条件旁边，
            因为用户是在「条件终于调顺了」的那一刻才想存下来的；
            放个人中心里等他想起来去翻，等于没有 */}
        {onSave && (
          <button className="link save-search" onClick={onSave}
                  disabled={saveState === 'saving'}>
            {saveState === 'saving' ? '保存中…'
              : saveState === 'saved' ? '✓ 已保存到「我的搜索」'
              : '保存这组条件'}
          </button>
        )}
      </div>

      {note && <div className="active-filters-note">{note}</div>}
    </section>
  )
}
