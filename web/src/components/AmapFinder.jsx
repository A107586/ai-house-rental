import { useEffect, useRef, useState } from 'react'
import AMapLoader from '@amap/amap-jsapi-loader'

import { api } from '../api'
import OfflineMapFinder from './MapFinder'

const AMAP_KEY = import.meta.env.VITE_AMAP_KEY || ''
const AMAP_SECURITY_CODE = import.meta.env.VITE_AMAP_SECURITY_JS_CODE || ''
const DALIAN_CENTER = [121.614682, 38.914003]

// 多久没画出来就当「连不上」，换成离线区域图。
//
// **原先只在脚本加载报错时才换**，而连不上高德的常见样子不是报错，是**卡住**：
// 网络被拦、外网很慢时 `AMapLoader.load` 一直不返回；Key 或安全密钥填错时
// 脚本能加载、底图却永远画不出来。两种情况都既不成功也不失败，
// 地图那一块就一直是白的——比离线图糟得多，离线图至少能点能筛。
// 所以以底图的 `complete` 事件为准：画出来了才算接上。
// 正常网络下高德 1~3 秒就画完，8 秒留足了余量。
const LOAD_TIMEOUT_MS = 8000

export default function AmapFinder({ selected, onPick, filters = {}, onOpen }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const amapRef = useRef(null)
  const markersRef = useRef([])
  const infoWindowRef = useRef(null)
  const filtersRef = useRef(filters)
  const refreshRef = useRef(null)
  const onOpenRef = useRef(onOpen)
  const [ready, setReady] = useState(false)
  const [fallback, setFallback] = useState(!AMAP_KEY)
  const [error, setError] = useState('')
  const [visibleTotal, setVisibleTotal] = useState(0)

  filtersRef.current = filters
  onOpenRef.current = onOpen

  useEffect(() => {
    if (!AMAP_KEY || !containerRef.current) return undefined
    let alive = true
    let drawn = false

    // 看门狗：到点还没画出底图，就认定连不上，拆掉半成品换离线图
    const giveUp = window.setTimeout(() => {
      if (!alive || drawn) return
      if (mapRef.current) mapRef.current.destroy()
      mapRef.current = null
      setFallback(true)
    }, LOAD_TIMEOUT_MS)

    window._AMapSecurityConfig = { securityJsCode: AMAP_SECURITY_CODE }
    AMapLoader.load({
      key: AMAP_KEY,
      version: '2.0',
      plugins: ['AMap.Scale', 'AMap.ToolBar'],
    })
      .then((AMap) => {
        if (!alive || !containerRef.current) return
        amapRef.current = AMap
        const map = new AMap.Map(containerRef.current, {
          center: DALIAN_CENTER,
          zoom: 11,
          viewMode: '2D',
          resizeEnable: true,
        })
        map.addControl(new AMap.Scale())
        map.addControl(new AMap.ToolBar({ position: 'RB' }))
        mapRef.current = map
        // 底图真的画出来了，才算接上——见 LOAD_TIMEOUT_MS 的说明
        map.on('complete', () => {
          drawn = true
          window.clearTimeout(giveUp)
        })

        refreshRef.current = async ({ useBounds = true, fitView = false } = {}) => {
          const params = { ...filtersRef.current }
          if (useBounds) {
            const bounds = map.getBounds()
            const southWest = bounds.getSouthWest()
            const northEast = bounds.getNorthEast()
            Object.assign(params, {
              west: southWest.lng,
              south: southWest.lat,
              east: northEast.lng,
              north: northEast.lat,
            })
          }
          try {
            const data = await api.mapHouses(params)
            if (!alive) return
            const houses = data.list || []
            renderMarkers(AMap, map, houses, markersRef,
              infoWindowRef, onOpenRef)
            setVisibleTotal(data.total || 0)
            setError('')
            if (fitView && houses.length) {
              map.setFitView(markersRef.current, false, [60, 60, 60, 60], 14)
            }
          } catch (err) {
            if (alive) setError(err.message)
          }
        }

        const refreshVisible = () => refreshRef.current?.({ useBounds: true })
        map.on('moveend', refreshVisible)
        map.on('zoomend', refreshVisible)
        setReady(true)
        refreshRef.current({ useBounds: false, fitView: true })
      })
      .catch(() => {
        window.clearTimeout(giveUp)
        if (alive) setFallback(true)
      })

    return () => {
      alive = false
      window.clearTimeout(giveUp)
      if (mapRef.current) mapRef.current.destroy()
      mapRef.current = null
    }
  }, [])

  useEffect(() => {
    if (ready) refreshRef.current?.({ useBounds: false, fitView: true })
  }, [ready, filters])

  if (fallback) {
    // C3：不再挂那条「真实地图暂时无法加载，已切换到离线区域图」。
    //
    // 它讲的是**我们的实现细节**，不是用户需要处理的事：区域图照样能
    // 点、能筛，功能一点没少。摆一条黄字提醒，等于每次进来都跟用户说
    // 一句「这里有点问题」——而他什么也做不了，只会平白觉得这个站不靠谱。
    // 真正需要知道这件事的是我们自己，那属于日志，不属于界面。
    return <OfflineMapFinder selected={selected} onPick={onPick} />
  }

  return (
    <section className="amap-finder">
      <div className="map-head">
        <h3>地图找房</h3>
        <span className="map-tip">拖动或缩放地图，查看当前区域房源</span>
        <div className="map-legend">
          <span><i className="match" />筛选命中 {visibleTotal} 套</span>
        </div>
        {selected && (
          <button className="btn ghost small" onClick={() => onPick('')}>
            清除区域：{selected}
          </button>
        )}
      </div>
      {error && <div className="reply failed">{error}</div>}
      <div className="amap-stage">
        <div ref={containerRef} className="amap-container" />
        {!ready && <div className="amap-loading">正在加载高德地图…</div>}
      </div>
      <div className="map-foot">
        地图只显示符合当前条件的房源。点红色价格标签，看详情。
      </div>
    </section>
  )
}

