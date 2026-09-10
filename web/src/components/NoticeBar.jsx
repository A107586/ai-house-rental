import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 平台公告的前台展示（F-10）。
 *
 * 公告的内容是押金怎么退、水电怎么算、看房要带什么这类**租房前就该知道
 * 的事**。放在个人中心里等用户想起来去翻，等于没发——所以放在首页，
 * 单行轮转，一条条轮着上；点整条展开当前这条的正文。
 *
 * **没有关闭按钮。** 原先有一个「×」，关掉就把最新一条的 id 记进
 * localStorage 不再显示。但公告已经改成单行轮转、只占一行高度，
 * 它不再挡住任何东西——这时候给一个关闭键，等于鼓励用户
 * 把「押金怎么退」「看房带什么」这些**租房前就该知道的事**永久关掉。
 * 不挡路的东西不需要关闭键。
 */

const CATEGORY_CLASS = {
  公告: '',
  提醒: 'warn',
  政策: 'brand',
}


export default function NoticeBar() {
  const [list, setList] = useState([])
  const [open, setOpen] = useState(false)
  // 轮转到第几条。公告不止一条，原先只露最新那条，其余的要点开
  // 「全部 N 条」才看得到——等于没发。改成一条条轮着上，
  // 每条都有几秒露脸的机会。
  const [idx, setIdx] = useState(0)
  const [paused, setPaused] = useState(false)

  useEffect(() => {
    let alive = true
    api
      .listNotices()
      .then((d) => { if (alive) setList(d.list || []) })
      // 公告取不到不该影响首页，安静地不显示
      .catch(() => {})
    return () => { alive = false }
  }, [])

  // 轮转：4 秒一条。比大图慢一点是有意的——公告是一行字，
  // 读完只要两三秒，停太久就成了「一直不动的那条横幅」。
  //
  // 暂停条件和大图那边一致：鼠标悬停、键盘焦点进来、页面切到后台、
  // 以及用户开了「减少动效」。只有一条公告时压根不启动定时器。
  useEffect(() => {
    if (list.length < 2 || paused || open) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const timer = setInterval(() => {
      setIdx((i) => (i + 1) % list.length)
    }, 4000)
    const onVisible = () => { if (document.hidden) clearInterval(timer) }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [list.length, paused, open])

  if (!list.length) return null

  // 当前轮到的那条
  const current = list[idx % list.length]


  return (
    <section
      className={`noticebar ${open ? 'open' : ''}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      {/* 整条可点，点开当前这条的正文。
          原先要去点右边那个「全部 N 条」按钮才展开——一行公告，
          可点的却只有末尾一个小按钮，多数人根本不会去点。 */}
      <button
        className="notice-head"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className={`notice-tag ${CATEGORY_CLASS[current.category] || ''}`}>
          {current.category || '公告'}
        </span>

        {/* 轮转窗口：高度固定成一行，内容整体上移，露出当前这条。
            用 transform 而不是切换 display，位移才是连续的。 */}
        <span className="notice-track" aria-live="off">
          {/* **按行高位移，不能用百分比。**
              translateY 的百分比是相对元素自身高度的——这条卷轴装着
              全部 N 条公告，高度是 N 行，`-100%` 一下就滑过了整卷，
              第 2 条起全部跑到视野外，只剩一片空白。
              改成 `-idx × 一行高`，一次正好走一条。 */}
          <span
            className="notice-reel"
            style={{ transform: `translateY(calc(${-idx} * var(--notice-line-h)))` }}
          >
            {list.map((n) => (
              <span className="notice-line" key={n.id}>
                <b>{n.title}</b>
                {n.summary && <i>{n.summary}</i>}
              </span>
            ))}
          </span>
        </span>

        <span className="notice-date">{current.published_at}</span>
        <span className="notice-count">
          {idx + 1}/{list.length}
        </span>
        <span className="notice-toggle">{open ? '收起' : '详情'}</span>
      </button>

      {/* 展开时只显示当前这条的正文。原先展开是把 N 条全列出来，
          用户点的是「这一条」，给他一整页反而要再找一次。 */}
      {open && (
        <div className="notice-detail">
          <p>{current.content || current.summary}</p>
          {list.length > 1 && (
            <div className="notice-dots">
              {list.map((n, i) => (
                <button
                  key={n.id}
                  className={`notice-dot ${i === idx ? 'on' : ''}`}
                  aria-label={`第 ${i + 1} 条：${n.title}`}
                  onClick={(e) => { e.stopPropagation(); setIdx(i) }}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
