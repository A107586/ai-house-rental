/**
 * 后台首屏的待办条（F5）。
 *
 * 中介后台七个页签、管理后台十七个，一字排开，全都长得一样。页签列表
 * 回答的是「这里有些什么」，回答不了**「现在该我做什么」**——真正
 * 要紧的那三件事（今天有几场带看、几份资质压着没审、几条举报没处理）
 * 和「系统配置」并排站着，看不出轻重。
 *
 * 所以在页签之上放一条待办。三条规则：
 *
 * 1. **只放要人动手的事**，不放统计。「本月成交 12 单」是结果，
 *    看了不用做什么；「3 份资质待审」是有人在等你。
 * 2. **数字为 0 的照样显示，但压暗。** 隐藏会让这条待办的宽度每次
 *    刷新都在变，人记不住哪一格是哪件事；压暗既表明「这件事清空了」，
 *    位置又还在。
 * 3. 点一格就跳到对应页签——**待办必须能直接下手**，
 *    看到「3 条待审」还得自己去页签里找，等于只提醒不帮忙。
 */
export default function FocusBar({ items = [], onPick, hint = '' }) {
  const shown = items.filter(Boolean)
  if (shown.length === 0) return null

  const urgent = shown.some((it) => it.n > 0 && it.urgent)

  return (
    <div className={`focus-bar${urgent ? ' has-urgent' : ''}`}>
      <div className="focus-row">
        {shown.map((it) => (
          <button
            type="button"
            key={`${it.key}-${it.label}`}
            className={`focus-cell${it.n > 0 ? ' on' : ''}${it.urgent && it.n > 0 ? ' urgent' : ''}`}
            onClick={() => onPick?.(it.key)}
          >
            <span className="focus-n">{it.n ?? 0}</span>
            <span className="focus-k">{it.label}</span>
            {/* 一句话说清这个数字是什么口径。「待办 5」——**五个什么？**
                后台里同一个词在不同页签下含义并不一样 */}
            {it.hint && <span className="focus-h">{it.hint}</span>}
          </button>
        ))}
      </div>
      {hint && <p className="focus-note">{hint}</p>}
    </div>
  )
}
