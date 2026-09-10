import { useEffect } from 'react'

/**
 * 抽屉/弹窗打开时锁住背景页的滚动。
 *
 * **为什么要锁。** 不锁的话，详情抽屉开着，鼠标滚轮一转背景整页跟着动——
 * 用户以为自己在翻详情，实际翻的是后面的列表；关掉抽屉才发现
 * 已经滚到不知哪里去了。这就是那句「点开详情页后页面还能滚动」。
 *
 * **为什么要补偿滚动条宽度。** 直接 `overflow: hidden` 会让滚动条消失，
 * 可用宽度突然多出十几个像素，整页内容向右一跳。这一跳在打开和关闭时
 * 各来一次，比不锁还难受——「点了还会动」多半指的就是它。
 * 所以锁的同时给 body 补上一条等宽的右内边距，视觉上纹丝不动。
 *
 * 计数式加锁：详情抽屉里再打开一个弹窗时，内层关掉不能把外层的锁
 * 一起解了。所以记的是「有几个东西要求锁」，减到 0 才真正解锁。
 */

let locks = 0
let savedBody = ''
let savedHtml = ''

function lock() {
  locks += 1
  if (locks > 1) return
  // 滚动条占了多少宽度。没有滚动条时（内容短、或用的是覆盖式滚动条）
  // 这个差值是 0，也就不需要补偿
  const gap = window.innerWidth - document.documentElement.clientWidth
  const html = document.documentElement

  savedBody = document.body.style.cssText
  savedHtml = html.style.cssText

  // **html 和 body 都要锁。**
  // 只锁 body 是不够的：这个页面的滚动容器是 html（document scrolling
  // element），body 上的 overflow:hidden 拦不住它。实测只锁 body 时，
  // 抽屉开着滚一下，背景照样从 900 滚到 1300。
  html.style.overflow = 'hidden'
  document.body.style.overflow = 'hidden'
  if (gap > 0) document.body.style.paddingRight = gap + 'px'
}

function unlock() {
  locks = Math.max(0, locks - 1)
  if (locks > 0) return
  // 恢复成加锁前的原样，而不是抹成空字符串——
  // 这两个元素上可能有别处设的内联样式，不该被这里顺手清掉
  document.body.style.cssText = savedBody
  document.documentElement.style.cssText = savedHtml
  savedBody = savedHtml = ''
}

export default function useScrollLock(active) {
  useEffect(() => {
    if (!active) return
    lock()
    return unlock
  }, [active])
}
