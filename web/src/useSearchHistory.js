import { useCallback, useState } from 'react'

/**
 * 搜索记录。
 *
 * 替掉原先那个「保存这组条件」按钮。那个按钮的问题是**它要求用户
 * 预判自己以后还会不会用这组条件**——人在搜的当下不会想这件事，
 * 所以那个按钮基本没人点。
 *
 * 改成自动记：搜过的就留下，下次一点输入框就出现在下面。
 * 用户不用决定什么，也不用管理什么。
 *
 * **存在本地而不是后端**，理由有两条：
 *   1. 搜索记录是「这台设备上这个人刚才在找什么」，跟着浏览器走最合适；
 *   2. 未登录也该有——找房的人多数是先逛再注册的。
 *
 * 后端那套「保存的搜索」（saved-searches）没有删，它解决的是另一件事：
 * 带条件的长期订阅。两者不冲突。
 */

const KEY = 'rental_search_history'
/** 最多留 10 条。再多下拉框就得自己滚动，反而不如直接重打一遍。 */
const MAX = 10

function read() {
  try {
    const raw = localStorage.getItem(KEY)
    const list = raw ? JSON.parse(raw) : []
    return Array.isArray(list) ? list.filter((x) => typeof x === 'string') : []
  } catch {
    // 隐私模式下读不到就当没有记录，不该因此让搜索框不能用
    return []
  }
}

function write(list) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list))
  } catch {
    // 存不进去也不影响本次搜索，静默即可
  }
}

export default function useSearchHistory() {
  const [list, setList] = useState(read)

  /** 记一条。重复的提到最前面，不新增一条。 */
  const remember = useCallback((raw) => {
    const q = (raw || '').trim()
    if (!q) return
    setList((prev) => {
      const next = [q, ...prev.filter((x) => x !== q)].slice(0, MAX)
      write(next)
      return next
    })
  }, [])

  const forget = useCallback((raw) => {
    setList((prev) => {
      const next = prev.filter((x) => x !== raw)
      write(next)
      return next
    })
  }, [])

  const clear = useCallback(() => {
    setList([])
    write([])
  }, [])

  /** 按当前输入过滤。空输入时给全部——刚点开框就该看到最近搜过什么。 */
  const match = useCallback((text) => {
    const q = (text || '').trim()
    if (!q) return list
    return list.filter((x) => x !== q && x.includes(q))
  }, [list])

  return { list, remember, forget, clear, match }
}
