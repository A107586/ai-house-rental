import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api'
import useHashRoute from './useHashRoute'

// 代码分割：整包原先 303 KB，管理后台、房东工作台这些**租客永远用不到的
// 面板**也一起下载，而租客占访问量的绝大多数。
//
// 拆分的判据是「租客首次打开页面会不会立刻看到它」——首屏要的（房源卡片、
// 筛选栏、公告、推荐）保持静态引入，点开才用的抽屉与面板按需加载。
//
// 每块外面都要包 Suspense，否则加载那一瞬间会抛错。fallback 给的是空节点
// 而不是转圈：这些块只有几十 KB，本地几乎瞬间到位，闪一下加载动画
// 反而更晃眼。
const AdminPanel = lazy(() => import('./components/AdminPanel'))
const AgentPanel = lazy(() => import('./components/AgentPanel'))
const LandlordPanel = lazy(() => import('./components/LandlordPanel'))
const MyPanel = lazy(() => import('./components/MyPanel'))
const HouseDrawer = lazy(() => import('./components/HouseDrawer'))
const ChatPanel = lazy(() => import('./components/ChatPanel'))
const ChatWithLandlord = lazy(() => import('./components/ChatWithLandlord'))
// 真实地图（高德）。没配 Key 或加载失败时它内部会退回
// 离线的行政区示意图（MapFinder），所以这里只引这一个
const MapFinder = lazy(() => import('./components/AmapFinder'))
const AuthDialog = lazy(() => import('./components/AuthDialog'))
import ErrorBoundary from './components/ErrorBoundary'
import ActiveFilters from './components/ActiveFilters'
import CompareBar from './components/CompareBar'
import FilterBar from './components/FilterBar'
import HouseCard from './components/HouseCard'
import NoticeBar from './components/NoticeBar'
import RecommendStrip from './components/RecommendStrip'

const EXAMPLES = [
  '中山区两室一厅，租金三千以内，最好带电梯',
  '沙河口 合租 单间 1500以下',
  '甘井子区 80平以上的三室 预算不超过五千',
  '有没有金州区2000到3000的一居室',
]

// 智能体的业务语义字段 → 后端 /api/houses 的参数名。
//
// **这张表是 K-06 的枢纽。** 智能体从此只做一件事：把人话翻译成条件。
// 翻译完的结果经这里映射成筛选参数，和用户手点筛选栏产生的参数
// 完全同构，之后统一走 /api/houses 一条路。
//
// 这样换来三件事：
//   1. 智能体不可用时，找房只是少了个便利入口，不是整站瘫痪；
//   2. AI 认出的条件可以继续用筛选栏改，两者不再互斥；
//   3. 条件看得见——用户知道系统「以为」他要什么。
const AI_FIELD_MAP = {
  suozaidiqu: 'district',
  fangwuhuxing: 'layout',
  fangyuanleixing: 'rent_type',
  zujia_min: 'rent_price_min',
  zujia_max: 'rent_price_max',
  mianji_min: 'area_min',
  mianji_max: 'area_max',
}

/** 把 /agent/parse 的结果翻成 /api/houses 的查询参数。 */
function toFilters(parsed) {
  const next = {}
  Object.entries(parsed.filters || {}).forEach(([key, value]) => {
    const mapped = AI_FIELD_MAP[key]
    if (mapped && value !== '' && value !== null && value !== undefined) {
      next[mapped] = value
    }
  })
  // 设施偏好交给后端按整词匹配。以前是取回来在前端比子串的，
  // 「无电梯」里含「电梯」，要电梯的人会被推一套没电梯的房
  const features = (parsed.features || []).filter(Boolean)
  if (features.length) next.features = features.join(',')
  return next
}

// 解析来源。fused 是当前的正常路径：规则先解析一遍，结论作为参考
// 交给大模型，由它给出最终结果——两者同时参与，不是二选一。
const SOURCE_LABEL = {
  rules: '规则解析，未配置大模型',
  fused: '规则 + 大模型融合',
  llm: '大模型解析',
  rules_fallback: '大模型输出不可用，已退回规则结果',
}

const PAGE_SIZE = 12

