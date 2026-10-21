import { useEffect } from 'react'

/**
 * 全屏看图。房源详情的主图点一下打开，← → 翻页，Esc 关闭。
 *
 * 反馈是「房源相册太小」：抽屉里的主图只有抽屉那么宽，缩略图 62×46，
 * 想看清厨房、卫生间的细节只能凑近屏幕。看房子主要靠图，得能放大看。
 */
export default function PhotoViewer({ photos, index, onIndex, onClose, title = '' }) {
  const count = photos.length
  const go = (step) => onIndex((index + step + count) % count)

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'ArrowLeft') go(-1)
    }
    window.addEventListener('keydown', onKey)
    // 看图时页面别跟着滚
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, count])

  return (
    <div className="pv" role="dialog" aria-modal="true" aria-label={`${title} 图片`}
         onClick={onClose}>
      <div className="pv-top" onClick={(e) => e.stopPropagation()}>
        <span>{title}</span>
        <span className="pv-count">{index + 1} / {count}</span>
        <button className="pv-close" onClick={onClose} aria-label="关闭">×</button>
      </div>

      <div className="pv-stage">
        {count > 1 && (
          <button className="pv-nav prev" aria-label="上一张"
                  onClick={(e) => { e.stopPropagation(); go(-1) }}>‹</button>
        )}
        <img src={photos[index]} alt={`${title} 第 ${index + 1} 张`}
             onClick={(e) => e.stopPropagation()} />
        {count > 1 && (
          <button className="pv-nav next" aria-label="下一张"
                  onClick={(e) => { e.stopPropagation(); go(1) }}>›</button>
        )}
      </div>

      {count > 1 && (
        <div className="pv-thumbs" onClick={(e) => e.stopPropagation()}>
          {photos.map((url, i) => (
            <button key={url} className={i === index ? 'on' : ''}
                    onClick={() => onIndex(i)} aria-label={`第 ${i + 1} 张`}>
              <img src={url} alt="" loading="lazy" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
