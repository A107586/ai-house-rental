import { useCallback, useEffect, useState } from 'react'

/**
 * 极简哈希路由（待完善清单 C-3）。
 *
 * 原先整站只有一个 URL：房源详情**分享不出去**（发给朋友只能打开首页）、
 * 浏览器前进后退不可用、刷新回到首页。
 *
 * **为什么不用 react-router。** 本该用，但这台机器 npm 装不上任何包
 * （网络到不了 registry）。而这个项目真正需要的只有「地址栏能表达当前
 * 看的是哪套房」这一件事，原生 History API 六十行就够——引一个路由库
 * 反而要把整棵组件树改成路由结构，代价远大于收益。
 *
 * **为什么用 hash 而不是 path。** 后端把 `web/dist` 挂在 `/` 上，
 * 访问 `/house/DL0001` 会被当成静态文件去找，直接 404；要支持 path
 * 路由就得在后端加一条「所有未匹配路径都返回 index.html」的兜底，
 * 那会和现有的静态挂载打架。hash 部分不发给服务器，零改动。
 *
 * 代价是 URL 里多一个 `#`，且搜索引擎对 hash 路由的收录不如 path——
 * 后者是真实平台的主要获客渠道，所以**这只是权宜之计**，
 * 联网之后应当换成 react-router + 后端兜底路由。
 */

function readHash() {
  const raw = window.location.hash.replace(/^#\/?/, '')
  if (!raw) return { name: 'home', param: '' }
  const [name, param = ''] = raw.split('/')
  return { name, param: decodeURIComponent(param) }
}

export default function useHashRoute() {
  const [route, setRoute] = useState(readHash)

  useEffect(() => {
    // 前进后退、以及用户直接改地址栏，都靠这个事件同步
    const onChange = () => setRoute(readHash())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  /** 跳到某个视图。`replace` 为真时不留历史记录。 */
  const go = useCallback((name, param = '', replace = false) => {
    const next = name === 'home'
      ? '#/'
      : `#/${name}/${encodeURIComponent(param)}`
    if (window.location.hash === next) return
    if (replace) {
      window.history.replaceState(null, '', next)
      setRoute(readHash())
    } else {
      // 直接改 hash 会触发 hashchange，状态由上面的监听器统一更新，
      // 不在这里 setRoute，免得同一次跳转更新两遍
      window.location.hash = next
    }
  }, [])

  /** 回到上一页。没有历史可回时退到首页——
   *  直接从分享链接进来的用户按「关闭」不该被弹出整个站点。 */
  const back = useCallback(() => {
    if (window.history.length > 1) window.history.back()
    else go('home', '', true)
  }, [go])

  return { route, go, back }
}
