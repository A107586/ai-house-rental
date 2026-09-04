import { useEffect, useState } from 'react'

/**
 * 房源图片，带统一的失败兜底（K-12）。
 *
 * 原先各处都是裸的 `<img src={house.cover_image}>`，只判断了
 * 「有没有地址」，**没有判断「这个地址能不能打开」**。图片被删掉、
 * 路径写错、静态目录没挂上——任何一种情况都会渲染成浏览器默认的
 * 碎图标：一个灰框、一个裂开的小图案，外加 alt 文字。
 *
 * 这个碎图标的问题不只是难看。它是**浏览器的**样式，和整站毫无关系，
 * 用户看到的第一反应是「这个网站坏了」，而不是「这套房没有照片」。
 * 后者是事实，前者不是。
 *
 * 所以统一成一个组件：加载失败就换成和「暂无图片」完全一致的占位，
 * 两种情况在视觉上不再有区别——因为对用户来说它们本来就是同一件事。
 */
export default function HousePhoto({ src, alt = '', className = '',
                                     placeholder = '暂无图片',
                                     placeholderClassName = 'photo-none',
                                     lazy = true }) {
  const [failed, setFailed] = useState(false)

  // 换了一张图要重新给它一次机会。不重置的话，同一个组件实例
  // （比如相册里切换大图）一旦失败过一次，后面每张都显示占位
  useEffect(() => { setFailed(false) }, [src])

  // 占位和图片各用各的样式类：图片上的类（比如相册大图的尺寸）
  // 套在占位文字上通常是错的
  if (!src || failed) {
    return <span className={placeholderClassName}>{placeholder}</span>
  }

  return (
    <img
      className={className}
      src={src}
      alt={alt}
      loading={lazy ? 'lazy' : undefined}
      onError={() => setFailed(true)}
    />
  )
}