function renderMarkers(AMap, map, houses, markersRef,
  infoWindowRef, onOpenRef) {
  if (markersRef.current.length) map.remove(markersRef.current)

  const markers = houses.map((house) => {
    const price = document.createElement('button')
    price.type = 'button'
    price.className = 'map-price-marker match'
    price.textContent = `¥${Math.round(house.rent_price)}`
    price.title = `${house.community} ${house.layout} ${house.rent_price}元/月`

    const marker = new AMap.Marker({
      position: [house.lng, house.lat],
      content: price,
      anchor: 'bottom-center',
      offset: new AMap.Pixel(0, -2),
      title: house.title,
      extData: house,
    })
    price.addEventListener('click', (event) => {
      event.stopPropagation()
      openHouseInfo(AMap, map, marker, house, infoWindowRef, onOpenRef)
    })
    return marker
  })
  markersRef.current = markers
  if (markers.length) map.add(markers)
}

function openHouseInfo(AMap, map, marker, house, infoWindowRef, onOpenRef) {
  if (infoWindowRef.current) infoWindowRef.current.close()

  const card = document.createElement('div')
  card.className = 'map-house-popup'
  const title = document.createElement('strong')
  title.textContent = house.title
  const meta = document.createElement('span')
  meta.textContent = `${house.community} · ${house.layout} · ${house.area}㎡`
  const relation = document.createElement('em')
  relation.className = 'match'
  relation.textContent = '符合当前条件'
  const rent = document.createElement('b')
  rent.textContent = `¥${Math.round(house.rent_price)}/月`
  const detail = document.createElement('button')
  detail.type = 'button'
  detail.className = 'btn small'
  detail.textContent = '查看房源详情'
  detail.addEventListener('click', () => onOpenRef.current?.(house))
  card.append(title, relation, meta, rent, detail)

  const infoWindow = new AMap.InfoWindow({
    content: card,
    offset: new AMap.Pixel(0, -34),
    closeWhenClickMap: true,
  })
  infoWindow.open(map, marker.getPosition())
  infoWindowRef.current = infoWindow
}
