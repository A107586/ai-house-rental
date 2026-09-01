export default function HouseCard({ house, favorited, onToggleFavorite, onClick }) {
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

      <div className="card-title">{house.title}</div>
      <div className="card-loc">
        {house.district} · {house.community} · {house.floor}
      </div>

      <div className="card-price">
        {Math.round(house.rent_price)}
        <small> 元/月</small>
      </div>

      <div className="card-meta">
        <span>{house.layout}</span>
        <span>{Math.round(house.area)} 平米</span>
        <span>{house.rent_type}</span>
      </div>

      <div className="tags">
        {house.tags.slice(0, 4).map((tag) => (
          <span className="tag" key={tag}>
            {tag}
          </span>
        ))}
      </div>
    </article>
  )
}
