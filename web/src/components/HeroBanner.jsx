import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 首页顶部的大图轮播。
 *
 * **为什么首页要有这一块。** 之前打开首页第一眼是一个白底的搜索框，
 * 像个内部工具。国内成熟的租房平台（贝壳、安居客、自如）首屏无一例外是
 * 一张大图加一句话——它承担两件事：**告诉用户这是个什么站**，
 * 以及**在用户还没输入任何东西之前先给出可点的入口**。
 *
 * **自动轮播是个容易做错的东西**，这里按无障碍要求做全了：
 *
 * - **一个可取消的定时器**，不是每张图各起一个。多定时器的经典症状是
 *   停下来之后过几秒又自己动了。
 * - **四种情况暂停**：鼠标悬停、键盘焦点进入、滚出视口、切到别的标签页。
 *   最后两种用 IntersectionObserver 与 visibilitychange，
 *   看不见的时候不该继续跑动画耗电。
 * - **不放暂停按钮**（按验收意见去掉）。停得下来这条保证靠上面四种
 *   自动暂停实现——真正的问题是「想看清楚时它还在不在动」，
 *   不是「有没有那个按钮」。键盘左右方向键仍可手动翻。
 * - **prefers-reduced-motion 直接停在当前这张**，不是放慢，是不动。
 * - 卸载时清掉定时器、断开 observer、摘掉全部监听器。
 *
 * 切换用的是透明度加轻微放大（Ken Burns），不是横向滑动——
 * 横滑在宽屏上位移距离大，容易显得廉价；淡入更稳，也更符合
 * 「简约」这个要求。
 */

const SLIDES = [
  {
    image: '/static/houses/h03.jpg',
    eyebrow: '大连全城真实房源',
    title: '说一句话，找到想住的房子',
    desc: '不用先想清楚该点哪个筛选项——直接说需求，条件我们替你翻译出来，而且看得见、改得动。',
    cta: '开始找房',
    target: 'search',
  },
  {
    image: '/static/houses/h07.jpg',
    eyebrow: '签约 · 缴费 · 退租',
    title: '一份合同，从签到退全程有据可查',
    desc: '合同生效即排出整期账单，每一笔缴费都有流水与凭证，退租押金怎么算、扣了多少，页面上写得清清楚楚。',
    cta: '看看怎么签约',
    target: 'list',
  },
  {
    image: '/static/houses/h11.jpg',
    eyebrow: '房东 · 中介 · 管理员',
    title: '房子委托出去，房东仍然说了算',
    desc: '委托是多一个人帮你管，不是换人管。咨询与预约自动转给经纪人，房东随时可以自己接手、随时解除。',
    cta: '了解委托',
    target: 'list',
  },
]

/** 定时器周期。5 秒是轮播的常见取值：够读完一句标题，又不至于让人等。 */
const INTERVAL = 5000

