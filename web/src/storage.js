/**
 * 按「现在登录的是谁」隔开的本地存储。
 *
 * ## 起因
 *
 * 原先几个键都是写死的常量：`rental_chat_draft`、`rental_chat_session`、
 * `rental_search_history`。一台电脑一份，**跟账号没有关系**。
 * 退出登录时也没人清它们。
 *
 * 于是在同一个浏览器里换个账号登录，助手面板恢复出来的是
 * **上一个人的整段对话**——里面有他的预算、看过哪几套房、
 * 报修留的电话；`pending` 里甚至可能是一张还没点的确认卡片，
 * 上面写着上一个人的账单号和金额。
 *
 * 后端这一侧是干净的：`GET /api/chat-messages` 同时按 `user_id` 过滤，
 * 别人的记录拿不走。**漏的是浏览器本地这一份。**
 *
 * ## 两条规矩
 *
 * 1. **键里带上身份**，而不是「在退出时记得清一下」。
 *    清理这种事漏一个入口就前功尽弃——而入口不止「退出」一个：
 *    令牌过期、换标签页、直接换账号登录都不会走 `logout()`。
 *    键里带了身份，就算一次都不清，也串不了。
 *    顺带还多一个好处：各自的对话各自留着，换回来能接着聊。
 *
 * 2. **未登录也是一种身份**，记作 `anon`。匿名会话不该被登录后的
 *    对话覆盖，反过来也一样。
 *
 * 令牌不走这里。它有自己的一套（见 api.js）：`sessionStorage` 存
 * 「这个标签页现在是谁」，`localStorage` 只存「上次用的是谁」。
 */

/** 本次识别不出身份时用的名字。`0` 也是合法 id，所以只判 null / undefined。 */
const ANON = 'anon'

/** 修复之前留下的、没有身份的旧键。**必须删掉**，见 `purgeLegacy`。 */
const LEGACY_KEYS = [
  'rental_chat_draft',
  'rental_chat_session',
  'rental_search_history',
]

export function whoami(user) {
  const id = user?.id
  return id === null || id === undefined ? ANON : String(id)
}

/** `'rental_chat_draft'` + 用户 7 → `'rental_chat_draft:7'`。 */
export function scopedKey(base, user) {
  return `${base}:${whoami(user)}`
}

/**
 * 读一个 JSON。读不到、坏了、隐私模式下抛异常，一律返回 `fallback`。
 *
 * 坏掉的 JSON 不能让面板打不开：一段存坏的草稿把整个助手废掉，
 * 比丢掉这段草稿严重得多。
 */
export function readJSON(key, fallback = null) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const value = JSON.parse(raw)
    return value === null || value === undefined ? fallback : value
  } catch {
    return fallback
  }
}

/** 写一个 JSON。存不下（隐私模式、配额满）就静默放弃，不影响当前这轮操作。 */
export function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export function readText(key, fallback = '') {
  try {
    return localStorage.getItem(key) || fallback
  } catch {
    return fallback
  }
}

export function writeText(key, value) {
  try {
    localStorage.setItem(key, value)
    return true
  } catch {
    return false
  }
}

export function remove(key) {
  try {
    localStorage.removeItem(key)
  } catch {
    /* 隐私模式下删也会抛，忽略 */
  }
}

/**
 * 删掉修复之前留下的、没有身份的旧键。
 *
 * **这一步不是清理代码，是清理数据。** 那几个键里现在就躺着某个人的
 * 对话记录，而这台电脑后来可能已经换人用了。不删的话，它会在每个
 * 用户的浏览器里一直留到有人手动清缓存为止——升级了代码，
 * 泄出去的那份还在原地。
 *
 * 只在应用启动时调一次。加上身份之后新旧键不会重名，删了不会误伤。
 */
export function purgeLegacy() {
  LEGACY_KEYS.forEach(remove)
}
