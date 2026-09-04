/**
 * 带分母的比率（方案 8.5 规矩三）。
 *
 * 后端所有比率一律返回 `{ rate, hit, total }` 而不是一个裸的百分数，
 * 前端就必须把分母显示出来，否则那条约定在服务端做了也白做。
 *
 * 为什么较真这件事：「成交率 25%」在带看了 4 次时和带看了 400 次时
 * 是完全不同的两句话，前者基本没有信息量。中介会拿这个数判断自己
 * 做得好不好，管理员会拿它判断要不要限制接单——两边都需要知道
 * 这个百分比是从多少个样本里算出来的。
 *
 * 样本太少时不显示百分比而显示「样本不足」，是因为把 1/2 渲染成
 * 「50%」比不显示更糟：它看起来是个结论。
 */

const MIN_SAMPLE = 5

export default function RatioCell({ value, label = '', warnAbove = null }) {
  if (!value || !value.total) {
    return <span className="ratio none">暂无数据</span>
  }
  const { rate, hit, total } = value
  if (total < MIN_SAMPLE) {
    return (
      <span className="ratio thin" title={`${hit} / ${total}，样本太少不足以算比率`}>
        {hit} / {total}
        <span className="ratio-note">样本不足</span>
      </span>
    )
  }
  const bad = warnAbove !== null && rate >= warnAbove
  return (
    <span className={`ratio ${bad ? 'bad' : ''}`} title={label}>
      <b>{rate}%</b>
      <span className="ratio-note">{hit} / {total}</span>
    </span>
  )
}
