// 后端接口封装。
//
// 三种运行方式都要支持：
//
// 1. 开发模式（vite dev，5173 端口）
//    两类请求都用相对地址，由 vite.config.js 里的代理转发，享受热更新。
//
// 2. 本机演示（业务后端 8080 直接托管 web/dist）
//    /api 与页面同源，仍用相对地址；
//    /agent 在另一个进程上，补一个 http://主机名:8000 的绝对地址。
//
// 3. 公网部署（nginx 在前，443 收口）
//    /api 与 /agent 都由 nginx 转发，**两者都是同源相对地址**。
//
// 这样组员只要跑 `npm run build`，之后访问 8080 就是完整网站，
// 不需要再单独启动前端，也不会出现「访问 8080 只看到 Not Found」的困惑。

const DEV_PORT = '5173'
const isDev = typeof location !== 'undefined' && location.port === DEV_PORT

// 智能体服务地址。
//
// **HTTPS 页面下必须走同源**，这是第 3 种方式的由来：
// 原先写死 `http://主机名:8000`，放到 https 站点上会踩两个坑——
//
//   一、混合内容：https 页面发 http 请求，浏览器直接拦掉，
//       控制台报 Mixed Content，而界面上只表现为「智能体没反应」；
//   二、8000 端口得对公网开着，而智能体服务**本来就不该暴露在公网上**
//       （它没有登录态，一切鉴权都由业务后端负责）。
//
// 所以：构建时可用 VITE_AGENT_BASE 指定；没指定时，
// https 页面一律走同源（交给 nginx 转发），http 页面保留原来的直连行为。
const CONFIGURED_AGENT_BASE =
  typeof import.meta !== 'undefined' && import.meta.env
    ? import.meta.env.VITE_AGENT_BASE
    : undefined

const AGENT_BASE = (() => {
  if (CONFIGURED_AGENT_BASE !== undefined) return CONFIGURED_AGENT_BASE
  if (isDev) return ''
  if (typeof location !== 'undefined' && location.protocol === 'https:') return ''
  return `${location.protocol}//${location.hostname}:8000`
})()

const agentUrl = (path) => AGENT_BASE + path

// 对话请求体。普通版 /agent/chat 和流式版 /agent/chat/stream 用同一份
function chatBody(text, sessionId, history, lastHouseNos, pending, extra) {
  return {
    text,
    session_id: sessionId,
    token: getToken(),
    history,
    last_house_nos: lastHouseNos,
    // 已经列出来几套（翻页要接着往下数），以及用户此刻打开的那一套
    // （「这套」指它，不指列表第一条）
    seen: extra.seen || 0,
    house_no: extra.houseNo || '',
    // 上一轮没做完的代办：助手问了哪个槽、已经填了什么、
    // 或者已经摆出的确认卡片。服务端不存会话态，由前端带着走。
    pending,
    // 随这句话发的照片，已传到本系统的地址（/static/uploads/...）
    images: extra.images || [],
    // 以什么身份办事。房东账号在助手顶部切「房东 / 租客」；智能体按账号角色核对
    identity: extra.identity || '',
  }
}

const TOKEN_KEY = 'rental_token'

/**
 * 令牌**按标签页隔离**，不是按浏览器。
 *
 * ## 这里修的是什么
 *
 * 原先令牌只存在 `localStorage` 里。`localStorage` 是**同源共享**的：
 * 同一个浏览器开两个标签页，A 登录 landlord1、B 登录 landlord2，
 * 第二次登录会把 A 的令牌一起换掉。A 的界面还停在原样，
 * 请求却已经是以 landlord2 的身份发出去的。
 *
 * 于是出现了三个看起来毫无关联的「bug」，其实是同一件事：
 *
 * - **「所有房东共享房源」**：A 页请求 /api/landlord/houses，
 *   带的是 landlord2 的令牌，看到的自然是 landlord2 的房源
 * - **「谁发的消息都显示成自己发的」**：`mine` 由服务端按令牌里的
 *   身份算。两个标签页共用一个令牌，两边看到的「我」就是同一个人
 * - **「私信只能发一条」**：第一条以 A 的身份发出，切换后第二条
 *   以 B 的身份发，而 B 不是这个会话的当事人 → 403
 *
 * 三条都不在会话逻辑里，接口和渲染其实都是对的——**错的是
 * 「现在是谁」这件事在标签页之间被共享了**。
 *
 * ## 为什么是 sessionStorage + localStorage 两层
 *
 * 只用 `sessionStorage`：每开一个新标签页都要重新登录，
 * 而「关了再开还在登录状态」是所有网站的默认行为，砍掉太粗暴。
 *
 * 所以：`sessionStorage` 存**这个标签页现在是谁**，
 * `localStorage` 存**上一次用的是谁**，只在标签页第一次要令牌时
 * 拿来做种子。此后这个标签页只认自己那一份，别处再登录也不影响它。
 */
