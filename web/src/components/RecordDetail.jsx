/**
 * 记录详情（D5）。
 *
 * 个人中心里的每条记录，列表上只放**认得出是哪一条**所必需的几个字段：
 * 账单是「租金 · 2026-09 · 3200 元」，预约是「房源 + 时间 + 状态」。
 * 剩下的——账单编号、交易流水号、AI 初判、处理人、备注——原先无处可看，
 * 用户要报账单号给客服时得自己去猜。
 *
 * 两个刻意的选择：
 *
 * 1. **就地展开，不再叠一层弹窗。** 个人中心本身已经是抽屉，
 *    抽屉上再开对话框就是第三层：关的时候要点两次，Esc 也说不清关哪个。
 *    展开的内容接在原记录下面，上下文一直在。
 * 2. **空字段整行不画**，而不是画一行「—」。一屏十几个「暂无」
 *    并不比没有这行信息更有用，只是把真有内容的那几行挤到了下面。
 */

/** 一个值算不算「没有」。0 是有的——「逾期 0 天」「浏览 0 次」都是信息。 */
function has(v) {
  if (v === null || v === undefined) return false
  if (typeof v === 'string') return v.trim() !== ''
  if (Array.isArray(v)) return v.length > 0
  return true
}

/**
 * rows：[标签, 值] 或 [标签, 值, { mono, wide, money }]
 *   mono  等宽显示，用于编号、流水号这类要照着念或复制的字段
 *   wide  值独占一行，用于故障描述、备注这类长文本
 *   money 金额高亮
 */
export default function RecordDetail({ rows = [], note = '' }) {
  const kept = rows.filter(([, v]) => has(v))
  if (kept.length === 0 && !note) return null

  return (
    <div className="rec-detail">
      <dl className="rec-dl">
        {kept.map(([label, value, opt = {}]) => (
          <div
            className={`rec-row${opt.wide ? ' wide' : ''}`}
            key={label}
          >
            <dt>{label}</dt>
            <dd className={[opt.mono ? 'mono' : '', opt.money ? 'money' : '']
              .filter(Boolean).join(' ')}>
              {String(value)}
            </dd>
          </div>
        ))}
      </dl>
      {/* note 是对这条记录的一句解释（「本项目不接真实支付」之类），
          不属于任何一个字段，所以放在字段表外面 */}
      {note && <p className="rec-note">{note}</p>}
    </div>
  )
}

/** 展开/收起的开关。文案跟着状态走，而不是一个不说明方向的箭头。 */
export function DetailToggle({ open, onClick }) {
  return (
    <button
      type="button"
      className={`rec-more${open ? ' on' : ''}`}
      onClick={onClick}
      aria-expanded={open}
    >
      {open ? '收起' : '详情'}
      <i aria-hidden="true" />
    </button>
  )
}
