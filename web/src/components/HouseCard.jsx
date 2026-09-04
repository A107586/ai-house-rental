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
      <button
        className={`fav ${favorited ? 'on' : ''}`}
        title={favorited ? '取消收藏' : '收藏'}
        onClick={(e) => {
          // 卡片整体可点，收藏按钮不能把点击冒泡上去打开抽屉
          e.stopPropagation()
          onToggleFavorite(house.house_no)
        }}
      >
        {favorited ? '★' : '☆'}
      </button>

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
      <div className="card-loc">
        {house.district} · {house.community} · {house.floor}
      </div>

      <div className="card-price">
        {Math.round(house.rent_price)}
        <small> 元/月</small>
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
