// 后端接口封装。
//
// 两种运行方式都要支持：
//
// 1. 开发模式（vite dev，5173 端口）
//    两类请求都用相对地址，由 vite.config.js 里的代理转发，享受热更新。
//
// 2. 生产模式（业务后端 8080 直接托管 web/dist）
//    /api 与页面同源，仍用相对地址；
//    /agent 在另一个进程上，必须补上绝对地址。
//    智能体服务的 CORS 已经放行了 8080 来源。
//
// 这样组员只要跑 `npm run build`，之后访问 8080 就是完整网站，
// 不需要再单独启动前端，也不会出现「访问 8080 只看到 Not Found」的困惑。

const DEV_PORT = '5173'
const isDev = typeof location !== 'undefined' && location.port === DEV_PORT

// 智能体服务地址：开发模式走代理，生产模式直连 8000
const AGENT_BASE = isDev
  ? ''
  : `${location.protocol}//${location.hostname}:8000`

const agentUrl = (path) => AGENT_BASE + path

const TOKEN_KEY = 'rental_token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    // 隐私模式下 localStorage 可能直接抛异常，此时按未登录处理
    return ''
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // 存不进去不影响本次会话使用，忽略
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
    throw new Error('连不上服务器，请确认三个服务都已启动')
  }

  if (response.status === 401 && url !== '/api/auth/login') {
    // 令牌过期或无效，清掉本地状态，让用户重新登录
    setToken('')
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
  changePassword: (oldPassword, newPassword) =>
    post('/api/auth/password', {
      old_password: oldPassword,
      new_password: newPassword,
    }),

  // ---------------- 房源 ----------------
  listHouses: (params = {}) => request(`/api/houses?${toQuery(params)}`),
  getHouse: (houseNo) => request(`/api/houses/${houseNo}`),
  listDistricts: () => request('/api/districts'),
  houseImages: (houseNo) => request(`/api/houses/${houseNo}/images`),
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
  myAppointments: () => request('/api/appointments/mine'),
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

  myContracts: (status = '') =>
    request(`/api/contracts/mine?${toQuery({ status })}`),
  getContract: (no) => request(`/api/contracts/${no}`),
  createContract: (payload) => post('/api/contracts', payload),
  activateContract: (no) => post(`/api/contracts/${no}/activate`, {}),
  endContract: (no, reason = '', deduct = 0) =>
    post(`/api/contracts/${no}/end`, { reason, deduct }),
  renewContract: (no, months = 12) =>
    post(`/api/contracts/${no}/renew`, { months }),

  // ---------------- 账单 ----------------
  myBills: (status = '') => request(`/api/bills/mine?${toQuery({ status })}`),
  payBill: (no, method = 'alipay', idempotencyKey = '') =>
    post(`/api/bills/${no}/pay`, {
      method,
      idempotency_key: idempotencyKey || `web-${no}-${Date.now()}`,
    }),
  paymentReceipt: (tradeNo) => request(`/api/payments/${tradeNo}`),
  myRefunds: () => request('/api/refunds/mine'),

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
  updateRepair: (id, status) =>
    request(`/api/repairs/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
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
      throw new Error('连不上服务器，请确认三个服务都已启动')
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

  // ---------------- 智能体 ----------------
  // AI-01 自然语言找房
  agentSearch: (text, limit = 12, page = 1) =>
    post(agentUrl('/agent/search'), { text, limit, page }),

  // AI-06 智能预约看房
  agentBooking: (text, houseNo, applicant = {}) =>
    post(agentUrl('/agent/booking'), {
      text,
      house_no: houseNo,
      token: getToken(),
      xingming: applicant.name || '',
      shouji: applicant.phone || '',
    }),

  // AI-04 报修工单智能分派
  agentRepair: (text, houseNo, applicant = {}, submit = true) =>
    post(agentUrl('/agent/repair'), {
      text,
      house_no: houseNo,
      xingming: applicant.name || '',
      shouji: applicant.phone || '',
      submit,
    }),

  // AI-02 合同智能审阅
  agentContractReview: (text) =>
    post(agentUrl('/agent/contract/review'), { text }),

  // AI-07 房源智能推荐。令牌要显式传给智能体：
  // 智能体是独立进程，不持有身份，只能靠前端把令牌转发过去。
  agentRecommend: (limit = 6) =>
    post(agentUrl('/agent/recommend'), { token: getToken(), limit }),

  // AI-05 经营分析报告
  agentReport: () => post(agentUrl('/agent/report'), {}),

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
  agentChat: (text, sessionId, history = [], lastHouseNos = [], pending = null) =>
    post(agentUrl('/agent/chat'), {
      text,
      session_id: sessionId,
      token: getToken(),
      history,
      last_house_nos: lastHouseNos,
      // 上一轮没做完的代办：助手问了哪个槽、已经填了什么、
      // 或者已经摆出的确认卡片。服务端不存会话态，由前端带着走。
      pending,
    }),

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

  // 一条回答有用 / 没用。「没用」是补语料时第一个该看的清单
  supportFeedback: (logId, value) =>
    post('/api/support/feedback', { log_id: logId, value }),

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
}
