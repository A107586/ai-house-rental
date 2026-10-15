import { useEffect, useState } from 'react'
import { api } from '../api'
import { filterChips } from './ActiveFilters'

/**
 * 搜不到房时，告诉用户**去掉哪一条还有房**。
 *
 * 原先这里只有一句「可以在已生效条件里删掉几条再试」。可用户不知道该删哪条：
 * 首页第一个示例「中山区两室一厅，租金三千以内，最好带电梯」就是 0 套——
 * 中山区均价四千五，三千以内的两室一厅一套没有；删电梯没用，删户型或预算才有。
 * 让人一条条去试，等于把系统知道的事推给用户猜。
 *
 * 做法：每个条件各去掉一次，问后端还剩几套（limit=1，只要 total），
 * 有房的按套数从多到少给出前三条，点一下就按那份条件重查。
 */
const MAX_HINTS = 3

export default function RelaxHints({ filters, onPick }) {
  const [options, setOptions] = useState([])
  const signature = JSON.stringify(filters)

  useEffect(() => {
    let alive = true
    // 只有一个条件时，去掉它就是「全部房源」，不用建议
    const chips = filterChips(filters)
    if (chips.length < 2) {
      setOptions([])
      return undefined
    }
    // 成对的价格 / 面积会出两个 chip，去掉后是同一份条件，只问一次
    const seen = new Set()
    const unique = chips.filter((chip) => {
      const key = JSON.stringify(chip.next)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    Promise.all(unique.map((chip) => api
      .listHouses({ ...chip.next, limit: 1, page: 1 })
      .then((data) => ({ ...chip, total: data.total || 0 }))
      .catch(() => null)))
      .then((rows) => {
        if (!alive) return
        setOptions(rows
          .filter((row) => row && row.total > 0)
          .sort((a, b) => b.total - a.total)
          .slice(0, MAX_HINTS))
      })
    return () => { alive = false }
    // filters 每次渲染都是新对象，按内容比较
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature])

  if (!options.length) return null
  return (
    <div className="relax-hints">
      <span>放宽一个条件就有房：</span>
      {options.map((option) => (
        <button
          key={option.key}
          className="chip-btn"
          onClick={() => onPick(option.next)}
        >
          去掉「{option.label}」，还有 {option.total} 套
        </button>
      ))}
    </div>
  )
}