export default function App() {
  const [text, setText] = useState('')
  // 智能体的解析结论。**它不再持有房源列表**——列表只有一份，
  // 由 filters 决定（K-06）。这里留的是「它听懂了什么、是怎么听懂的」，
  // 用来在页面上向用户解释，答辩时也能展示规则与大模型的分工。
  const [parsed, setParsed] = useState(null)
  const [keyword, setKeyword] = useState('')
  // K-09 保存条件的按钮状态：'' | 'saving' | 'saved'
  const [saveState, setSaveState] = useState('')
  const [houses, setHouses] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)
  // C-3：地址栏要能表达「现在看的是哪套房」，否则详情分享不出去、
  // 刷新回首页、前进后退也不可用
  const { route, go, back } = useHashRoute()
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
  const [showTalk, setShowTalk] = useState(false)
  const [talkId, setTalkId] = useState(0)
  // 已经搜过的那句话。翻页要用它重新请求，
  // 不能用输入框里的当前内容——用户可能已经改了半句还没提交
  const [searched, setSearched] = useState('')
  // M-09 公开配置。取不到就用内置文案，配置服务不该拦住首页。
  const [site, setSite] = useState({ site_name: '大连智租', contact_phone: '' })

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

  // URL 决定开哪套房的详情，而不是反过来。
  //
  // 这样三件事同时成立：分享链接能直接打开那套房、刷新不丢、
  // 浏览器前进后退可用。任何一处代码想打开详情，改 URL 即可。
  useEffect(() => {
    if (route.name !== 'house' || !route.param) {
      setSelected(null)
      return
    }
    // 已经是这套就不用再取一次——避免前进后退时反复请求
    if (selected?.house_no === route.param) return
    let alive = true
    api
      .getHouse(route.param)
      .then((house) => alive && setSelected(house))
      .catch(() => {
        if (!alive) return
        // 链接里的房号已下架或不存在。退回首页而不是留一个空抽屉，
        // 从分享链接进来的人至少还能看到别的房子
        setError('这套房源已下架或不存在。')
        go('home', '', true)
      })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.name, route.param])

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
    api.publicConfig().then(setSite).catch(() => {})
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

  /** 智能找房：**只解析，不取数**（K-06）。
   *
   * 解析出来的条件合并进 filters，随后和手点筛选栏走同一条检索路径。
   * 智能体不可用时这里会给出提示，但搜索框和筛选栏照常能用——
   * 它从「唯一入口」降级成了「一个便利入口」。
   */
  async function search(e, query) {
    if (e) e.preventDefault()
    const content = (query ?? text).trim()
    if (!content || busy) return

    setBusy(true)
    setError('')
    try {
      const parsed = await api.agentParse(content)
      setSearched(content)
      setParsed(parsed)
      if (!parsed.ok) {
        // 没听懂就别动用户已经设好的条件。把筛选清空再告诉他
        // 「没听懂」，等于一句话毁掉他刚调好的筛选
        setBusy(false)
        return
      }
      // 覆盖式合并：这一句话表达的是一次新的检索意图，
      // 和上一句的条件叠加只会得到一个谁也没要过的结果
      const next = toFilters(parsed)
      setFilters(next)
      await loadPage(1, true, next)
    } catch (err) {
      // 智能体不通。**不要清空列表**——用户还能用搜索框和筛选栏，
      // 把房源列表也一并清掉才是真的没法用了
      setParsed(null)
      setError('智能体暂时联系不上，可以直接用上面的搜索框或下面的筛选条件找房。')
    } finally {
      setBusy(false)
    }
  }

  /** 关键词检索。这条路径**完全不经过智能体**，是找房的保底入口。 */
  function searchKeyword(e) {
    if (e) e.preventDefault()
    const next = { ...filters, keyword: keyword.trim() }
    if (!next.keyword) delete next.keyword
    setParsed(null)
    setFilters(next)
    loadPage(1, true, next)
  }

  /** 把当前这组条件存进「我的搜索」（K-09）。
   *
   * 存的是 filters 本身，不是那句话——用户之后要的是「按这组条件再找一次」，
   * 而条件已经是结构化的了，没必要再让智能体翻译一遍。
   */
  async function saveCurrentSearch() {
    if (!user) {
      setShowAuth(true)
      return
    }
    setSaveState('saving')
    try {
      await api.createSavedSearch(filters)
      setSaveState('saved')
    } catch (err) {
      setSaveState('')
      setError(err.message)
    }
  }

  function reset() {
    setParsed(null)
    setSaveState('')
    setSearched('')
    setText('')
    setKeyword('')
    setFilters({})
    loadPage(1, true, {})
  }

  // 地图选区与条件筛选共用同一套 filters，两者不是两条独立的检索路径，
  // 否则「地图选了中山区」和「筛选选了两室一厅」会互相覆盖
  function pickDistrict(district) {
    const next = { ...filters, district }
    if (!district) delete next.district
    setFilters(next)
    loadPage(1, true, next)
  }

  /** 应用筛选条件。
   *
   * 这里**不再清空智能体的结果**。原先是「用筛选器就退出智能搜索」，
   * 两者互斥——用户先用一句话搜出来、再想点一下「有电梯」缩小范围，
   * 这一下会把结果整个清空退回全量列表。而这恰恰是最自然的操作顺序。
   *
   * 现在筛选栏改的就是智能体填进来的那份条件，两者是同一份东西。
   */
  function applyFilters(next) {
    // 筛选栏只管它自己那几项，关键词和设施偏好要带过去
    const merged = { ...next }
    if (filters.keyword) merged.keyword = filters.keyword
    if (filters.features) merged.features = filters.features
    setFilters(merged)
    loadPage(1, true, merged)
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

  const hasMore = houses.length < total
  const hasFilters = Object.values(filters).some(
    (v) => v !== '' && v !== null && v !== undefined)

  return (
    <Suspense fallback={null}>
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="logo">
            {/* 站点名可在「系统配置」里改。取前两字做深色、其余做品牌色，
                名字长短不一时也不会散架 */}
            {site.site_name.slice(0, 2)}
            <span>{site.site_name.slice(2)}</span>
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
                {/* 这里写死三个角色而不是 `role !== 'tenant'`：
                    下面的渲染分支是按角色一个个列出来的，用「不等于租客」
                    的话，将来加一个新角色就会得到一个点开全白的抽屉 */}
                {['landlord', 'agent', 'admin'].includes(user.role) && (
                  <button className="btn ghost small" onClick={() => setShowWork(true)}>
                    {user.role === 'admin' ? '管理后台' : '工作台'}
                  </button>
                )}
                <button className="btn ghost small" onClick={() => setShowTalk(true)}>
                  咨询
                </button>
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
            直接描述需求，智能体会把它翻译成下面的筛选条件——
            条件看得见、也改得动。
          </p>

          <form className="search-row" onSubmit={search}>
            <input
              placeholder="例如：中山区两室一厅，租金三千以内，最好带电梯"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn" type="submit" disabled={busy || !text.trim()}>
              {busy ? '解析中…' : '智能找房'}
            </button>
          </form>

          {/* 关键词检索。**这条路径完全不经过智能体**，是找房的保底入口。
              以前全站唯一的找房入口就是上面那个自然语言框，
              智能体一挂，整站就搜不了房。而用户想找「桂林街」这种
              具体地名时，本来也不需要惊动大模型。 */}
          <form className="search-row plain" onSubmit={searchKeyword}>
            <input
              placeholder="或直接搜小区、街道、商圈，如：桂林街"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            <button className="btn ghost" type="submit">搜索</button>
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

          {/* 解析结论：只说「听懂了什么、怎么听懂的」。
              条件本身显示在下面的「已生效条件」里，那一份是可删可改的，
              两处重复摆一遍反而让人分不清哪个说了算 */}
          {parsed && !parsed.ok && (
            <div className="answer error">
              没听懂这句话，已经保留你原来的筛选条件。
              可以说得具体一点，比如「中山区两室一厅，租金三千以内」。
            </div>
          )}

          {parsed?.ok && (
            <div className="parsed">
              <div className="parsed-title">
                智能体把「{searched}」翻译成了下面的筛选条件
                {SOURCE_LABEL[parsed.source] ? `（${SOURCE_LABEL[parsed.source]}）` : ''}
              </div>
              {parsed.divergence?.length > 0 && (
                <div className="parsed-note">
                  {parsed.divergence.join('；')}
                </div>
              )}
            </div>
          )}

          {error && <div className="answer error">{error}</div>}
        </section>

        {/* 无论条件来自哪里都在这里如实列出来，并且每一条都能删。
            这是 K-06 的落点：检索条件只有一份 */}
        <ActiveFilters
          value={filters}
          onChange={(next) => {
            setFilters(next)
            setSaveState('')
            loadPage(1, true, next)
          }}
          onSave={saveCurrentSearch}
          saveState={saveState}
          note={parsed?.ok
            ? '这些条件由智能体解析而来，可以直接删改，也可以在下方筛选栏继续调整。'
            : ''}
        />

        <ErrorBoundary label="公告">
          <NoticeBar />
        </ErrorBoundary>

        {!hasFilters && (
          <RecommendStrip
            user={user}
            favorites={favorites}
            onToggleFavorite={toggleFavorite}
            onOpen={(house) => go('house', house.house_no)}
          />
        )}

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

        {showMap && (
          <MapFinder
            selected={filters.district || ''}
            filters={filters}
            onPick={pickDistrict}
            onOpen={(house) => go('house', house.house_no)}
          />
        )}

        {/* 筛选栏不再对智能搜索隐藏。它改的就是智能体填进来的那份条件，
            两者是同一份东西——藏起来才是把两条路径割裂开的那一刀 */}
        <FilterBar value={filters} onApply={applyFilters} disabled={busy} />

        <div className="list-head">
          <h3>{hasFilters ? '筛选结果' : '最新房源'}</h3>
          <span className="count">共 {total} 套</span>
          {hasFilters && (
            <button className="btn ghost small" onClick={reset}>
              重置
            </button>
          )}
        </div>

        {/* 解析中先摆骨架屏（K-10）。智能体最慢的那一下要等一趟大模型往返，
            这期间如果页面纹丝不动，用户会以为点击没生效，然后再点一次 */}
        {busy && houses.length === 0 ? (
          <div className="grid" aria-hidden="true">
            {Array.from({ length: 6 }).map((_, i) => (
              <div className="card skeleton" key={i}>
                <div className="sk-photo" />
                <div className="sk-line wide" />
                <div className="sk-line" />
                <div className="sk-line short" />
              </div>
            ))}
          </div>
        ) : houses.length === 0 ? (
          <div className="empty">
            {error
              ? '无法加载房源，请确认后端服务已启动。'
              : hasFilters
                ? '没有符合条件的房源。可以在上面的「已生效条件」里删掉几条再看看。'
                : '暂时还没有房源。'}
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
                  onClick={(house) => go('house', house.house_no)}
                />
              ))}
            </div>

            {hasMore && (
              <div className="load-more">
                {/* 翻页只有一条路径了。原先智能搜索要走另一个接口翻页，
                    而它的设施筛选是在前端做的，翻页时就出现了
                    「第二页开始条件不生效」这种说不清的行为 */}
                <button
                  className="btn ghost"
                  disabled={busy}
                  onClick={() => loadPage(page + 1, false)}
                >
                  {busy ? '加载中…' : `加载更多（已显示 ${houses.length} / ${total}）`}
                </button>
              </div>
            )}
          </>
        )}
      </main>

      {/* 悬浮入口。对比栏在底部居中，这个放右下角，两者不重叠。
          任何抽屉打开时都要收起来——它的层级在抽屉之上，
          不收起就会盖住抽屉右下角的内容，窄屏上尤其明显。 */}
      {!showChat && !showMine && !showWork && !selected && (
      <button
        className="chat-fab"
        onClick={() => setShowChat(true)}
        title="打开租房助手"
      >
        <span className="fab-dot" />
        租房助手
      </button>
      )}

      {showTalk && (
        <ChatWithLandlord
          initialId={talkId}
          onClose={() => { setShowTalk(false); setTalkId(0) }}
          onOpenHouse={async (no) => {
            try {
              setShowTalk(false)
              go('house', no)
            } catch {
              setError('这套房源已下架或不存在。')
            }
          }}
        />
      )}

      {showChat && (
        <ChatPanel
          user={user}
          onOpenHouse={(house) => {
            setShowChat(false)
            go('house', house.house_no)
          }}
          onClose={() => setShowChat(false)}
        />
      )}

      <footer className="site-foot">
        <span>{site.site_name} · 基于 AI 智能体的房屋租赁平台</span>
        {site.contact_phone && <span>客服电话：{site.contact_phone}</span>}
        <span>大连理工大学 实习实训课程设计</span>
      </footer>

      <CompareBar
        items={compare}
        onRemove={(no) => setCompare(compare.filter((h) => h.house_no !== no))}
        onClear={() => setCompare([])}
      />

      {selected && (
        <HouseDrawer
          onOpenHouse={(house) => go('house', house.house_no)}
          onTalk={(id) => {
            // 从房源详情发起咨询：关掉详情、打开会话面板并定位到这条
            go('home', '', true)
            setTalkId(id)
            setShowTalk(true)
          }}
          house={selected}
          user={user}
          favorited={favorites.includes(selected.house_no)}
          onToggleFavorite={toggleFavorite}
          onClose={back}
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
        <ErrorBoundary label="工作台">
          <LandlordPanel
            user={user}
            onClose={() => setShowWork(false)}
            onOpenHouse={(house) => {
              // 工作台是抽屉，房源详情也是抽屉。叠两层看不清，
              // 所以先关掉工作台再打开房源
              setShowWork(false)
              go('house', house.house_no)
            }}
          />
        </ErrorBoundary>
      )}

      {showWork && user?.role === 'agent' && (
        <ErrorBoundary label="中介工作台">
          <AgentPanel
            user={user}
            onClose={() => setShowWork(false)}
            onOpenHouse={(house) => {
              setShowWork(false)
              go('house', house.house_no)
            }}
          />
        </ErrorBoundary>
      )}

      {showWork && user?.role === 'admin' && (
        <ErrorBoundary label="管理后台">
          <AdminPanel user={user} onClose={() => setShowWork(false)} />
        </ErrorBoundary>
      )}

      {showMine && user && (
        <MyPanel
          user={user}
          onUserUpdated={setUser}
          onLogout={logout}
          onClose={() => setShowMine(false)}
          onApplySearch={(conditions) => {
            // 从「我的搜索」一键恢复：条件原样填回，走同一条检索路径
            setParsed(null)
            setSaveState('')
            setFilters(conditions)
            loadPage(1, true, conditions)
          }}
          onOpenHouse={(house) => {
            setShowMine(false)
            go('house', house.house_no)
          }}
        />
      )}
    </>
    </Suspense>
  )
}
