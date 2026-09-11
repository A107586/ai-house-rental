import HousePhoto from './HousePhoto'

/** 「3天前维护」。
 *
 * 贝壳在每张卡片上都印这一条，看着不起眼，作用很大：租房列表里最让人
 * 恼火的事就是**打电话过去房子早租出去了**。有这个时间，用户自己就能
 * 判断这条信息还值不值得信——把风险量化了，而不是让他碰运气。
 *
 * 超过 30 天不再显示具体天数，只说「30天前」：再精确也没有意义，
 * 那个数字本身已经说明问题了。
 */
function maintainedLabel(date) {
  if (!date) return ''
  const then = new Date(date + 'T00:00:00')
  if (Number.isNaN(then.getTime())) return ''
  const days = Math.floor((Date.now() - then.getTime()) / 86400000)
  if (days < 0) return ''
  if (days === 0) return '今天维护'
  if (days === 1) return '昨天维护'
  if (days > 30) return '30天前维护'
  return `${days}天前维护`
}

export default function HouseCard({ house, favorited, onToggleFavorite,
  onClick, compared, onToggleCompare }) {
  return (
    <article
      className="card"
      onClick={() => onClick(house)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onClick(house)
        }
      }}
    >
      <div className="card-photo">
        {/* 加载失败和「本来就没图」显示同一个占位（K-12）。
            原先只判断了地址有没有，图片 404 时会渲染成浏览器的碎图标，
            用户看到的第一反应是「这网站坏了」而不是「这套房没照片」 */}
        <HousePhoto src={house.cover_image} alt={house.title} />
        <span className="kind-tag">{house.rent_type}</span>
        {onToggleCompare && (
          <label className="cmp-pick" onClick={(e) => e.stopPropagation()}>
            <input
              type="checkbox"
              checked={compared}
              onChange={() => onToggleCompare(house)}
            />
            对比
          </label>
        )}
      </div>

      <div className="card-body">
      <div className="card-title">{house.title}</div>
      {/* A5：地址行不再印楼层。
          原来这里是「沙河口区 · 黑石礁 · 2/28层」，而下面的属性行里
          还有一个「低楼层」——**同一张卡片上两个楼层说法**，
          看的人会以为是两套房的数据串了。楼层归属性行，这里只讲位置。 */}
      <div className="card-loc">
        {house.district} · {house.community}
      </div>

      {/* D1：收藏从图片右上角挪到价格右边，星星换成爱心。
          压在图上时它得靠一个半透明白底才看得清，那个白圆片
          正是「一眼看出是 AI 生成的」的那类元素；而且图片右上角
          还压着「整租」标签和「对比」勾选，三样东西挤在同一个角上。

          放在价格这一行有个额外的好处：**价格是用户在卡片上停留最久的
          地方**，想收藏的念头也正是在看到价格那一刻产生的。 */}
      <div className="card-price">
        <span className="card-price-num">
          {Math.round(house.rent_price)}
          <small> 元/月</small>
        </span>
        <button
          className={`fav-heart ${favorited ? 'on' : ''}`}
          title={favorited ? '取消收藏' : '收藏'}
          aria-label={favorited ? '取消收藏' : '收藏'}
          aria-pressed={favorited}
          onClick={(e) => {
            // 卡片整体可点，收藏按钮不能把点击冒泡上去打开抽屉
            e.stopPropagation()
            onToggleFavorite(house.house_no)
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 20.6 4.3 13a4.8 4.8 0 0 1 0-6.8 4.8 4.8 0 0 1 6.8 0l.9.9.9-.9a4.8 4.8 0 0 1 6.8 0 4.8 4.8 0 0 1 0 6.8Z" />
          </svg>
        </button>
      </div>

      <div className="card-meta">
        {house.rating > 0 && (
          <span className="rate">★ {house.rating}</span>
        )}
        <span>{house.layout}</span>
        <span>{Math.round(house.area)} 平米</span>
        <span>{house.rent_type}</span>
        {house.floor_level && <span>{house.floor_level}</span>}
        {house.elevator && <span>电梯</span>}
      </div>

      {/* 核验与维护时间。这两条回答的是同一个问题：
          **这条信息还能不能信** */}
      <div className="card-trust">
        {house.verify_status === 'verified' && (
          <span className="verified-mark">✓ 已核验</span>
        )}
        {house.pay_plan && <span>{house.pay_plan}</span>}
        {maintainedLabel(house.maintained_at) && (
          <span className="maintained">
            {maintainedLabel(house.maintained_at)}
          </span>
        )}
      </div>

      <div className="tags">
        {house.tags.slice(0, 4).map((tag) => (
          <span className="tag" key={tag}>
            {tag}
          </span>
        ))}
      </div>
      </div>
    </article>
  )
}
