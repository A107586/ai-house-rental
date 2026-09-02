import { useCallback, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api'
import AdminPanel from './components/AdminPanel'
import AuthDialog from './components/AuthDialog'
import ChatPanel from './components/ChatPanel'
import CompareBar from './components/CompareBar'
import FilterBar from './components/FilterBar'
import HouseCard from './components/HouseCard'
import HouseDrawer from './components/HouseDrawer'
import LandlordPanel from './components/LandlordPanel'
import MapFinder from './components/MapFinder'
import MyPanel from './components/MyPanel'
import RecommendStrip from './components/RecommendStrip'

const EXAMPLES = [
  '中山区两室一厅，租金三千以内，最好带电梯',
  '沙河口 合租 单间 1500以下',
  '甘井子区 80平以上的三室 预算不超过五千',
  '有没有金州区2000到3000的一居室',
]

// 筛选条件字段 → 展示文案
const FILTER_LABELS = {
  suozaidiqu: (v) => `区域：${v}`,
  fangwuhuxing: (v) => `户型：${v}`,
  fangyuanleixing: (v) => `类型：${v}`,
  zujia_min: (v) => `租价 ≥ ${v} 元`,
  zujia_max: (v) => `租价 ≤ ${v} 元`,
  mianji_min: (v) => `面积 ≥ ${v} 平`,
  mianji_max: (v) => `面积 ≤ ${v} 平`,
}

const SOURCE_LABEL = {
  rules: '规则解析，未调用大模型',
  llm: '大模型解析',
  rules_fallback: '大模型不可用，已降级为规则解析',
}

const PAGE_SIZE = 12

export default function App() {
  const [text, setText] = useState('')
  const [result, setResult] = useState(null)
  const [houses, setHouses] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)
  const [online, setOnline] = useState(null)

  const [user, setUser] = useState(null)
  const [showAuth, setShowAuth] = useState(false)
  const [showMine, setShowMine] = useState(false)
  const [showWork, setShowWork] = useState(false)
  const [favorites, setFavorites] = useState([])
  const [filters, setFilters] = useState({})
  const [compare, setCompare] = useState([])
  const [showChat, setShowChat] = useState(false)
  const [showMap, setShowMap] = useState(false)

  const loadFavorites = useCallback(async () => {
    if (!getToken()) {
      setFavorites([])
      return
    }
    try {
      const data = await api.listFavorites()
      setFavorites((data.list || []).map((h) => h.house_no))
    } catch {
      setFavorites([])
    }
  }, [])

  // 首屏：恢复登录状态并展示最新房源
  useEffect(() => {
    if (getToken()) {
      api
        .me()
        .then((data) => {
          setUser(data)
          loadFavorites()
        })
        .catch(() => setUser(null))
    }
    loadPage(1, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function loadPage(nextPage, replace, override) {
    const active = override ?? filters
    try {
      const data = await api.listHouses({
        ...active, limit: PAGE_SIZE, page: nextPage,
      })
      setHouses((prev) => (replace ? data.list : [...prev, ...data.list]))
      setTotal(data.total || 0)
      setPage(nextPage)
      setOnline(true)
      setError('')
    } catch (err) {
      setError(err.message)
      setOnline(false)
    }
  }

  async function search(e, query) {
    if (e) e.preventDefault()
    const content = (query ?? text).trim()
    if (!content || busy) return

    setBusy(true)
    setError('')
    try {
      const data = await api.agentSearch(content, 50)
      setResult(data)
      setHouses(data.items || [])
      setTotal(data.total || 0)
      setOnline(true)
    } catch (err) {
      setError(err.message)
      setResult(null)
      setOnline(false)
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setResult(null)
    setText('')
    setFilters({})
    loadPage(1, true, {})
  }

  // 地图选区与条件筛选共用同一套 filters，两者不是两条独立的检索路径，
  // 否则「地图选了中山区」和「筛选选了两室一厅」会互相覆盖
  function pickDistrict(district) {
    const next = { ...filters, district }
    if (!district) delete next.district
    setResult(null)
    setText('')
    setFilters(next)
    loadPage(1, true, next)
  }

  function applyFilters(next) {
    // 用筛选器就退出智能搜索结果，两者是互斥的两种检索方式
    setResult(null)
    setText('')
    setFilters(next)
    loadPage(1, true, next)
  }

  async function toggleFavorite(houseNo) {
    if (!user) {
      setShowAuth(true)
      return
    }
    try {
      if (favorites.includes(houseNo)) {
        await api.removeFavorite(houseNo)
        setFavorites(favorites.filter((n) => n !== houseNo))
      } else {
        await api.addFavorite(houseNo)
        setFavorites([...favorites, houseNo])
      }
    } catch (err) {
      setError(err.message)
    }
  }

  function toggleCompare(house) {
    setCompare((prev) => {
      if (prev.some((h) => h.house_no === house.house_no)) {
        return prev.filter((h) => h.house_no !== house.house_no)
      }
      // 超过四套就看不过来了，横向表格也会挤成一团
      if (prev.length >= 4) return prev
      return [...prev, house]
    })
  }

  function logout() {
    setToken('')
    setUser(null)
    setFavorites([])
    setShowMine(false)
    setShowWork(false)
  }

  const filterChips = result
    ? Object.entries(result.filters || {})
        .filter(([key]) => FILTER_LABELS[key])
        .map(([key, value]) => FILTER_LABELS[key](value))
    : []

  const hasMore = !result && houses.length < total

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="logo">
            大连<span>智租</span>
          </div>
          <div className="tagline">基于 AI 智能体的房屋租赁平台</div>

          <div className="topbar-right">
            <span className="status-dot">
              <span
                className={`dot ${online === true ? 'up' : online === false ? 'down' : ''}`}
              />
              {online === true ? '服务正常' : online === false ? '服务未连接' : '检查中'}
            </span>

            {user ? (
              <>
                {(user.role === 'landlord' || user.role === 'admin') && (
                  <button className="btn ghost small" onClick={() => setShowWork(true)}>
                    {user.role === 'admin' ? '管理后台' : '工作台'}
                  </button>
                )}
                <button className="btn ghost small" onClick={() => setShowMine(true)}>
                  {user.real_name || user.username}
                </button>
                <button className="btn ghost small" onClick={logout}>
                  退出
                </button>
              </>
            ) : (
              <button className="btn small" onClick={() => setShowAuth(true)}>
                登录
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="page">
        <section className="search-card">
          <h2>说一句话，帮你找房</h2>
          <p className="hint">
            不用逐个下拉框筛选，直接描述你的需求，智能体会自动解析成筛选条件。
          </p>

          <form className="search-row" onSubmit={search}>
            <input
              placeholder="例如：中山区两室一厅，租金三千以内，最好带电梯"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn" type="submit" disabled={busy || !text.trim()}>
              {busy ? '思考中…' : '智能找房'}
            </button>
          </form>

          <div className="examples">
            <span>试试：</span>
            {EXAMPLES.map((sample) => (
              <button
                key={sample}
                className="chip-btn"
                onClick={() => {
                  setText(sample)
                  search(null, sample)
                }}
              >
                {sample}
              </button>
            ))}
          </div>

          {result && (filterChips.length > 0 || result.features?.length > 0) && (
            <div className="parsed">
              <div className="parsed-title">
                智能体解析出的条件
                {SOURCE_LABEL[result.source] ? `（${SOURCE_LABEL[result.source]}）` : ''}
              </div>
              <div className="chips">
                {filterChips.map((label) => (
                  <span className="chip" key={label}>
                    {label}
                  </span>
                ))}
                {(result.features || []).map((feature) => (
                  <span className="chip feature" key={feature}>
                    偏好：{feature}
                  </span>
                ))}
              </div>
            </div>
          )}

          {result?.message && (
            <div className={`answer ${result.ok ? '' : 'error'}`}>{result.message}</div>
          )}

          {error && <div className="answer error">{error}</div>}
        </section>

        {!result && (
          <RecommendStrip
            user={user}
            favorites={favorites}
            onToggleFavorite={toggleFavorite}
            onOpen={setSelected}
          />
        )}

        {!result && (
          <div className="find-switch">
            <button
              className={`tab ${showMap ? 'active' : ''}`}
              onClick={() => setShowMap(!showMap)}
            >
              {showMap ? '收起地图' : '地图找房'}
            </button>
            {filters.district && (
              <span className="chip">已选区域：{filters.district}</span>
            )}
          </div>
        )}

        {!result && showMap && (
          <MapFinder selected={filters.district || ''} onPick={pickDistrict} />
        )}

        {!result && <FilterBar onApply={applyFilters} disabled={busy} />}

        <div className="list-head">
          <h3>
            {result ? '搜索结果'
              : Object.values(filters).some(Boolean) ? '筛选结果' : '最新房源'}
          </h3>
          <span className="count">共 {total} 套</span>
          {result && (
            <button className="btn ghost small" onClick={reset}>
              重置
            </button>
          )}
        </div>

        {houses.length === 0 ? (
          <div className="empty">
            {error ? '无法加载房源，请确认后端服务已启动。' : '没有符合条件的房源，换个说法试试。'}
          </div>
        ) : (
          <>
            <div className="grid">
              {houses.map((house) => (
                <HouseCard
                  key={house.house_no}
                  house={house}
                  favorited={favorites.includes(house.house_no)}
                  onToggleFavorite={toggleFavorite}
                  compared={compare.some((h) => h.house_no === house.house_no)}
                  onToggleCompare={toggleCompare}
                  onClick={setSelected}
                />
              ))}
            </div>

            {hasMore && (
              <div className="load-more">
                <button className="btn ghost" onClick={() => loadPage(page + 1, false)}>
                  加载更多（已显示 {houses.length} / {total}）
                </button>
              </div>
            )}
          </>
        )}
      </main>

      {/* 悬浮入口。对比栏在底部居中，这个放右下角，两者不重叠 */}
      <button
        className="chat-fab"
        onClick={() => setShowChat(true)}
        title="打开租房助手"
      >
        <span className="fab-dot" />
        租房助手
      </button>

      {showChat && (
        <ChatPanel
          user={user}
          onOpenHouse={(house) => {
            setShowChat(false)
            setSelected(house)
          }}
          onClose={() => setShowChat(false)}
        />
      )}

      <CompareBar
        items={compare}
        onRemove={(no) => setCompare(compare.filter((h) => h.house_no !== no))}
        onClear={() => setCompare([])}
      />

      {selected && (
        <HouseDrawer
          house={selected}
          user={user}
          favorited={favorites.includes(selected.house_no)}
          onToggleFavorite={toggleFavorite}
          onClose={() => setSelected(null)}
        />
      )}

      {showAuth && (
        <AuthDialog
          onClose={() => setShowAuth(false)}
          onLoggedIn={(u) => {
            setUser(u)
            setShowAuth(false)
            loadFavorites()
          }}
        />
      )}

      {showWork && user?.role === 'landlord' && (
        <LandlordPanel user={user} onClose={() => setShowWork(false)} />
      )}

      {showWork && user?.role === 'admin' && (
        <AdminPanel user={user} onClose={() => setShowWork(false)} />
      )}

      {showMine && user && (
        <MyPanel
          user={user}
          onUserUpdated={setUser}
          onLogout={logout}
          onClose={() => setShowMine(false)}
          onOpenHouse={(house) => {
            setShowMine(false)
            setSelected(house)
          }}
        />
      )}
    </>
  )
}