export default function HeroBanner({ stats, children }) {
  const [index, setIndex] = useState(0)
  const [reduced, setReduced] = useState(false)
  const rootRef = useRef(null)
  const timerRef = useRef(null)

  const go = useCallback((next) => {
    setIndex((cur) => (next + SLIDES.length) % SLIDES.length)
  }, [])

  // 定时器只有这一处创建与清除。停止条件写在一个地方，
  // 免得「暂停了又自己动起来」这种只能靠运气复现的 bug。
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const root = rootRef.current

    let onscreen = true
    let hovering = false
    let focused = false

    const stop = () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
    }

    const sync = () => {
      stop()
      const blocked =
        media.matches || document.hidden ||
        !onscreen || hovering || focused
      if (!blocked) {
        timerRef.current = setInterval(() => {
          setIndex((cur) => (cur + 1) % SLIDES.length)
        }, INTERVAL)
      }
      setReduced(media.matches)
    }

    const onEnter = () => { hovering = true; sync() }
    const onLeave = () => { hovering = false; sync() }
    const onFocusIn = () => { focused = true; sync() }
    const onFocusOut = () => { focused = false; sync() }
    const onVisibility = () => sync()

    // 滚出视口就别跑了——看不见的动画只是在耗电
    let observer = null
    if (root && 'IntersectionObserver' in window) {
      observer = new IntersectionObserver(([entry]) => {
        onscreen = entry.isIntersecting
        sync()
      })
      observer.observe(root)
    }

    if (root) {
      root.addEventListener('pointerenter', onEnter)
      root.addEventListener('pointerleave', onLeave)
      root.addEventListener('focusin', onFocusIn)
      root.addEventListener('focusout', onFocusOut)
    }
    document.addEventListener('visibilitychange', onVisibility)
    // Safari 14 以下只有 addListener，这里两种都试一下
    if (media.addEventListener) media.addEventListener('change', sync)
    else if (media.addListener) media.addListener(sync)

    sync()

    return () => {
      stop()
      if (observer) observer.disconnect()
      if (root) {
        root.removeEventListener('pointerenter', onEnter)
        root.removeEventListener('pointerleave', onLeave)
        root.removeEventListener('focusin', onFocusIn)
        root.removeEventListener('focusout', onFocusOut)
      }
      document.removeEventListener('visibilitychange', onVisibility)
      if (media.removeEventListener) media.removeEventListener('change', sync)
      else if (media.removeListener) media.removeListener(sync)
    }
  }, [])

  const onKeyDown = (event) => {
    if (event.key === 'ArrowLeft') { go(index - 1); event.preventDefault() }
    if (event.key === 'ArrowRight') { go(index + 1); event.preventDefault() }
  }

  const slide = SLIDES[index]

  return (
    <section
      className="hero"
      ref={rootRef}
      role="region"
      aria-roledescription="轮播"
      aria-label="平台介绍"
      onKeyDown={onKeyDown}
    >
      <div className="hero-stage">
        {SLIDES.map((item, i) => (
          <div
            key={item.image}
            className={`hero-slide ${i === index ? 'on' : ''}`}
            aria-hidden={i !== index}
          >
            <img src={item.image} alt="" loading={i === 0 ? 'eager' : 'lazy'} />
          </div>
        ))}
        <div className="hero-veil" />
      </div>

      <div className="hero-inner">
        {/* key 换了就重放一次入场动画，让文字跟着图一起换 */}
        <div className="hero-copy" key={index}>
          <span className="hero-eyebrow">{slide.eyebrow}</span>
          <h1 className="hero-title">{slide.title}</h1>
          <p className="hero-desc">{slide.desc}</p>
        </div>

        {/* 搜索直接摆在大图上。
            原先这里是「开始找房 / 浏览全部房源」两个按钮，点了只是
            滚到下面的搜索框——**中间隔了一步没有意义的跳转**。
            国内房产平台的首屏一律是「大图 + 搜索框」，用户落地即可输入。
            搜索框不随轮播切换，所以放在 hero-copy 外面，
            换图时只有文案重放入场动画，输入框不闪。 */}
        {children && <div className="hero-search">{children}</div>}

        {stats && (
          <div className="hero-stats">
            {stats.map((s) => (
              <div className="hero-stat" key={s.label}>
                <b>{s.value}</b>
                <span>{s.label}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 左右箭头与暂停键按验收意见去掉了：三张图的轮播摆四个控件，
          右下角看着像播放器的操作面板，而这一屏本身是一句标语加一个搜索框。

          **自动播放「停得下来」这条保证没有丢**，只是换了实现：
          鼠标悬停、键盘焦点进入、滚出视口、切走标签页四种情况都停，
          `prefers-reduced-motion` 下压根不自动播。真正的问题从来不是
          「有没有那个按钮」，是「想看清楚的时候它还在不在动」。 */}
      <div className="hero-ctrl">
        <div className="hero-dots" role="tablist" aria-label="选择要看的介绍">
          {SLIDES.map((item, i) => (
            <button
              key={item.image}
              className={`hero-dot ${i === index ? 'on' : ''}`}
              role="tab"
              aria-selected={i === index}
              aria-label={`第 ${i + 1} 张，共 ${SLIDES.length} 张`}
              onClick={() => go(i)}
            />
          ))}
        </div>
      </div>

      {/* 给读屏软件用：切换时播报，但不抢焦点 */}
      <div className="sr-only" aria-live="polite">
        第 {index + 1} 张，共 {SLIDES.length} 张：{slide.title}
      </div>
    </section>
  )
}
