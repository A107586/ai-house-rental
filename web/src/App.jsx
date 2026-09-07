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

// 例句里**必须留一个纯地名**。
//
// 两个框合成一个之后，「这里也能直接搜小区」这件事就只剩占位符一句话
// 在说了，而占位符一打字就没了。放一个「桂林街」在例句里，用户点一次
// 就知道这个框两种说法都吃。
const EXAMPLES = [
  '中山区两室一厅，租金三千以内，最好带电梯',
  '桂林街',
  '沙河口 合租 单间 1500以下',
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
  // 认不出是哪个区的地名（街道、小区、商圈）。智能体那边判定它不是
  // 大连的六个区之一，就落到这里；后端的 keyword 会去比对小区、地址、
  // 商圈和标题。**不这么做的话「桂林街」会被当成一个区，查出 0 套。**
  guanjianci: 'keyword',
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
//
// **这张表只是兜底。** 正文优先用后端返回的 `source_label`：
// 同一句话在两处各写一份，早晚会写得不一样——实测就是这么写岔的。
// 前端这份把 `rules` 说成「未配置大模型」，而大模型明明配着
// （gpt-5.5），只是规则已经足够确定、按 K-10 跳过了它。
// 页面上于是出现「未配置大模型」，用户和评委都会以为接入失败了。
const SOURCE_LABEL = {
  rules: '规则解析，未调用大模型',
  fused: '规则 + 大模型融合',
  llm: '大模型解析',
  rules_fallback: '大模型输出不可用，已退回规则结果',
}

const sourceLabel = (parsed) => (parsed?.source_label
  || SOURCE_LABEL[parsed?.source] || '')

const PAGE_SIZE = 12

export default function App() {
  const [text, setText] = useState('')
  // 智能体的解析结论。**它不再持有房源列表**——列表只有一份，
  // 由 filters 决定（K-06）。这里留的是「它听懂了什么、是怎么听懂的」，
  // 用来在页面上向用户解释，答辩时也能展示规则与大模型的分工。
  const [parsed, setParsed] = useState(null)
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
  // 从哪套房源打开的助手。购物软件从商品页点客服会带上那件商品，
  // 这里同理：带上之后开场白就是关于这套房的，用户说「收藏这套」
  // 「约看房」也不用再报编号。
  const [chatHouseNo, setChatHouseNo] = useState('')
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
        // 一个条件都没解析出来——那多半是个地名（「桂林街」「青泥洼桥」）。
        // 直接当关键词搜，别回一句「没听懂」把人打发走。
        await byKeyword(content)
        return
      }
      // 覆盖式合并：这一句话表达的是一次新的检索意图，
      // 和上一句的条件叠加只会得到一个谁也没要过的结果
      const next = toFilters(parsed)
      setFilters(next)
      await loadPage(1, true, next)
    } catch (err) {
      // **智能体不通时，这个框仍然要能找房。**
      //
      // 这正是当初单独放一个关键词框的理由。框合成一个之后，那份保底
      // 不能跟着没了——改成在这里自动降级：解析不了就按关键词搜，
      // 顺带告诉他现在走的是哪条路。
      setParsed(null)
      // **先搜，再报。**
      // 反过来写的话，`loadPage` 成功时会顺手把 error 清成空串——
      // 于是降级悄悄发生了，用户完全不知道自己现在走的是哪条路。
      await byKeyword(content)
      setError('智能体暂时联系不上，已按关键词直接检索；也可以用下面的筛选条件找房。')
    } finally {
      setBusy(false)
    }
  }

  /** 关键词检索。这条路径**完全不经过智能体**，是找房的保底入口。
   *
   * 它不再有自己的输入框，但仍然是一条独立的路径：地名、小区名这类
   * 说法解析器认不出来（它认的是区、户型、价格、面积），
   * 而后端的 keyword 会去比对标题、描述、小区、地址、商圈。
   *
   * 与解析路径不同，这里是**叠加**而不是覆盖：用户先说「中山区两室一厅」
   * 再补一句「桂林街」，想要的显然是在刚才的结果里缩小，而不是
   * 把区和户型都丢掉。
   */
  function byKeyword(word) {
    const next = { ...filters, keyword: (word || '').trim() }
    if (!next.keyword) delete next.keyword
    setFilters(next)
    return loadPage(1, true, next)
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
            描述需求也行，直接打小区、街道名也行。智能体会把它翻译成
            下面的筛选条件—— 条件看得见、也改得动。
          </p>

          {/* **一个框。**
              这里原先并排放着两个：一个自然语言、一个纯关键词。分成两个
              的初衷是「智能体挂了还能搜」，可代价是把一个技术上的分工
              摆到了用户面前——他站在两个长得一样的框前面，得先想清楚
              自己这句话算「描述需求」还是「搜小区」才知道往哪打字。
              两个框还各存一份状态，光是让它们互相同步就已经出过一个 bug。

              保底没有丢，只是挪到了代码里：解析不出条件（多半是地名），
              或者智能体压根连不上，`search()` 自动转成关键词检索。 */}
          <form className="search-row" onSubmit={search}>
            <input
              placeholder="说说要求或直接搜地名，如：中山区两室一厅三千以内 / 桂林街"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn" type="submit" disabled={busy || !text.trim()}>
              {busy ? '找房中…' : '找房'}
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
                {sourceLabel(parsed) ? `（${sourceLabel(parsed)}）` : ''}
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
        onClick={() => { setChatHouseNo(''); setShowChat(true) }}
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
          houseNo={chatHouseNo}
          onOpenHouse={(house) => {
            setShowChat(false)
            go('house', house.house_no)
          }}
          onClose={() => {
            setShowChat(false)
            // **房源上下文只跟着这一次「问助手」。**
            // 不清掉的话，他下次从右下角的按钮打开助手，问的每一句话
            // 仍然挂在半小时前那套房上——「帮我收藏」收的是那一套。
            setChatHouseNo('')
          }}
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
          onAskAssistant={(house) => {
            // 关掉详情再开助手：两个抽屉叠着会互相盖住
            setChatHouseNo(house.house_no)
            go('home', '', true)
            setShowChat(true)
          }}
          onTalk={(id) => {
            // 从房源详情发起咨询：关掉详情、打开会话面板并定位到这条
            go('home', '', true)
            setTalkId(id)
            setShowTalk(true)
          }}
          house={selected}
          user={user}
          // 用户最近说的那句找房需求（J-09-a）。发起咨询时先拿它整理成
          // 一份需求摘要，随咨询一并发给负责人——省掉「请您再描述一遍」
          demandText={searched}
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
