import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 平台公告的前台展示（F-10）。
 *
 * 公告的内容是押金怎么退、水电怎么算、看房要带什么这类**租房前就该知道
 * 的事**。放在个人中心里等用户想起来去翻，等于没发——所以放在首页，
 * 默认只露最新一条的标题，点开才展开全部。
 *
 * 关掉之后记在 localStorage 里，不再自动弹出。每次进首页都被同一条公告
 * 挡一下，用户学会的只是"看到就关掉"。
 */
const DISMISS_KEY = 'rental_notice_dismissed'

const CATEGORY_CLASS = {
  公告: '',
  提醒: 'warn',
  政策: 'brand',
}

function loadDismissed() {
  try {
    return localStorage.getItem(DISMISS_KEY) || ''
  } catch {
    return ''
  }
}

export default function NoticeBar() {
  const [list, setList] = useState([])
  const [open, setOpen] = useState(false)
  const [dismissed, setDismissed] = useState(loadDismissed)

  useEffect(() => {
    let alive = true
    api
      .listNotices()
      .then((d) => { if (alive) setList(d.list || []) })
      // 公告取不到不该影响首页，安静地不显示
      .catch(() => {})
    return () => { alive = false }
  }, [])

  if (!list.length) return null

  const latest = list[0]
  // 用「最新一条的 id」当关闭标记：发了新公告就会重新出现，
  // 而不是关一次以后永远看不到
  const hidden = String(latest.id) === dismissed

  function dismiss() {
    setDismissed(String(latest.id))
    setOpen(false)
    try {
      localStorage.setItem(DISMISS_KEY, String(latest.id))
    } catch {
      // 隐私模式下存不进去，这次会话内不再弹就够了
    }
  }

  if (hidden && !open) {
    return (
      <div className="notice-mini">
        <button className="chip-btn" onClick={() => setOpen(true)}>
          查看平台公告（{list.length}）
        </button>
      </div>
    )
  }

  return (
    <section className={`noticebar ${open ? 'open' : ''}`}>
      <div className="notice-head">
        <span className={`notice-tag ${CATEGORY_CLASS[latest.category] || ''}`}>
          {latest.category || '公告'}
        </span>
        <span className="notice-title">{latest.title}</span>
        <span className="notice-date">{latest.published_at}</span>
        <button className="btn ghost small" onClick={() => setOpen(!open)}>
          {open ? '收起' : `全部 ${list.length} 条`}
        </button>
        <button className="notice-close" onClick={dismiss} title="不再提示">
          ×
        </button>
      </div>

      {!open && latest.summary && (
        <div className="notice-summary">{latest.summary}</div>
      )}

      {open && (
        <ul className="notice-list">
          {list.map((n) => (
            <li key={n.id}>
              <div className="notice-item-head">
                <span className={`notice-tag ${CATEGORY_CLASS[n.category] || ''}`}>
                  {n.category || '公告'}
                </span>
                <b>{n.title}</b>
                <span className="notice-date">{n.published_at}</span>
              </div>
              <p>{n.content}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
