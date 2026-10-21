import { useCallback, useEffect, useRef, useState } from 'react'
import { readJSON, scopedKey, writeJSON } from './storage'

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
 * 后端那套「保存的搜索」（saved-searches）解决的是另一件事：带条件的长期订阅，
 * 有新房上架就发站内消息。每条记录上的「提醒我」就是把这一条变成订阅。
 *
 * **「跟着浏览器走」不等于「一台电脑一份」。** 原先这个键不带身份，
 * 同一台电脑换个人登录，看到的是上一个人搜过什么——
 * 找房条件里有预算、有区域、有户型，那是这个人的事。
 * 现在按账号分开存，未登录另算一份，理由见 storage.js。
 *
 * ## 记的是条件，不只是那句话（2026-10-22）
 *
 * 原先只存搜索框里那句话。点一条记录等于把这句话重新交给智能体解析一遍：
 * 要等一趟大模型，而且**同一句话两次解析不一定一样**——上次搜到 8 套，
 * 这次点回来可能是 12 套。用筛选栏缩小过的那些条件也全丢了。
 *
 * 现在每条是 `{ q, filters, total, at }`：恢复时直接套用当时生效的条件，
 * 不再解析。搜完紧接着在筛选栏里收窄的，更新的是这同一条（`refine`），
 * 不会一点一个条件就多出一条记录。旧版存的纯字符串照样能用（按原来的方式重搜）。
 */

const KEY = 'rental_search_history'
/** 最多留 10 条。再多下拉框就得自己滚动，反而不如直接重打一遍。 */
const MAX = 10

function normalize(item) {
  if (typeof item === 'string') return item.trim() ? { q: item.trim() } : null
  if (!item || typeof item !== 'object') return null
  const q = typeof item.q === 'string' ? item.q.trim() : ''
  const filters = item.filters && typeof item.filters === 'object' ? item.filters : null
  if (!q && !filters) return null
  return { q, filters, total: Number.isFinite(item.total) ? item.total : null,
           at: item.at || '' }
}

/** 同一条记录的判据：有那句话就按那句话；纯筛选的按条件本身。 */
function sameEntry(a, b) {
  if (a.q || b.q) return a.q === b.q
  return JSON.stringify(a.filters || {}) === JSON.stringify(b.filters || {})
}

function read(user) {
  // 隐私模式下读不到就当没有记录，不该因此让搜索框不能用
  const list = readJSON(scopedKey(KEY, user), [])
  return Array.isArray(list) ? list.map(normalize).filter(Boolean) : []
}

function write(user, list) {
  // 存不进去也不影响本次搜索，writeJSON 里已经吞掉了
  writeJSON(scopedKey(KEY, user), list)
}

export default function useSearchHistory(user) {
  const [list, setList] = useState(() => read(user))

  // 换账号时重新读一份。
  //
  // **这个 hook 不像 ChatPanel 那样能靠重挂来重置**——它挂在 App 上，
  // 而 App 是不会因为换人就卸载的。所以这里得自己盯着身份变化。
  const who = user?.id ?? 'anon'
  const loadedFor = useRef(who)
  useEffect(() => {
    if (loadedFor.current === who) return
    loadedFor.current = who
    setList(read(user))
  }, [who, user])

  /** 记一条：`remember('一句话', 生效的条件, 结果套数)`。重复的提到最前面。 */
  const remember = useCallback((raw, filters = null, total = null) => {
    const entry = normalize({ q: raw || '', filters, total, at: new Date().toISOString() })
    if (!entry) return
    setList((prev) => {
      const next = [entry, ...prev.filter((x) => !sameEntry(x, entry))].slice(0, MAX)
      write(user, next)
      return next
    })
  }, [user])

  /** 搜完之后在筛选栏里收窄：更新最近那一条的条件，而不是再记一条。 */
  const refine = useCallback((q, filters, total = null) => {
    setList((prev) => {
      if (!prev.length || !q || prev[0].q !== q) return prev
      const next = [{ ...prev[0], filters, total, at: new Date().toISOString() },
                    ...prev.slice(1)]
      write(user, next)
      return next
    })
  }, [user])

  const forget = useCallback((entry) => {
    setList((prev) => {
      const next = prev.filter((x) => !sameEntry(x, entry))
      write(user, next)
      return next
    })
  }, [user])

  const clear = useCallback(() => {
    setList([])
    write(user, [])
  }, [user])

  /** 按当前输入过滤。空输入时给全部——刚点开框就该看到最近搜过什么。 */
  const match = useCallback((text) => {
    const q = (text || '').trim()
    if (!q) return list
    return list.filter((x) => x.q !== q && x.q.includes(q))
  }, [list])

  return { list, remember, refine, forget, clear, match }
}
