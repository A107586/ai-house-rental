import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api'
import useHashRoute from './useHashRoute'
import useScrollLock from './useScrollLock'
import useSearchHistory from './useSearchHistory'
import { whoami } from './storage'

// 代码分割：整包原先 303 KB，管理后台、房东工作台这些**租客永远用不到的
// 面板**也一起下载，而租客占访问量的绝大多数。
//
// 拆分的判据是「租客首次打开页面会不会立刻看到它」——首屏要的（房源卡片、
// 筛选栏、公告、推荐）保持静态引入，点开才用的抽屉与面板按需加载。
//
// 每块外面都要包 Suspense，否则加载那一瞬间会抛错。fallback 给的是空节点
// 而不是转圈：这些块只有几十 KB，本地几乎瞬间到位，闪一下加载动画
// 反而更晃眼。
const MyPanel = lazy(() => import('./components/MyPanel'))
const HouseDrawer = lazy(() => import('./components/HouseDrawer'))
const ChatPanel = lazy(() => import('./components/ChatPanel'))
const ChatWithLandlord = lazy(() => import('./components/ChatWithLandlord'))
const Workbench = lazy(() => import('./components/Workbench'))
// 真实地图（高德）。没配 Key 或加载失败时它内部会退回
// 离线的行政区示意图（MapFinder），所以这里只引这一个
const MapFinder = lazy(() => import('./components/AmapFinder'))
const AuthDialog = lazy(() => import('./components/AuthDialog'))
import ErrorBoundary from './components/ErrorBoundary'
// 首屏大图不做懒加载：它是第一眼就要出现的东西，
// 懒加载会让首屏先白一下再补上，反而更差
import HeroBanner from './components/HeroBanner'
// 登录注册整页。也不做懒加载：未登录用户点「登录」是高频路径，
// 让他先看半秒白屏不值当
import AuthPage from './components/AuthPage'
import HomeSections from './components/HomeSections'
import ActiveFilters from './components/ActiveFilters'
import RelaxHints from './components/RelaxHints'
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
/** 列表页的标题。四种 kind 共用同一套版式，只有标题和取数条件不同。 */
const LIST_TITLE = {
  search: '搜索结果',
  latest: '最新发布',
  featured: '优选好房',
  all: '自己翻也行，用筛选和地图收窄更快',
}

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

  /** 列表页：`#/list/<kind>`。
   *
   * 首页与列表页拆成两个**路由**而不是同一页的两种状态，
   * 是为了让浏览器的后退键能用——原先在原地切换，用户想回首页
   * 只能自己找「重置」按钮，后退键会直接退出站点。
   *
   * kind 取值：search（搜索结果）、latest、featured、all。
   * 四种共用同一套版式，只是取数条件不同。
   */
  // 房源详情是**盖在页面之上的抽屉**，不是另一个页面。
  // 所以要记住「抽屉底下原本是哪一页」，抽屉开着的时候背景不该变。
  //
  // 不记的话有两个后果，都实际发生了：
  //   1. 从首页点开一套房 → route 变成 house → 大图的渲染条件
  //      `route.name === 'home'` 不再成立 → **大图整块消失，
  //      下面的内容往上跳 460px**。这就是「点击有轻微位移」。
  //   2. 从列表页点开一套房 → 背景从列表变成了首页四栏，
  //      关掉抽屉才发现列表没了。
  // 搜索框占位文字轮转到第几条。
  //
  // 空框配一句「说说要求或直接搜地名」是句正确的废话——用户还是不知道
  // 能说什么。改成让示例本身在框里轮着出现：看见「沙河口 合租 单间
  // 1500以下」这种具体句子，就知道这个框吃什么。
  // 而且**不用先打字**，直接点找房就按当前这条搜。
  const [hintIdx, setHintIdx] = useState(0)
  const [showHistory, setShowHistory] = useState(false)

  // 房源抽屉底下压着的是哪一屏。
  //
  // **只记「有内容的那几种」**：house 是抽屉不换背景，work / auth 是
  // 整页、根本走不到这段渲染。其余一概当首页——不这么兜的话，
  // 一个拼错的 hash（#/wrok）会让首页少掉大图，而页面其余部分照常，
  // 排查起来完全看不出是路由的问题。
  const BG_ROUTES = ['home', 'list']
  const [bg, setBg] = useState({ name: 'home', param: '' })
  useEffect(() => {
    if (route.name === 'house') return
    setBg(BG_ROUTES.includes(route.name) ? route : { name: 'home', param: '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.name, route.param])

  // 占位轮转。只在用户没打字时转——他一开始输入就该停，
  // 不然框里的提示还在动，很像输入被系统改掉了。
  useEffect(() => {
    if (text.trim()) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const timer = setInterval(() => {
      setHintIdx((i) => (i + 1) % EXAMPLES.length)
    }, 3200)
    return () => clearInterval(timer)
  }, [text])

  const isList = bg.name === 'list'
  const listKind = isList ? (bg.param || 'all') : ''

  /** 列表页要不要摆筛选与地图。**四种 kind 都要**——
   * 它们本来就是「进来之后再收窄」的工具，进了列表页就该有。 */
  const inResults = isList


  const [user, setUser] = useState(null)
  // 登录态查过了没有。首帧 user 一定是 null，但那不等于「未登录」
  const [authReady, setAuthReady] = useState(false)

  // 本地存的对话与搜索记录都按账号分开，理由见 storage.js。
  // `identity` 同时当作 ChatPanel 的 key——换个人登录，
  // 那个组件整个重挂，上一个人的消息不会留在 React 的 state 里。
  //
  // **这两行必须排在 `user` 声明之后。** 原先跟搜索记录的其它状态
  // 摆在一起（第 166 行），那里 `user` 还没声明，读它是暂时性死区：
  // 构建照样通过，页面一打开就是「Cannot access 'C' before
  // initialization」，整页被 ErrorBoundary 接住，什么都不显示。
  const identity = whoami(user)
  // 搜索记录。替掉了「保存这组条件」——那个按钮要求用户预判
  // 自己以后还会不会用这组条件，人在搜的当下不会想这件事。
  const history = useSearchHistory(user)
  const [showAuth, setShowAuth] = useState(false)
  const [showMine, setShowMine] = useState(false)
  // D1/D2：从哪一层打开的房源详情。
  //
  // 面板原先只有「开」和「全关」两种动作：在个人中心里点开一套房，
  // 代码顺手 `setShowMine(false)` 把个人中心也关了；关掉详情之后
  // 用户回到的是首页，而他明明是从「我的收藏」进去的——
  // **回不去意味着他得从头再点一遍**：个人中心 → 收藏 → 往下翻。
  //
  // 记一个来处就够了，不必上完整的路由栈：详情之上不会再叠第三层。
  const [cameFrom, setCameFrom] = useState('')

  /** F3/F4：经纪人与管理员登录后直接进各自的后台。
   *
   * 他们打开这个站不是来找房子的。让他们先看一屏「说一句话帮你找房」、
   * 再自己去点右上角，等于每次都多走一步——**首页是给租客的，
   * 不是给所有人的**。
   *
   * 做法是登录后立刻把对应面板打开，而不是另建一套路由：
   * 面板本来就是全屏抽屉，内容与功能都齐；另做一套页面等于把
   * 同一批功能维护两遍。他们仍可以关掉面板去逛首页——
   * 这是默认落点，不是牢笼。
   */
  useEffect(() => {
    if (!user) return
    // 中介与管理员登录后直接进各自的工作台。
    // **只在首页时才自动跳**：用户点开一套房又被弹去工作台，
    // 会以为自己点错了
    if (['agent', 'admin'].includes(user.role) && route.name === 'home') {
      go('work', '', true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.role])

  const [favorites, setFavorites] = useState([])
  const [filters, setFilters] = useState({})
  const [compare, setCompare] = useState([])
  const [showChat, setShowChat] = useState(false)
  // 从哪套房源打开的助手。购物软件从商品页点客服会带上那件商品，
  // 这里同理：带上之后开场白就是关于这套房的，用户说「收藏这套」
  // 「约看房」也不用再报编号。
  const [chatHouseNo, setChatHouseNo] = useState('')
  // 列表页默认展开地图（贴在右侧），首页默认收起。
  // 列表页是「在找」的语境，地图和列表并排看才有意义；
  // 首页展开着只会把房源整体推下去一屏。
  const [showTalk, setShowTalk] = useState(false)
  const [talkId, setTalkId] = useState(0)
  // 已经搜过的那句话。翻页要用它重新请求，
  // 不能用输入框里的当前内容——用户可能已经改了半句还没提交
  const [searched, setSearched] = useState('')
  // M-09 公开配置。取不到就用内置文案，配置服务不该拦住首页。
  const [site, setSite] = useState({ site_name: '大连智租', contact_phone: '' })

  // 任何一个抽屉/弹窗开着时都锁住背景页的滚动。
  // 不锁的话，详情开着滚轮一转，后面的列表跟着动——用户以为在翻详情，
  // 实际翻的是背景，关掉才发现滚到别处去了。
  useScrollLock(Boolean(selected) || showMine
                || showChat || showTalk || showAuth)

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
        setError('这套房源已下架或不存在，详情看不了了。')
        go('home', '', true)
      })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.name, route.param])

  // 回到首页时把搜索的痕迹清干净。
  //
  // 拆成两个路由之后漏了这一步：从列表页退回首页，`text`、`parsed`、
  // `searched`、`filters` 全都还在，于是首页上出现三样不该有的东西——
  // 搜索框里留着上次搜的词、「智能体把『桂林街』翻译成了…」的结论条、
  // 以及「已生效条件 1」。**连大图上的「在架房源」都显示成 3**，
  // 因为 total 还是上次搜索的结果数。
  //
  // 首页的语义是「什么都没搜」，所以这些状态一个都不该留。
  useEffect(() => {
    // 用 route 而不是 bg：只有真的导航回首页才清，
    // 从列表页点开详情（route=house、bg 仍是 list）不该触发
    if (route.name !== 'home') return
    setText('')
    setParsed(null)
    setSearched('')
    setError('')
    setSaveState('')
    setFilters({})
    // 重新取一次不带条件的列表，把 total 还原成全站套数——
    // 大图上的统计读的就是它
    loadPage(1, true, {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.name])

  // 进列表页时按 kind 取数。
  //
  // search 那一路不在这里取——它的条件由 `search()` 解析出来后自己加载，
  // 这里再取一次会把解析结果冲掉。
  useEffect(() => {
    // C6：地图默认展开——**四个入口都展开，包括搜索结果那一路**。
    // 原来 search 走的是下面那个 early return，于是「说一句话找房」
    if (!isList || listKind === 'search') return
    // 和首页那一栏用**同一组条件**。两处写得不一样的话，
    // 用户点「全部最新房源」会看到一批和上面那排对不上的房子。
    const preset = {
      latest: { published_days: 30, sort: 'published_at' },
      featured: { min_rating: 4, sort: 'rating' },
      all: {},
    }[listKind] || {}
    setSearched('')
    setParsed(null)
    setFilters(preset)
    loadPage(1, true, preset)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isList, listKind])

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
        // **不管成没成，都要标记「查过了」。** 整页路由的权限守卫
        // 靠它区分「还没查出来是谁」和「查出来了，没这个权限」——
        // 不区分的话，带着有效令牌直接打开 #/work 会在首帧被踢回首页，
        // 因为那一刻 user 还是 null
        .finally(() => setAuthReady(true))
    } else {
      setAuthReady(true)
    }
    // **直接打开列表页时不要在这里取数。**
    //
    // 两个 loadPage 会并发：这一个不带任何条件，列表页那个带着栏目预设
    // （min_rating=4）。谁后返回谁写进 state——实测是不带条件的那个后到，
    // 于是「优选好房」一进去就是全部 95 套，直到用户碰一下排序才变成 48 套。
    // 表现很怪：**页面标题是优选好房，内容是全部房源，改个排序反而对了。**
    //
    // 判据用 location.hash 而不是 route/bg：那两个要等 effect 跑完才准，
    // 而这里是挂载时第一件事。
    if (!window.location.hash.startsWith('#/list/')) {
      loadPage(1, true)
    }
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
    // 搜索一律落到列表页。首页只负责给入口，结果摆在自己的路由上——
    // 这样后退键能直接回首页，不用去找「重置」在哪
    if (route.name !== 'list') go('list', 'search')
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
      setError('智能找房暂时用不了，先按关键词帮你搜了；也可以用下面的筛选条件。')
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
  /** 跳转并回到页面顶部（D3）。
   *
   * 单纯换路由不会动滚动位置——这在从页脚跳转时尤其别扭：
   * 用户滚到底才看见那几个入口，点完停在新页面的底部。
   *
   * 用 `auto` 不用 `smooth`：这是一次换页，不是同一页里的移动。
   * 平滑滚动会让人以为页面还是刚才那一页，只是往上挪了挪。
   */
  function goTop(name, param) {
    go(name, param)
    window.scrollTo({ top: 0, behavior: 'auto' })
  }

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
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  /** 跳到第 n 页。**替换而不是追加**，并把视口带回列表顶部——
   *  翻了页却停在原来的滚动位置，用户会以为没反应。 */
  function gotoPage(n) {
    const target = Math.min(Math.max(1, n), totalPages)
    if (target === page || busy) return
    loadPage(target, true)
    const anchor = document.querySelector('.list-head')
    if (anchor) {
      const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      anchor.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' })
    }
  }

  /** 页码按钮。页数多时中间省略，首尾与当前页附近始终可见——
   *  全量列出来的话 20 页就是 20 个按钮，一行摆不下。 */
  const pageItems = (() => {
    if (totalPages <= 7) {
      return Array.from({ length: totalPages }, (_, i) => i + 1)
    }
    const out = [1]
    const from = Math.max(2, page - 1)
    const to = Math.min(totalPages - 1, page + 1)
    if (from > 2) out.push('...')
    for (let i = from; i <= to; i += 1) out.push(i)
    if (to < totalPages - 1) out.push('...')
    out.push(totalPages)
    return out
  })()
  const hasFilters = Object.values(filters).some(
    (v) => v !== '' && v !== null && v !== undefined)

  // G1：登录注册整页。它是**独占一屏的路由**，不是盖在页面上的弹窗，
  // 所以在这里直接返回，不渲染顶栏、大图、列表那一整套。
  //
  // **位置很关键：必须放在所有 hook 与函数定义之后。**
  // 我一开始把它插在组件靠前的位置，`onDone` 里引用了下面才用
  // `const` 定义的 `loadFavorites`——登录接口其实成功了、令牌也存进去了，
  // 但回调一执行就抛 `Cannot access 'j' before initialization`
  // （压缩后的变量名），页面卡在登录页不动，报错还显示在「登录失败」的
  // 位置上，看着像是账号密码错了。
  //
  // 提前 return 不能提前到「它用到的东西还没初始化」之前。
  // F1：工作台是整页，不是抽屉。
  //
  // 和上面 auth 那一条同理，这个 return 也**必须排在所有 hook 之后**。
  // 放前面的话，它引用到的 `user` / `go` 还没初始化，
  // 页面会白屏在一句 `Cannot access ... before initialization` 上——
  // 这个坑 9-10 已经踩过一次了。
  // 进不去 work 就把地址栏退回首页。
  //
  // 不退的话会停在一个「谁也进不去」的 hash 上：整页工作台不渲染，
  // 下面的常规分支又因为 `bg.name` 是 'work' 而不认这是首页，
  // **首屏大图整块消失**，看着像页面加载了一半。
  // 退出登录、或者把 #/work 链接发给一个租客账号，都会撞上。
  // 房东、中介、管理员都走整页工作台。
  //
  // 管理后台原来是抽屉 + 十七个横向页签，多到要横滚；改成同一套
  // 左栏竖排之后一屏列完，当前位置也一直看得见。三个角色用同一个
  // 外壳，是因为他们要的形态本来就是同一种：**长时间停留、
  // 功能多、要随时知道自己在哪一栏**。
  const WORK_ROLES = ['landlord', 'agent', 'admin']
  const canWork = user && WORK_ROLES.includes(user.role)

  // **查清楚了再踢人。** authReady 之前 user 必然是 null，
  // 这时候判「没权限」就会把带着有效令牌直接打开 #/work 的人
  // 在首帧弹回首页——而他明明有权限。
  if (route.name === 'work' && authReady && !canWork) {
    go('home', '', true)
  }

  if (route.name === 'work' && !authReady) {
    return <div className="page"><div className="empty">加载中…</div></div>
  }

  if (route.name === 'work' && canWork) {
    return (
      <Suspense fallback={<div className="page"><div className="empty">加载中…</div></div>}>
        <Workbench
          user={user}
          onClose={() => go('home', '', true)}
        />
      </Suspense>
    )
  }

  if (route.name === 'auth') {
    return (
      <AuthPage
        mode={route.param || 'login'}
        onSwitch={(m) => go('auth', m, true)}
        onBack={() => go('home', '', true)}
        onDone={(u) => {
          setUser(u)
          loadFavorites()
          go('home', '', true)
        }}
      />
    )
  }

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="logo">
            {/* 站点名可在「系统配置」里改。取前两字做深色、其余做品牌色，
                名字长短不一时也不会散架 */}
            {site.site_name.slice(0, 2)}
            <span>{site.site_name.slice(2)}</span>
          </div>
          <div className="tagline">AI 智能找房</div>

          <div className="topbar-right">
            {/* B1：服务状态和「退出」都收进个人中心了。
                顶栏是**每一屏都在的东西**，摆的应该是每一屏都可能要点的入口。
                「服务正常」一天看一次就够（真出问题时页面自己会报错），
                「退出」更是几天才点一次——两个低频项占着最贵的位置，
                把真正高频的「咨询」「工作台」挤到了一排文字里。

                服务未连接时是例外：这时它不再是状态，是一句**要立刻看见**
                的警告，所以只在异常时留在顶栏。 */}
            {online === false && (
              <span className="status-dot offline" title="服务暂时连不上">
                <span className="dot down" />
                当前无连接
              </span>
            )}

            {user ? (
              <>
                {/* E2：咨询改成独立图标。
                    它是**未读会有新消息的入口**，和「工作台」「退出」
                    这类纯导航不是一类东西——混在一排文字里，
                    有人找你说话这件事就被淹没了。 */}
                <button
                  className="icon-btn"
                  onClick={() => setShowTalk(true)}
                  aria-label="咨询消息"
                  title="咨询消息"
                >
                  <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                    <path
                      d="M3 4.5h14v9H7.5L4 16.5v-3H3z"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>

                {/* E3：其余改成纯文字，用竖线分隔。
                    一排描边按钮会让顶栏看着像工具栏，而这些其实
                    只是导航链接——**文字加分隔线才是导航的样子**。 */}
                <nav className="topnav">
                  {/* 这里写死三个角色而不是 `role !== 'tenant'`：
                      下面的渲染分支是按角色一个个列出来的，用「不等于租客」
                      的话，将来加一个新角色就会得到一个点开全白的抽屉 */}
                  {['landlord', 'agent', 'admin'].includes(user.role) && (
                    <button
                      className="topnav-a"
                      onClick={() => go('work')}
                    >
                      {user.role === 'admin' ? '管理后台'
                        : user.role === 'agent' ? '中介工作台' : '房东工作台'}
                    </button>
                  )}
                </nav>

                {/* 头像 +「个人中心」。**入口该说自己是什么，不是叫什么名字**——
                    顶栏写着「林小雨」，用户知道自己已经登录了，
                    但不知道点下去会到哪；写「个人中心」他就知道。
                    名字改由头像承担：一个字的圆片既认得出是自己，又不占地方。 */}
                <button
                  className="me-entry"
                  onClick={() => setShowMine(true)}
                  title={user.real_name || user.username}
                >
                  <span className="me-avatar" aria-hidden="true">
                    {(user.real_name || user.username || '我').trim()[0]}
                  </span>
                  <span className="me-label">个人中心</span>
                </button>
              </>
            ) : (
              <button className="btn small" onClick={() => go('auth', 'login')}>
                登录 / 注册
              </button>
            )}
          </div>
        </div>
      </header>

      {/* 首屏大图。只在首页出现——进了房源详情还压一张大图，
          等于把用户想看的内容往下推一屏。 */}
      {bg.name === 'home' && (
        <HeroBanner
          stats={[
            { value: total || '—', label: '在架房源（套）' },
            { value: '6', label: '覆盖行政区' },
            { value: 'AI', label: '智能找房 · 客服 · 审阅' },
          ]}
        >
          {/* **一个框。**
              这里原先并排放着两个：一个自然语言、一个纯关键词。分成两个
              的初衷是「智能体挂了还能搜」，可代价是把一个技术上的分工
              摆到了用户面前——他站在两个长得一样的框前面，得先想清楚
              自己这句话算「描述需求」还是「搜小区」才知道往哪打字。

              保底没有丢，只是挪到了代码里：解析不出条件（多半是地名），
              或者智能体压根连不上，`search()` 自动转成关键词检索。 */}
          <form
            className="search-row"
            onSubmit={(e) => {
              e.preventDefault()
              // **空框也能搜。** 没打字就按框里正在轮转的那条搜——
              // 用户看到的是什么，点下去就搜什么，所见即所得。
              const q = text.trim() || EXAMPLES[hintIdx]
              if (!text.trim()) setText(q)
              history.remember(q)
              setShowHistory(false)
              search(null, q)
            }}
          >
            <input
              placeholder={EXAMPLES[hintIdx]}
              value={text}
              onChange={(e) => { setText(e.target.value); setShowHistory(true) }}
              onFocus={() => setShowHistory(true)}
              // 延迟收起：直接在 blur 里关会赶在点击之前，
              // 用户点记录时框已经没了，等于点了个空气
              onBlur={() => setTimeout(() => setShowHistory(false), 160)}
            />
            {/* **不置灰。** 一个灰掉的主按钮等于告诉用户「这儿现在没用」，
                可这是首屏唯一的行动点。既然空框也能搜，它就该一直是亮的。 */}
            <button className="btn" type="submit" disabled={busy}>
              {busy ? '找房中…' : '找房'}
            </button>

            {/* 搜索记录。点开输入框就出现，边打字边过滤。 */}
            {showHistory && history.match(text).length > 0 && (
              <div className="search-history">
                <div className="sh-head">
                  <span>搜索历史</span>
                  <button
                    type="button"
                    className="sh-clear"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={history.clear}
                  >
                    清空
                  </button>
                </div>
                {history.match(text).map((q) => (
                  <div className="sh-item" key={q}>
                    <button
                      type="button"
                      className="sh-text"
                      // mousedown 里阻止默认，免得输入框先失焦把下拉关掉
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        setText(q)
                        setShowHistory(false)
                        history.remember(q)
                        search(null, q)
                      }}
                    >
                      {q}
                    </button>
                    <button
                      type="button"
                      className="sh-del"
                      aria-label={`删除记录：${q}`}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => history.forget(q)}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </form>

          <div className="examples">
            <span>示例：</span>
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
        </HeroBanner>
      )}

      <main
        className={`page${bg.name === 'home' ? ' with-hero' : ''}`
          + (isList ? ' list-page' : '')}
      >

        {/* B1：删掉了「智能体把『xx』翻译成了下面的筛选条件」那一条。
            它和下面的「已生效条件」讲的是同一件事——一个用文字复述、
            一个用可删的胶囊列出。**同一屏把同一件事说两遍，
            用户还得先判断哪个才算数。** 留可操作的那个。

            解析来源（规则 / 融合）挪进了「已生效条件」的说明行里，
            信息没丢，只是不再单占一块。 */}
        {error && <div className="answer error">{error}</div>}
        {parsed && !parsed.ok && (
          <div className="answer error">
            未识别到有效条件，已保留原有筛选条件。可以说得更具体，比如「中山区两室一厅，租金三千以内」。
          </div>
        )}

        {/* C4：这排 chip 已经并进筛选栏（见下方 FilterBar 的 chips），
            这里不再单独出一块。 */}

        {/* 公告只在首页出现。列表页是「我在找房」的语境，
            顶上横一条平台公告属于打断——用户是带着目的进来的。 */}
        {!isList && (
          <ErrorBoundary label="公告">
            <NoticeBar />
          </ErrorBoundary>
        )}

        {/* 「为你推荐」是首页的第一栏。它要登录、要有浏览记录才出得来，
            所以由组件自己决定渲不渲染，这里不加条件。 */}
        {!isList && (
          <RecommendStrip
            user={user}
            favorites={favorites}
            onToggleFavorite={toggleFavorite}
            onOpen={(house) => go('house', house.house_no)}
          />
        )}

        {/* 首页其余三栏：最新发布 / 优选好房 / 全部房源。
            每栏只出一排，右上角「全部 N 套」跳到对应的列表页。 */}
        {!isList && (
          <HomeSections
            favorites={favorites}
            onToggleFavorite={toggleFavorite}
            onOpen={(house) => go('house', house.house_no)}
            onMore={(kind) => go('list', kind)}
          />
        )}

        {/* 地图找房：搜索框挪走之后，这块接管了原来的位置。
            原先只是一行灰字，看不出可点、也看不出点了会发生什么。
            现在做成一个有标题有说明的折叠面板：**默认收起**——
            地图是「换一种方式找」，不是主路径，展开着会把房源列表
            推下去一屏。 */}
        {/* **列表页是左右两栏。**
            左边：筛选、结果、翻页；右边：地图，跟着滚动粘住。
            原先地图是横在列表上方的折叠条，展开后把房源整体推下一屏，
            而且「看地图」和「看列表」得来回滚——**这两件事本来就该
            同时看得见**：在图上点一个区，左边列表当场变。 */}
        {/* F2：地图收起时整个布局回到单栏。
            原先收起只是把地图内容藏了，**右边那一栏 520–620px 的宽度
            还占着**——于是屏幕右半边空一大片，左边的房源卡片也挤在
            原来的窄栏里不肯铺开。收起本该是「把地方让出来」。 */}
        <div className={isList ? 'list-layout' : ''}>
        <div className="list-main">

        {/* C1 + C2：搜索框搬进左栏。
            原来它在 `.list-layout` 外面通栏放着——1334px 宽，
            而它正下方的筛选栏和房源栅格只有 794px。**同一列里，
            最上面那个框比下面所有框都宽出去一大截**，看着就像没对齐。
            搬进来之后三个框自然同宽，右侧地图也跟着往上提了 88px，
            和左栏第一个框齐平。 */}
        {isList && (
          <div className="list-search">
            <form className="search-row" onSubmit={search}>
              <input
                placeholder="换个说法试试，比如：中山区两室一厅三千以内 / 桂林街"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <button className="btn" type="submit" disabled={busy || !text.trim()}>
                {busy ? '找房中…' : '找房'}
              </button>
            </form>
          </div>
        )}

        {/* 筛选栏不再对智能搜索隐藏。它改的就是智能体填进来的那份条件，
            两者是同一份东西——藏起来才是把两条路径割裂开的那一刀。
            但它只在结果页出现：首页一进来就摆一排筛选项，
            等于让人先做题再看房。 */}
        {inResults && (
          <FilterBar
            value={filters}
            onApply={applyFilters}
            disabled={busy}
            chips={(
              <ActiveFilters
                value={filters}
                onChange={(next) => {
                  setFilters(next)
                  setSaveState('')
                  loadPage(1, true, next)
                }}
              />
            )}
          />
        )}

        {/* 底部这条完整列表只属于列表页。
            首页已经用四个分栏给过入口了，再挂一条九十多套的长列表，
            等于把「给入口」和「摆全部」两件事做了两遍。 */}
        {isList && (
        <div className="list-head">
          {/* C5：返回键移到最左、缩成一个圆圈。
              **返回是「离开这一页」，不是这一页的一个功能**——
              摆在标题右边跟「共 95 套」并排，它看着像个筛选项；
              放在标题前面才是所有网站都在用的那个位置。
              文字收进 title：图标够认，一行字白占地方。 */}
          <button
            className="back-round"
            onClick={() => go('home')}
            aria-label="返回首页"
            title="返回首页"
          >
            ‹
          </button>
          <h3>{LIST_TITLE[listKind] || '房源'}</h3>
          <span className="count">共 {total} 套</span>
        </div>
        )}

        {/* 解析中先摆骨架屏（K-10）。智能体最慢的那一下要等一趟大模型往返，
            这期间如果页面纹丝不动，用户会以为点击没生效，然后再点一次 */}
        {isList && (busy && houses.length === 0 ? (
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
              ? '房源加载失败，请稍后刷新再试。'
              : hasFilters
                ? '没有符合条件的房源。'
                : '暂时还没有房源'}
            {!error && hasFilters && (
              <RelaxHints
                filters={filters}
                onPick={(next) => {
                  setFilters(next)
                  setSaveState('')
                  loadPage(1, true, next)
                }}
              />
            )}
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

            {/* **列表页用翻页，不用「加载更多」。**
                「加载更多」适合无尽流（微博、朋友圈）——那种场景里
                用户不需要「回到第 3 页」。找房不是：他会来回比较，
                会记得「刚才那套在第二页」。一路追加下去的话，
                翻到第五页想回头就得从头滚。

                首页那三栏仍是固定 4 套的预览，不涉及翻页。 */}
            {totalPages > 1 && (
              <nav className="pager" aria-label="房源分页">
                <button
                  className="pg-btn"
                  disabled={busy || page <= 1}
                  onClick={() => gotoPage(page - 1)}
                >
                  上一页
                </button>

                {pageItems.map((it, i) => (
                  it === '...' ? (
                    <span className="pg-gap" key={`gap${i}`}>…</span>
                  ) : (
                    <button
                      key={it}
                      className={`pg-btn num${it === page ? ' on' : ''}`}
                      disabled={busy}
                      aria-current={it === page ? 'page' : undefined}
                      onClick={() => gotoPage(it)}
                    >
                      {it}
                    </button>
                  )
                ))}

                <button
                  className="pg-btn"
                  disabled={busy || page >= totalPages}
                  onClick={() => gotoPage(page + 1)}
                >
                  下一页
                </button>

                <span className="pg-info">共 {total} 套 · 第 {page}/{totalPages} 页</span>
              </nav>
            )}
          </>
        ))}
        </div>{/* /.list-main */}

        {/* 地图常驻，不能折叠。
            折叠键原本是想「不用地图的人可以把它收起来」，实际效果相反：
            收起之后这一栏变成一条什么也没有的横带，用户得先想起来
            「这里本来有张地图」才会去点它。**而地图和列表本来就是
            要同时看的**——在图上点一个区，左边列表当场变，
            这才是地图找房的用处。 */}
        {isList && (
        <section className="map-panel open">
          <div className="map-panel-head static">
            <span className="map-panel-title">地图找房</span>
            <span className="map-panel-sub">
              {filters.district
                ? `已选区域：${filters.district}`
                : '在地图上按区域挑，位置和周边都看得见'}
            </span>
          </div>

          {true && (
            <div className="map-panel-body">
              {/* **地图必须有自己的 Suspense。**
                  外层那个 Suspense 包着整个页面，地图是懒加载的——
                  一点击就开始加载，React 会挂起整棵树、渲染 fallback，
                  于是整页卸载、滚动位置归零，加载完再从头挂上。
                  表现就是「点地图找房，页面弹回最顶端」。
                  在这里单独兜一层，挂起的范围就只剩地图这一块。 */}
              <Suspense
                fallback={<div className="map-loading">高德地图加载中…</div>}
              >
                <MapFinder
                  selected={filters.district || ''}
                  filters={filters}
                  onPick={pickDistrict}
                  onOpen={(house) => go('house', house.house_no)}
                />
              </Suspense>
            </div>
          )}
        </section>
        )}

        </div>{/* /.list-layout */}

      </main>

      {/* 悬浮入口。对比栏在底部居中，这个放右下角，两者不重叠。
          任何抽屉打开时都要收起来——它的层级在抽屉之上，
          不收起就会盖住抽屉右下角的内容，窄屏上尤其明显。 */}
      {/* E2：图标换掉了那个绿色小圆点。
          圆点原本想表达「在线」，可它和顶栏的「服务正常」是同一个意思，
          说了两遍；而且一个纯色小点没有任何含义指向「这是个助手」。
          换成对话气泡里嵌两格的标记——和聊天面板里的助手头像同一个图形，
          **点开之前和点开之后看到的是同一个东西**。 */}
      {!showChat && !showMine && !selected && (
      <button
        className="chat-fab"
        onClick={() => { setChatHouseNo(''); setShowChat(true) }}
        title="打开租房助手"
      >
        <span className="fab-ico" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M6.5 4.75h11A1.75 1.75 0 0 1 19.25 6.5v7a1.75 1.75 0 0 1-1.75 1.75H9.74l-3.99 3.19V15.25H6.5A1.75 1.75 0 0 1 4.75 13.5v-7A1.75 1.75 0 0 1 6.5 4.75Z"
                  stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
            <rect x="8" y="8.4" width="3" height="3" rx=".6" fill="currentColor" />
            <rect x="13" y="8.4" width="3" height="3" rx=".6" fill="currentColor" />
          </svg>
        </span>
        <span className="fab-text">租房助手</span>
      </button>
      )}

      {showTalk && (
        <Suspense fallback={null}>
          <ChatWithLandlord
          me={user}
          initialId={talkId}
          onClose={() => {
          setShowTalk(false)
          setTalkId(0)
          if (cameFrom === 'mine') setShowMine(true)
          setCameFrom('')
          }}
          onOpenHouse={async (no) => {
          try {
          setShowTalk(false)
          go('house', no)
          } catch {
          setError('这套房源已下架或不存在，详情看不了了。')
          }
          }}
          />
        </Suspense>
      )}

      {showChat && (
        <Suspense fallback={null}>
          <ChatPanel
          key={identity}
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
          if (cameFrom === 'mine') setShowMine(true)
          setCameFrom('')
          }}
          />
        </Suspense>
      )}

      {/* E1：页脚重做。
          原来是三段并排的小灰字，一行了事——页脚是每一页的最后一屏，
          用户滚到这里通常是**没找到想要的东西**，一行版权信息接不住他。
          现在给四件事：这个站是什么、常用入口、能问谁、以及必要的
          免责声明。

          **不做成六列的巨型页脚。** 那是内容站的形态，我们统共只有
          十几个页面，撑不起六列，硬凑只会得到一堆重复链接。 */}
      <footer className="site-foot">
        <div className="foot-inner">
          <div className="foot-brand">
            <div className="foot-logo">
              {site.site_name.slice(0, 2)}
              <span>{site.site_name.slice(2)}</span>
            </div>
            <p className="foot-slogan">
              在大连找房、签约、交租、报修，一个地方办完。
            </p>
            <div className="foot-badges">
              <span>房源人工核验</span>
              <span>合同电子存证</span>
              <span>缴费流水可查</span>
            </div>
          </div>

          <nav className="foot-links" aria-label="页脚导航">
            <div className="foot-col">
              <h4>找房</h4>
              {/* D3：从页脚跳过去要**回到顶部**。
                  用户是滚到页面最底下才看到这几个入口的，点完页面换了
                  但滚动位置没动，他看到的是新页面的底部——一片页脚，
                  而他刚点的是「全部房源」。第一反应是「没反应」。 */}
              <button onClick={() => goTop('list', 'all')}>全部房源</button>
              <button onClick={() => goTop('list', 'latest')}>最新发布</button>
              <button onClick={() => goTop('list', 'featured')}>优选好房</button>
            </div>
            <div className="foot-col">
              <h4>我的</h4>
              {user ? (
                <>
                  <button onClick={() => setShowMine(true)}>个人中心</button>
                  <button onClick={() => setShowTalk(true)}>咨询消息</button>
                </>
              ) : (
                <>
                  <button onClick={() => go('auth', 'login')}>登录</button>
                  <button onClick={() => go('auth', 'register')}>注册</button>
                </>
              )}
              <button onClick={() => { setChatHouseNo(''); setShowChat(true) }}>
                租房助手
              </button>
            </div>
            <div className="foot-col">
              <h4>联系我们</h4>
              {site.contact_phone
                ? <span className="foot-phone">{site.contact_phone}</span>
                : <span className="foot-muted">客服电话未配置</span>}
              <span className="foot-muted">工作日 9:00–18:00</span>
            </div>
          </nav>

          {/* **这句话必须有。** 站里的房源、评价、账单全是课程演示数据，
              界面又做得像个真站；不写清楚，看到的人真按上面的号码打过去
              就成了我们的问题。
              放进 foot-inner 里：分割线和上面的内容一样宽，左右对齐。 */}
          <div className="foot-bottom">
            <span>© 2026 {site.site_name}</span>
            <span>大连理工大学实习实训作品 · 房源与交易均为演示数据</span>
          </div>
        </div>
      </footer>

      <CompareBar
        items={compare}
        onRemove={(no) => setCompare(compare.filter((h) => h.house_no !== no))}
        onClear={() => setCompare([])}
      />

      {selected && (
        <Suspense fallback={null}>
          <HouseDrawer
          onOpenHouse={(house) => go('house', house.house_no)}
          onAskAssistant={(house) => {
          // D2：关掉详情再开助手——两个抽屉叠着会互相盖住。
          // **但只关详情，不动背景**：原先这里还 `go('home')`，
          // 于是用户从列表页点进一套房、再点「问助手」，
          // 关掉助手之后人在首页，刚才那一页搜索结果全没了。
          setChatHouseNo(house.house_no)
          setCameFrom(showMine ? 'mine' : '')
          back()
          setShowChat(true)
          }}
          onTalk={(id) => {
          // D2：同上——只关详情，背景留在原处
          back()
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
          // D1：关掉详情要**回到打开它的那一层**，不是一路回首页。
          // 从「我的收藏」点进来的，关掉就该重新看到收藏列表——
          // 否则用户得重走一遍：个人中心 → 收藏 → 往下翻到刚才那个位置。
          onClose={() => {
          back()
          if (cameFrom === 'mine') setShowMine(true)
          setCameFrom('')
          }}
          />
        </Suspense>
      )}

      {showAuth && (
        <Suspense fallback={null}>
          <AuthDialog
          onClose={() => setShowAuth(false)}
          onLoggedIn={(u) => {
          setUser(u)
          setShowAuth(false)
          loadFavorites()
          }}
          />
        </Suspense>
      )}

      {/* 三个角色的面板都搬进了整页工作台（#/work），这里不再挂抽屉。
          面板组件本身没删——它们仍然是唯一一份业务实现，
          只是换了个壳；抽屉那条路没有人再走，留着就是死代码。 */}

      {showMine && user && (
        <Suspense fallback={null}>
          <MyPanel
          user={user}
          online={online}
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
          // 记住是从个人中心进来的。**自己不关**——关了的话
          // 用户关掉详情就回到首页，而他是从收藏列表点进来的
          setCameFrom('mine')
          setShowMine(false)
          go('house', house.house_no)
          }}
          />
        </Suspense>
      )}
    </>
  )
}
