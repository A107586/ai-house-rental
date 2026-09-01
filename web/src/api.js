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

  if (response.status === 401) {
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

  // ---------------- 房源 ----------------
  listHouses: (params = {}) => request(`/api/houses?${toQuery(params)}`),
  getHouse: (houseNo) => request(`/api/houses/${houseNo}`),
  listDistricts: () => request('/api/districts'),
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
  payBill: (no, method = 'alipay') => post(`/api/bills/${no}/pay`, { method }),

  // ---------------- 消息 ----------------
  listMessages: (unreadOnly = false) =>
    request(`/api/messages?${toQuery({ unread_only: unreadOnly })}`),
  readMessage: (id) => request(`/api/messages/${id}/read`, { method: 'PATCH' }),
  readAllMessages: () => post('/api/messages/read-all', {}),

  // ---------------- 公告 ----------------
  listNotices: () => request('/api/notices'),

  // ---------------- 智能体 ----------------
  // AI-01 自然语言找房
  agentSearch: (text, limit = 12) => post(agentUrl('/agent/search'), { text, limit }),

  // AI-06 智能预约看房
  agentBooking: (text, houseNo, applicant = {}) =>
    post(agentUrl('/agent/booking'), {
      text,
      house_no: houseNo,
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

  // 只做意图识别，用于展示智能体的决策过程
  agentRoute: (text) => post(agentUrl('/agent/route'), { text }),
}