function readSession() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function getToken() {
  const own = readSession()
  if (own) return own
  try {
    // 本标签页还没有身份：从「上次用的」继承一次，并**立刻落到
    // 自己的 sessionStorage 里**。不落的话，每次取令牌都会回头去读
    // localStorage，别的标签页一登录这里就跟着变了，等于没隔离
    const remembered = localStorage.getItem(TOKEN_KEY) || ''
    if (remembered) {
      try { sessionStorage.setItem(TOKEN_KEY, remembered) } catch { /* 忽略 */ }
    }
    return remembered
  } catch {
    // 隐私模式下两个 storage 都可能直接抛异常，此时按未登录处理
    return ''
  }
}

export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // 存不进去不影响本次会话使用，忽略
  }
  try {
    // localStorage 里那份只是「下次开新标签页默认用谁」。
    // 退出登录时一并清掉——否则新开标签页又会自动变回已登录
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // 忽略
  }
}

/**
 * 本标签页的令牌失效了（401）。
 *
 * **只清自己那一份，外加 localStorage 里恰好是同一个令牌的那份。**
 *
 * 不能直接 `setToken('')`：那会把「下次开新标签页默认用谁」一并清掉，
 * 而那一份可能是**另一个账号**的——一台电脑开两个窗口、后一个窗口
 * 登录了别人，此时前一个窗口的令牌过期，不该把后一个人的登录状态
 * 也一起抹掉。别的标签页自己的 `sessionStorage` 动不了，
 * 但新开的标签页会莫名其妙变成未登录。
 */
