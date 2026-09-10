/**
 * 聊天头像（E1）。
 *
 * 原先两个聊天面板都只有裸气泡，靠左右对齐区分谁在说话。左右对齐能
 * 分「我 / 不是我」，分不了「不是我的那边是谁」——委托期间房东一侧
 * 同时坐着房东本人和受托中介，一句「这个可以商量」出自谁，
 * 只看对齐方向是读不出来的。
 *
 * 助手用图形标记，人用姓氏字符：
 *
 * - 助手是同一个「它」，每条消息都该是同一个标记，用首字反而会
 *   随名字变；
 * - 人各不相同，而**姓氏是这里唯一拿得到的身份**——聊天对象不上传
 *   头像，用户名是 landlord1 这种东西。首字加一个由名字定死的底色，
 *   同一个人每次进来颜色一样，混合会话里就能靠颜色跟住同一个人。
 *
 * 没有用 emoji。emoji 在 Windows / macOS / Android 上是三套完全不同的
 * 画风，放进一个自己定了色板的界面里，等于让别人替你决定这一块长什么样。
 */

/** 一组低饱和底色。深色字压在上面才读得清，纯色圆片上的白字在小尺寸会糊。 */
const TINTS = [
  ['#e3f0ff', '#1a6fdb'],
  ['#e6f4ea', '#1e7d44'],
  ['#fdf0e3', '#c2681a'],
  ['#f0e9fb', '#6b46c1'],
  ['#fde8ec', '#c0384f'],
  ['#e6f6f7', '#127a80'],
]

/** 同一个名字每次都要落在同一个颜色上，所以哈希，不能用随机或下标。 */
function tintOf(seed) {
  let sum = 0
  for (let i = 0; i < seed.length; i += 1) sum = (sum + seed.charCodeAt(i) * (i + 1)) % 9973
  return TINTS[sum % TINTS.length]
}

/**
 * kind: 'ai' 助手 | 'me' 自己 | 'other' 对方
 * name: 显示名，取首字
 */
export default function ChatAvatar({ kind = 'other', name = '', title }) {
  if (kind === 'ai') {
    return (
      <span className="cav cav-ai" title={title || '租房助手'} aria-hidden="true">
        {/* 一个对话气泡里嵌小方块的标记：说话 + 在查东西。
            比一个机器人脑袋更贴近它实际做的事，也更耐看 */}
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.4A.5.5 0 0 1 5 19v-3H6.5A2.5 2.5 0 0 1 4 13.5v-7Z"
                fill="currentColor" opacity=".16" />
          <path d="M6.5 4.75h11A1.75 1.75 0 0 1 19.25 6.5v7a1.75 1.75 0 0 1-1.75 1.75H9.74l-3.99 3.19V15.25H6.5A1.75 1.75 0 0 1 4.75 13.5v-7A1.75 1.75 0 0 1 6.5 4.75Z"
                stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
          <rect x="8" y="8.4" width="3" height="3" rx=".6" fill="currentColor" />
          <rect x="13" y="8.4" width="3" height="3" rx=".6" fill="currentColor" />
        </svg>
      </span>
    )
  }

  const label = (name || '').trim()
  const first = label ? Array.from(label)[0] : '·'
  const [bg, fg] = tintOf(label || String(kind))

  return (
    <span
      className={`cav cav-${kind}`}
      style={{ background: bg, color: fg }}
      title={title || label}
      aria-hidden="true"
    >
      {first}
    </span>
  )
}
