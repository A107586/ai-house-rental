import { useEffect, useState } from 'react'

/**
 * 动效用的三个小 hook。
 *
 * **这三个都只决定「什么时候挂载」和「挂哪个 class」，不碰任何业务状态。**
 * 单独成文件而不是塞进各组件：退场延迟和「超过多久才显示转圈」这两件事
 * 一旦各写各的，时长就会不一致——和 styles.css 里把 --dur 统一定在一处
 * 是同一个理由。
 *
 * 为什么不引 motion / framer-motion：这里真正需要库的只有列表重排的
 * FLIP 位移，而房源筛选那一路已经改成「容器整体淡入」，不需要逐卡算位移。
 * 剩下的退场和延迟显示合起来不到 60 行，为这点东西往 209KB 的主包里
 * 再加一个动画引擎不值。
 */

/** 退场动画的默认时长，和 theme.css 里 panelOut / bubbleOut 的 0.16s 对齐。
 *  改这里要同时改那边，否则会出现「class 已经撤了但动画还没播完」。 */
const EXIT_MS = 160

/** 当前环境是不是要求减少动态效果。
 *  读不到 matchMedia（旧环境、测试里的 jsdom）就按「不减少」处理。 */
function prefersReduced() {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * 给「条件渲染的东西」补一个退场。
 *
 * 原来 `{open && <Dialog />}` 这种写法，open 一变 false 元素立刻从 DOM 里
 * 消失——进场浮起 12px，退场却是啪地没了。**CSS 单独做不到退场**，
 * 因为动画要播的时候元素已经不在了。
 *
 * 用法：
 *   const { mounted, closing } = useExitTransition(open)
 *   return mounted ? <div className={closing ? 'dialog is-closing' : 'dialog'} /> : null
 *
 * 减少动态效果时直接跳过延迟——那类用户要的是立刻结束，不是放慢。
 *
 * @param open  外部的开关状态（只读，不会被改写）
 * @param ms    退场动画时长，默认 160ms
 * @returns {{mounted: boolean, closing: boolean}}
 */
export function useExitTransition(open, ms = EXIT_MS) {
  const [mounted, setMounted] = useState(open)
  const [closing, setClosing] = useState(false)

  useEffect(() => {
    if (open) {
      setMounted(true)
      setClosing(false)
      return undefined
    }
    if (!mounted) return undefined
    if (prefersReduced()) {
      setMounted(false)
      return undefined
    }
    setClosing(true)
    const timer = setTimeout(() => {
      setMounted(false)
      setClosing(false)
    }, ms)
    // 退场没播完就又被打开（连点两下），清掉定时器，
    // 否则上一轮的 setMounted(false) 会把刚打开的那个关掉
    return () => clearTimeout(timer)
  }, [open, mounted, ms])

  return { mounted, closing }
}

/**
 * 「持续超过 delay 毫秒才算真的在忙」。
 *
 * 为什么要这个：请求快的时候（本地 80ms 回来）转圈会闪一下就消失，
 * **闪一下比完全不转更让人心慌**——用户会以为自己看错了，或者以为
 * 出了什么错。所以 400ms 以内的请求一律不显示转圈，灰掉按钮就够了。
 *
 * 400ms 这个数来自 Nielsen 那条老结论：0.1s 以内算即时，1s 以内用户
 * 不会走神。取中间值，既躲开了本地快请求，也赶在用户起疑之前出现。
 *
 * @param on     外部的忙碌状态
 * @param delay  要持续多久才返回 true，默认 400ms
 */
export function useDelayedFlag(on, delay = 400) {
  const [late, setLate] = useState(false)

  useEffect(() => {
    if (!on) {
      setLate(false)
      return undefined
    }
    const timer = setTimeout(() => setLate(true), delay)
    return () => clearTimeout(timer)
  }, [on, delay])

  return late
}

/**
 * 列表进场该不该逐个错开。
 *
 * 要区分两种进场：
 *   - 刚进这个列表视图 / 骨架屏换成真内容 —— 保留 40ms 错开，读起来像依次出现
 *   - 之后筛选、翻页、排序换来的批次 —— 掐掉错开，改成整体快淡入
 *
 * 为什么要分：错开 8 张就是 280ms 延迟叠 360ms 动画，接近 0.65 秒。
 * 刚进来时没有参照物，这 0.65 秒读起来是「内容正在到位」；但调价格区间
 * 那种操作是连着做好几次的，每次都要等 0.65 秒重排完才能看清结果，
 * **就没法把改之前和改之后放在一起比**——那恰好是筛选唯一的用途。
 *
 * ## 为什么按时间窗判定，而不是数「第几批数据」
 *
 * 数批次试过，在这个应用里必然算错，原因有两条实测出来的：
 *
 * 1. `App.jsx` 在首页路由就会 `loadPage(1, true, {})`（为了把大图上的
 *    「在架房源」总数还原成全站套数）。所以点进「全部房源」时 houses
 *    已经填过一次了，那一批会被当成第二批，错开被误掐。
 * 2. 更要紧的：**改 class 会打断正在播的动画。** 列表视图自己的那次取数
 *    紧跟着到（同样的条件、同一批 house_no），class 从错开切到快淡入时，
 *    0.65 秒的错开才跑了一半，后面几张直接闪现——比两种都不做更难看。
 *
 * 时间窗绕开了这两件事：进一个视图就给固定一段「允许错开」的窗口，
 * 窗口内不管取数几次都不改 class，窗口过了再换。
 * 700ms = 错开 280ms + 动画 360ms 再留一点余量。
 *
 * ## viewKey 传什么
 *
 * 「换了一个列表视图」的标志，在 App 里传 listKind 正好：
 * 筛选和翻页不改它，换列表（全部 / 最新 / 优选 / 搜索结果）才改。
 *
 * ## 为什么在渲染期间 setState
 *
 * class 必须在新视图的**首帧**就定下来。放进 useEffect 会晚一帧——
 * 卡片已经带着上一个视图留下的 class 开播了。
 *
 * 渲染期间按「prop 变了就调整 state」改状态是 React 文档认可的写法：
 * React 会立刻丢弃这次渲染结果重算，不会多绘制一帧。
 * 对 StrictMode 也安全——第二遍 `key === viewKey` 已成立，不再 set。
 *
 * @param viewKey 换了它就重新开一个错开窗口
 * @param ms      窗口多长，默认 700ms
 */
export function useEntranceStagger(viewKey = '', ms = 700) {
  const [key, setKey] = useState(viewKey)
  const [stagger, setStagger] = useState(true)

  if (key !== viewKey) {
    setKey(viewKey)
    setStagger(true)
  }

  useEffect(() => {
    const timer = setTimeout(() => setStagger(false), ms)
    return () => clearTimeout(timer)
  }, [key, ms])

  return stagger
}