function expireToken(dead) {
  try {
    sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // 忽略
  }
  try {
    // 同一个令牌才清。别人的那份还好好的，留着
    if (localStorage.getItem(TOKEN_KEY) === dead) {
      localStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    // 忽略
  }
}

async function request(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response
  try {
    response = await fetch(url, { ...options, headers })
  } catch {
    // 网络层失败通常是后端没启动，给出可操作的提示而不是原始报错
    throw new Error('当前无连接，请确认三个服务都已启动')
  }

  if (response.status === 401 && url !== '/api/auth/login') {
    // 令牌过期或无效，清掉**本标签页**的状态，让用户重新登录。
    // 别的标签页可能登着另一个账号，不能顺手把它也踢下去
    expireToken(token)
    throw new Error('登录已失效，请重新登录')
  }

  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : ''
    } catch {
      // 响应体不是 JSON，忽略
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`)
  }
  return response.json()
}

function toQuery(params) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) {
      search.append(key, value)
    }
  })
  return search.toString()
}

const post = (url, body) =>
  request(url, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  // ---------------- 认证 ----------------
  register: (payload) => post('/api/auth/register', payload),
  login: (username, password) => post('/api/auth/login', { username, password }),
  me: () => request('/api/auth/me'),
  updateProfile: (payload) =>
    request('/api/auth/profile', { method: 'PUT', body: JSON.stringify(payload) }),
  // 退出所有设备：把令牌失效基线推到当前，之前签发的一律作废
  logoutAll: () => request('/api/auth/logout-all', { method: 'POST' }),

  changePassword: (oldPassword, newPassword) =>
    post('/api/auth/password', {
      old_password: oldPassword,
      new_password: newPassword,
    }),

  // ---------------- 房源 ----------------
  listHouses: (params = {}) => request(`/api/houses?${toQuery(params)}`),
  // 地图点位：同一套筛选条件 + 视野四角，返回比卡片更窄的载荷
  mapHouses: (params = {}) => request(`/api/houses/map?${toQuery(params)}`),
  getHouse: (houseNo) => request(`/api/houses/${houseNo}`),
  listDistricts: () => request('/api/districts'),
  houseReviews: (houseNo) => request(`/api/houses/${houseNo}/reviews`),

  // ---------------- 足迹与评价 ----------------
  history: () => request('/api/history'),
  clearHistory: () => request('/api/history', { method: 'DELETE' }),
  pendingReviews: () => request('/api/reviews/pending'),
  createReview: (payload) => post('/api/reviews', payload),
  stats: () => request('/api/stats'),

  // ---------------- 收藏 ----------------
  listFavorites: () => request('/api/favorites'),
  addFavorite: (houseNo) => request(`/api/favorites/${houseNo}`, { method: 'POST' }),
  removeFavorite: (houseNo) =>
    request(`/api/favorites/${houseNo}`, { method: 'DELETE' }),

  // ---------------- 预约 ----------------
  listAppointments: (houseNo) =>
    request(`/api/appointments?${toQuery({ house_no: houseNo })}`),
  myAppointments: (side = '') =>
    request(`/api/appointments/mine?${toQuery({ side })}`),
  updateAppointment: (id, status, reply = '') =>
    request(`/api/appointments/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status, reply }),
    }),

  // ---------------- 报修 ----------------
  listRepairs: (params = {}) => request(`/api/repairs?${toQuery(params)}`),

  // ---------------- 合同 ----------------
  // 合同档案：账单 + 租期内报修 + 状态时间线。三块一次取回——
  // 它们在界面上同时出现，分三次请求会出现「账单好了、报修还在转」的割裂
  contractDetail: (contractNo) =>
    request(`/api/contracts/${contractNo}/detail`),

  // side：'tenant' 只要我承租的，'landlord' 只要我出租的，不传是两者都要（D3）
  // B5：这套房可以签给谁——在这套房上约过看房或被带看过的人
  contractCandidates: (houseNo) =>
    request(`/api/landlord/houses/${encodeURIComponent(houseNo)}/candidates`),
  myContracts: (status = '', side = '') =>
    request(`/api/contracts/mine?${toQuery({ status, side })}`),
  getContract: (no) => request(`/api/contracts/${no}`),
  createContract: (payload) => post('/api/contracts', payload),
  // 改一份还没生效的合同：月租、押金、佣金比例、日期都能改，不填的不动
  updateContract: (no, payload) =>
    request(`/api/contracts/${no}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  activateContract: (no) => post(`/api/contracts/${no}/activate`, {}),
  endContract: (no, reason = '', deduct = 0) =>
    post(`/api/contracts/${no}/end`, { reason, deduct }),
  renewContract: (no, months = 12) =>
    post(`/api/contracts/${no}/renew`, { months }),
  // 撤回自己提的退租 / 续租申请，或拒绝对方提的
  withdrawContractRequest: (no) =>
    request(`/api/contracts/${no}/request`, { method: 'DELETE' }),

  // ---------------- 账单 ----------------
  // side：tenant 我要交的 / landlord 我该收的（D3）。不传就是两份混在一起
  myBills: (status = '', side = '') => request(`/api/bills/mine?${toQuery({ status, side })}`),
  payBill: (no, method = 'alipay', idempotencyKey = '') =>
    post(`/api/bills/${no}/pay`, {
      method,
      idempotency_key: idempotencyKey || `web-${no}-${Date.now()}`,
    }),
  // F1：演示网关的「用户在支付宝点了确认付款」这一下。
  //
  // 真实链路是跳到支付宝、支付宝回调平台。我们没有支付宝，
  // 所以由这个接口代替那一步——**整条链因此不需要联网也能走完**。
  mockPay: (tradeNo, result = 'success') =>
    post(`/api/payments/${tradeNo}/mock-pay`, { result }),
  paymentStatus: (tradeNo) => request(`/api/payments/${tradeNo}/status`),
  paymentReceipt: (tradeNo) => request(`/api/payments/${tradeNo}`),
  myRefunds: () => request('/api/refunds/mine'),
  // 退租结算预览：如果今天退租，押金退多少、预付租金退多少、哪些账单作废、还欠多少。
  // 和真正退租用的是同一份计算，弹窗里看到的数就是确认之后的数
  contractSettlement: (no, deduct = 0) =>
    request(`/api/contracts/${encodeURIComponent(no)}/settlement?${toQuery({ deduct })}`),
  // 收支流水：缴费成功的与退回的每一笔。side 为 tenant / landlord，管理员不传看全平台
  moneyRecords: (side = '') => request(`/api/money/records?${toQuery({ side })}`),

  // ---------------- 消息 ----------------
  listMessages: (unreadOnly = false) =>
    request(`/api/messages?${toQuery({ unread_only: unreadOnly })}`),
  readMessage: (id) => request(`/api/messages/${id}/read`, { method: 'PATCH' }),
  readAllMessages: () => post('/api/messages/read-all', {}),

  // ---------------- 公告 ----------------
  listNotices: () => request('/api/notices'),

  // ---------------- 房东端 ----------------
  landlordDashboard: () => request('/api/landlord/dashboard'),
  landlordHouses: () => request('/api/landlord/houses'),
  landlordTenants: () => request('/api/landlord/tenants'),
  // 一位租客和我之间的合同、账单、报修
  landlordTenant: (id) => request(`/api/landlord/tenants/${id}`),
  remindTenant: (id) => post(`/api/landlord/tenants/${id}/remind`, {}),
  updateRepair: (id, status) =>
    request(`/api/repairs/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  // 管理员就一张工单给房东（to=landlord）或报修人（to=tenant）发站内消息
  adminRepairMessage: (id, to, content) =>
    post(`/api/admin/repairs/${id}/message`, { to, content }),
  createHouse: (payload) => post('/api/houses', payload),
  updateHouse: (houseNo, payload) =>
    request(`/api/houses/${houseNo}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  setHouseStatus: (houseNo, status) =>
    request(`/api/houses/${houseNo}/status?${toQuery({ status })}`, {
      method: 'PATCH',
    }),

  // ---------------- 管理员端 ----------------
  adminHouses: (params = {}) => request(`/api/admin/houses?${toQuery(params)}`),
  reviewHouse: (houseNo, approved, reason = '') =>
    request(`/api/houses/${houseNo}/review`, {
      method: 'PATCH',
      body: JSON.stringify({ approved, reason }),
    }),
  adminUsers: (params = {}) => request(`/api/admin/users?${toQuery(params)}`),
  // 一个人的全貌：作为租客 / 房东 / 中介各做过什么
  adminUserOverview: (id) => request(`/api/admin/users/${id}/overview`),
  adminContracts: (status = '') =>
    request(`/api/admin/contracts?${toQuery({ status })}`),
  adminAppointments: (status = '') =>
    request(`/api/admin/appointments?${toQuery({ status })}`),
  adminNotices: () => request('/api/admin/notices'),
  createNotice: (payload) => post('/api/notices', payload),
  toggleNotice: (id, published) =>
    request(`/api/notices/${id}?${toQuery({ published })}`, { method: 'PATCH' }),
  updateUser: (id, payload) =>
    request(`/api/admin/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  // 图片上传。走 FormData，不能带 Content-Type——
  // 分隔符由浏览器生成，手动设了反而会让后端解不出来。
  uploadImage: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const headers = {}
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`

    let response
    try {
      response = await fetch('/api/upload', { method: 'POST', headers, body: form })
    } catch {
      throw new Error('当前无连接，请确认三个服务都已启动')
    }
    if (!response.ok) {
      let detail = ''
      try {
        detail = (await response.json()).detail || ''
      } catch {
        detail = ''
      }
      throw new Error(detail || `上传失败（${response.status}）`)
    }
    return response.json()
  },

  houseImages: (houseNo) => request(`/api/houses/${houseNo}/images`),
  // 改一张房源图的说明文字。这一列原先 324 条全是空的——
  // 上传接口一直收 caption，而这里永远传 ''，没有任何入口能填。
  setHouseImageCaption: (houseNo, imageId, caption) =>
    request(`/api/houses/${encodeURIComponent(houseNo)}/images/${imageId}/caption`,
      { method: 'POST', body: JSON.stringify({ caption }) }),

  addHouseImage: (houseNo, url, caption = '') =>
    request(`/api/houses/${houseNo}/images`,
      { method: 'POST', body: JSON.stringify({ url, caption }) }),
  deleteHouseImage: (houseNo, id) =>
    request(`/api/houses/${houseNo}/images/${id}`, { method: 'DELETE' }),
  setHouseCover: (houseNo, id) =>
    request(`/api/houses/${houseNo}/images/${id}/cover`, { method: 'POST' }),

  repairImages: (repairId) => request(`/api/repairs/${repairId}/images`),
  addRepairImage: (repairId, url) =>
    request(`/api/repairs/${repairId}/images`,
      { method: 'POST', body: JSON.stringify({ url }) }),

  // F-09 维修数据
  repairWorkers: () => request('/api/repair-workers'),
  createWorker: (payload) =>
    request('/api/repair-workers', { method: 'POST', body: JSON.stringify(payload) }),
  updateWorker: (id, payload) =>
    request(`/api/repair-workers/${id}`,
      { method: 'PATCH', body: JSON.stringify(payload) }),
  repairItems: () => request('/api/repair-items'),
  createItem: (payload) =>
    request('/api/repair-items', { method: 'POST', body: JSON.stringify(payload) }),
  updateItem: (id, payload) =>
    request(`/api/repair-items/${id}`,
      { method: 'PATCH', body: JSON.stringify(payload) }),
  repairRecords: () => request('/api/repair-records'),
  repairStats: () => request('/api/repair-stats'),
  closeRepair: (repairId, payload) =>
    request(`/api/repairs/${repairId}/record`,
      { method: 'POST', body: JSON.stringify(payload) }),

  // F-02 户主档案
  adminLandlords: (params) => request(`/api/admin/landlords?${toQuery(params)}`),

  // D1 角色升级申请（房东 / 经纪人）
  adminRoleRequests: (status = 'pending') =>
    request(`/api/admin/role-requests?${toQuery({ status })}`),
  reviewRoleRequest: (id, action, note = '') =>
    post(`/api/admin/role-requests/${id}`, { action, note }),

  // M-08 内容审核
  adminFlags: (status = 'pending') =>
    request(`/api/admin/flags?${toQuery({ status })}`),
  handleFlag: (id, action) =>
    request(`/api/admin/flags/${id}?${toQuery({ action })}`, { method: 'PATCH' }),

  // M-09 系统配置
  publicConfig: () => request('/api/config'),
  adminConfig: () => request('/api/admin/config'),
  updateConfig: (items) =>
    request('/api/admin/config', { method: 'PATCH', body: JSON.stringify({ items }) }),

  // M-07 操作日志
  adminLogs: (params) => request(`/api/admin/logs?${toQuery(params)}`),
  adminLogActions: () => request('/api/admin/logs/actions'),

  // C-1 租客与房东会话。
  // `demandSummary` 是 J-09-a 的需求摘要，服务端会把它落成会话的
  // 第一条消息——中介一进来就看得到，不必让租客再说一遍
  startConversation: (houseNo, demandSummary = '') =>
    request('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ house_no: houseNo,
                             demand_summary: demandSummary }),
    }),
  conversations: () => request('/api/conversations'),
  // after_id 用于轮询增量，不传就是整段重拉
  conversationLines: (id, afterId = 0) =>
    request(`/api/conversations/${id}/lines?${toQuery({ after_id: afterId })}`),
  sendLine: (id, content) =>
    request(`/api/conversations/${id}/lines`, {
      method: 'POST', body: JSON.stringify({ content }),
    }),
  readConversation: (id) =>
    request(`/api/conversations/${id}/read`, { method: 'POST' }),

  // A-5 房源详情的补充信息
  houseLandlord: (houseNo) => request(`/api/houses/${houseNo}/landlord`),
  similarHouses: (houseNo) => request(`/api/houses/${houseNo}/similar`),

  // ---------------- 智能体 ----------------
  // AI-01 自然语言找房
  // K-06：**只解析，不取数**。网站拿到条件后自己走 /api/houses，
  // 检索路径因此只剩一条，智能体挂了也只是少了个便利入口。
  //
  // 原先这里还有一个 `agentSearch`（打 /agent/search，一次拿回条件和房源）。
  // 统一检索路径之后前端不再需要它，就删掉了——留着会让人以为
  // 有两条取数路径可选，而那正是这次改造要消灭的东西。
  // 服务端 /agent/search 仍然保留，聊天面板走 supervisor 时会用到。
  agentParse: (text) => post(agentUrl('/agent/parse'), { text }),

  // ---------------- K-13 身份升级 ----------------
  landlordEligibility: () => request('/api/auth/landlord-eligibility'),
  becomeLandlord: () => post('/api/auth/become-landlord', {}),
  // 退回租客：名下还有在架房源或没结束的合同时后端会列出来拦下
  revertToTenant: () => post('/api/auth/revert-to-tenant', {}),

  // ---------------- K-11 找回密码 ----------------
  forgotPassword: (username) => post('/api/auth/forgot', { username }),
  // B1：管理员为某个用户签发一次性重置码（邮箱通道不可用时的人工兜底）
  issueResetCode: (userId) => post(`/api/admin/users/${userId}/reset-code`, {}),
  resetPassword: (token, newPassword) =>
    post('/api/auth/reset', { token, new_password: newPassword }),

  // ---------------- K-09 保存搜索 ----------------
  savedSearches: () => request('/api/saved-searches'),
  createSavedSearch: (conditions, name = '') =>
    post('/api/saved-searches', { conditions, name }),
  updateSavedSearch: (id, patch) =>
    request(`/api/saved-searches/${id}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    }),
  deleteSavedSearch: (id) =>
    request(`/api/saved-searches/${id}`, { method: 'DELETE' }),

  // ---------------- K-08 房源举报 ----------------
  reportKinds: () => request('/api/report-kinds'),
  createReport: (payload) => post('/api/reports', payload),
  myReports: () => request('/api/reports/mine'),
  adminReports: (status = 'pending') =>
    request(`/api/admin/reports?${toQuery({ status })}`),
  handleReport: (no, status, conclusion = '') =>
    request(`/api/admin/reports/${no}`, {
      method: 'PATCH',
      body: JSON.stringify({ status, conclusion }),
    }),

  // AI-06 智能预约看房
  agentBooking: (text, houseNo, applicant = {}) =>
    post(agentUrl('/agent/booking'), {
      text,
      house_no: houseNo,
      token: getToken(),
      xingming: applicant.name || '',
      shouji: applicant.phone || '',
    }),

  // AI-04 报修工单智能分派。
  //
  // `images` 是已经上传到本系统的照片地址（`/static/uploads/...`）。
  // 智能体那边只认这种地址和 data URI，外链一律丢弃——识图是由模型
  // 那一端去取图的，放任意地址进去等于让用户指定「服务器去访问哪里」。
  agentRepair: (text, houseNo, applicant = {}, submit = true, images = []) =>
    post(agentUrl('/agent/repair'), {
      text,
      house_no: houseNo,
      xingming: applicant.name || '',
      shouji: applicant.phone || '',
      submit,
      images,
      // 以报修人的身份提交：后端只收在住租客与管房的人
      token: getToken(),
    }),

  // 看一张房源照片，写一句图片说明的**草稿**。
  // 只出草稿不落库——图片说明是挂在房源上给租客看的公开文案，
  // 得房东看过、点保存才走 setHouseImageCaption。
  agentImageCaption: (imageUrl) =>
    post(agentUrl('/agent/image-caption'), { images: [imageUrl] }),

  // AI-02 合同智能审阅
  agentContractReview: (text) =>
    post(agentUrl('/agent/contract/review'), { text }),

  // AI-07 房源智能推荐。令牌要显式传给智能体：
  // 智能体是独立进程，不持有身份，只能靠前端把令牌转发过去。
  // 推荐理由：大模型写的一句话，列表先出、它后到
  agentRecommendExplain: (limit = 6) =>
    post(agentUrl('/agent/recommend/explain'), { token: getToken(), limit }),
  agentRecommend: (limit = 6) =>
    post(agentUrl('/agent/recommend'), { token: getToken(), limit }),

  // AI-05 经营分析报告
  // 带上令牌：经营分析里的钱数要管理员身份才取得到（/api/stats 分两档）。
  // 这个面板本来就只在管理后台里
  agentReport: () => post(agentUrl('/agent/report'), { token: getToken() }),

  // AI-02 审阅结论回写。只存摘要，明细每次都能重算——
  // 存下来只会在规则升级后变成一份过期的旧结论。
  saveReviewResult: (contractNo, summary) =>
    request(`/api/contracts/${contractNo}/review-result`,
      { method: 'POST', body: JSON.stringify(summary) }),

  // L-10 定价建议
  pricingSuggest: (params) => request(`/api/pricing/suggest?${toQuery(params)}`),

  // AI-08 房源描述自动生成。字段由前端原样带过去——
  // 房源还没提交，库里根本没有它，智能体查不到。
  agentDescribe: (house) => post(agentUrl('/agent/describe'), house),

  // AI-09 多轮对话。history 是未登录时的兜底：
  // 登录用户的上下文存在后端，未登录用户由前端自己带着走。
  agentChat: (text, sessionId, history = [], lastHouseNos = [], pending = null,
              extra = {}) =>
    post(agentUrl('/agent/chat'), chatBody(text, sessionId, history, lastHouseNos, pending, extra)),

  // 同一轮对话的流式版本（SSE）：边查边告诉你在做哪一步，回答一个字一个字出来。
  // onEvent 收到 {type:'step', label} / {type:'delta', text} / {type:'reset'}；
  // 返回的 Promise 最后给出和 agentChat 一模一样的结果。
  //
  // 浏览器自带的 EventSource 只能发 GET，带不了请求体，所以这里用 fetch
  // 读响应流，自己按 SSE 的格式（空行分隔、event:/data: 开头）切开。
  agentChatStream: async (text, sessionId, history = [], lastHouseNos = [], pending = null,
                          extra = {}, onEvent = () => {}) => {
    let response
    try {
      response = await fetch(agentUrl('/agent/chat/stream'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(chatBody(text, sessionId, history, lastHouseNos, pending, extra)),
      })
    } catch {
      throw Object.assign(new Error('流式接口连不上'), { streamUnsupported: true })
    }
    // 旧版智能体没有这个接口（404），调用方退回 agentChat
    if (!response.ok || !response.body) {
      throw Object.assign(new Error(`流式接口返回 ${response.status}`), { streamUnsupported: true })
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let cut
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        const data = block.split('\n').find((line) => line.startsWith('data:'))
        if (!data) continue                       // 心跳（: ping）之类的注释行
        const event = JSON.parse(data.slice(5))
        if (event.type === 'done') return event.result
        if (event.type === 'error') throw new Error(event.message)
        onEvent(event)
      }
    }
    throw new Error('连接中断，没收到完整回复')
  },

  chatHistory: (sessionId) =>
    request(`/api/chat-messages?${toQuery({ session_id: sessionId })}`),
  clearChat: (sessionId) =>
    request(`/api/chat-messages?${toQuery({ session_id: sessionId })}`,
      { method: 'DELETE' }),

  // 只做意图识别，用于展示智能体的决策过程
  agentRoute: (text) => post(agentUrl('/agent/route'), { text }),

  // ---------------------------------------------------------- AI-03 智能客服

  // 知识型问答走检索，事务型问题（「我还欠多少房租」）由智能体去后端查真实数据。
  // token 必须带上，否则事务型问题查不到「这个人自己的」记录。
  agentSupport: (text, sessionId = '', history = [], houseNo = '') =>
    post(agentUrl('/agent/support'), {
      text,
      token: getToken(),
      session_id: sessionId,
      history,
      house_no: houseNo,
    }),

  // 知识库现状，答辩时用来当场展示「库里到底有什么」
  supportKnowledge: () => request(agentUrl('/agent/support/knowledge')),

  // 执行一个已被用户确认的代办操作。
  // **只有用户点了确认按钮才允许调这个接口**——对话本身永远不会执行写操作，
  // 它最多返回一张确认卡片。这条边界在前后端两侧都要守住。
  supportConfirm: (name, params) =>
    post(agentUrl('/agent/support/confirm'), {
      name,
      params,
      token: getToken(),
    }),

  // 可代办操作清单
  supportActions: () => request(agentUrl('/agent/support/actions')),

  // 会话开场白。带上令牌看这个人有什么没办完，带上房源编号则围绕那套房开场。
  supportGreeting: (houseNo = '', identity = '') =>
    post(agentUrl('/agent/support/greeting'), {
      token: getToken(), house_no: houseNo, identity,
    }),

  // 一条回答有用 / 没用。「没用」是补语料时第一个该看的清单
  supportFeedback: (logId, value, sessionId = '') =>
    post('/api/support/feedback', { log_id: logId, value, session_id: sessionId }),

  // 转人工：把答不上来的问题留给管理员
  supportHandoff: (question, reason = '', context = '') =>
    post('/api/support/handoff', { question, reason, context }),
  myHandoffs: () => request('/api/support/handoffs/mine'),

  // 帮助中心页面直接读已发布的知识条目
  faqs: (category = '') => request(`/api/faqs?${toQuery({ category })}`),

  // ---- 管理端 ----
  adminFaqs: (keyword = '') =>
    request(`/api/admin/faqs?${toQuery({ keyword })}`),
  createFaq: (payload) => post('/api/faqs', payload),
  updateFaq: (id, payload) =>
    request(`/api/faqs/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  toggleFaq: (id, published) =>
    request(`/api/faqs/${id}?${toQuery({ published })}`, { method: 'PATCH' }),
  deleteFaq: (id) => request(`/api/faqs/${id}`, { method: 'DELETE' }),

  adminHandoffs: (status = '') =>
    request(`/api/admin/handoffs?${toQuery({ status })}`),
  replyHandoff: (handoffNo, reply) =>
    post(`/api/admin/handoffs/${handoffNo}/reply`, { reply }),

  supportAnalytics: (days = 30) =>
    request(`/api/admin/support/analytics?${toQuery({ days })}`),

  // ---- 中介 / 经纪人（J 系列） ----
  //
  // **命名注意**：这个文件里的 `agentXxx` 全部指「AI 智能体微服务」
  // （/agent/chat、/agent/parse 那一套，跑在 8000 端口）。中介是完全
  // 不相干的一个业务概念，如果也叫 agent，往后任何人读到 `api.agentXxx`
  // 都得先愣一下判断是哪个。所以前端这一侧统一用 `broker` 前缀，
  // 服务端路径保持 /api/agent/*（那边没有歧义，它没有 AI 微服务的路由）。

  brokerDashboard: () => request('/api/agent/dashboard'),
  brokerProfile: () => request('/api/agent/profile'),
  updateBrokerProfile: (payload) =>
    request('/api/agent/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  submitLicense: (licenseNo, images = []) =>
    post('/api/agent/license', { license_no: licenseNo, license_images: images }),

  // 委托。房东和中介调的是同一组接口，靠 role 参数区分「我是哪一方」
  delegations: (params = {}) => request(`/api/delegations?${toQuery(params)}`),
  // 我（房东）应付给中介的佣金，与「付清后标记已结清」。钱线下给，这里只记状态
  owedCommissions: () => request('/api/landlord/commissions'),
  settleCommission: (id) => request(`/api/commissions/${id}/settle`, { method: 'POST' }),
  // 管理员取消一笔佣金（纠纷处理），必须写原因
  cancelCommission: (id, reason) =>
    request(`/api/commissions/${id}/cancel`, { method: 'POST', body: JSON.stringify({ reason }) }),
  createDelegation: (payload) => post('/api/delegations', payload),

  // 由房源负责人主动联系某位租客（J-07）。租客设了免打扰会被服务端拒绝，
  // 前端只负责把按钮置灰——**按钮是提示，接口才是边界**
  contactTenant: (houseNo, tenantUsername) =>
    post('/api/conversations',
      { house_no: houseNo, tenant_username: tenantUsername }),
  // accept 走请求体而不是查询串：服务端是 `Body(embed=True)`。
  // 拒绝不带理由——那个接口不收，与其在这里编一个参数送过去被忽略，
  // 不如让签名如实反映它能做什么
  respondDelegation: (id, accept) =>
    request(`/api/delegations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accept }),
    }),
  endDelegation: (id, reason = '') =>
    post(`/api/delegations/${id}/end`, { reason }),

  openHouses: (limit = 20) => request(`/api/agent/open-houses?${toQuery({ limit })}`),

  viewings: (params = {}) => request(`/api/viewings?${toQuery(params)}`),
  createViewing: (payload) => post('/api/viewings', payload),

  brokerCommissions: (status = '') =>
    request(`/api/agent/commissions?${toQuery({ status })}`),

  // ---- 管理端的中介治理（J-11） ----
  adminAgentQueue: () => request('/api/admin/agent-queue'),
  reviewLicense: (userId, approved, reason = '') =>
    request(`/api/admin/agents/${userId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved, reason }),
    }),
  adminAgents: (params = {}) => request(`/api/admin/agents?${toQuery(params)}`),
  adminAgentStats: () => request('/api/admin/agent-stats'),

  // 智能派单（J-09-b）。**全部由规则算**，返回里带着权重与逐条理由，
  // 页面把它们一起显示出来——排序规则不该是黑箱
  suggestAgents: (houseNo) =>
    request(`/api/delegations/suggest?${toQuery({ house_no: houseNo })}`),

  // F1：不依赖房源的中介名录。委托面板一进去就用它列人，
  // 不用先选房、再点「帮我推荐」
  agentDirectory: (params = {}) =>
    request(`/api/agents/directory?${toQuery(params)}`),

  // ---- J-09 的两个智能体入口（跑在 8000，与业务后端分开）----

  // 需求摘要交接（J-09-a）：把租客的一句话整理成摘要，
  // 发起咨询时一并带给中介，省掉「请您再描述一遍需求」那一轮
  brokerHandoff: (text) =>
    post(agentUrl('/agent/handoff'), { text, token: getToken() }),

  // 带看纪要整理（J-09-c）：口述几句 → 结构化纪要
  viewingSummary: (text) =>
    post(agentUrl('/agent/viewing-summary'), { text }),
}
