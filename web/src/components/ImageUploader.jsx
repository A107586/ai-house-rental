import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * 图片上传与相册管理。房源与报修共用，靠 `mode` 区分。
 *
 * 两处刻意的处理：
 *
 * 1. **上传前先在本地看一眼大小**。5 MB 的限制服务端一定要拦，但让用户
 *    等一个大文件传完再被拒绝是最差的体验。
 * 2. **逐张上传而不是一次性并发全部**。手机拍的照片一张两三兆，同时发
 *    十张既容易撞上超时，也让进度无从显示。
 */
const MAX_MB = 5

export default function ImageUploader({ mode = 'house', houseNo, repairId,
  onChanged }) {
  const [images, setImages] = useState([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  // 正在编辑说明的那张图：{id, text}。**草稿一定要经过这一步**——
  // 图片说明是挂在房源上给租客看的公开文案，
  // 让模型写完直接存，等于替房东发布了一句他没看过的话
  const [editing, setEditing] = useState(null)
  const [drafting, setDrafting] = useState(0)
  const inputRef = useRef(null)

  const key = mode === 'house' ? houseNo : repairId

  async function load() {
    if (!key) return
    try {
      const data = mode === 'house'
        ? await api.houseImages(houseNo)
        : await api.repairImages(repairId)
      setImages(data.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  async function pick(event) {
    const files = Array.from(event.target.files || [])
    // 清空 input，否则连续选同一个文件不会再触发 change
    event.target.value = ''
    if (!files.length) return

    setError('')
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i]
      if (file.size > MAX_MB * 1024 * 1024) {
        setError(`${file.name} 超过 ${MAX_MB}MB，已跳过`)
        continue
      }
      setBusy(`正在上传第 ${i + 1} / ${files.length} 张…`)
      try {
        const { url } = await api.uploadImage(file)
        if (mode === 'house') await api.addHouseImage(houseNo, url)
        else await api.addRepairImage(repairId, url)
      } catch (err) {
        setError(err.message)
        break
      }
    }
    setBusy('')
    await load()
    onChanged?.()
  }

  async function remove(id) {
    setError('')
    try {
      await api.deleteHouseImage(houseNo, id)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    }
  }

  /** 让助手看图写一句草稿，填进输入框等房东过目。 */
  async function draftCaption(image) {
    setError('')
    setDrafting(image.id)
    try {
      const { caption } = await api.agentImageCaption(image.url)
      if (caption) setEditing({ id: image.id, text: caption })
      else setError('这次没写出来，可以再试一次，或者自己写一句')
    } catch (err) {
      setError(err.message)
    } finally {
      setDrafting(0)
    }
  }

  async function saveCaption(id, text) {
    setError('')
    try {
      await api.setHouseImageCaption(houseNo, id, text)
      setEditing(null)
      await load()
      onChanged?.()
    } catch (err) {
      // 内容审核拦下来时后端回 400，原文照显——房东得知道是哪一句不行
      setError(err.message)
    }
  }

  async function makeCover(id) {
    setError('')
    try {
      await api.setHouseCover(houseNo, id)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="uploader">
      <div className="up-head">
        <span className="up-title">
          {mode === 'house' ? '房源相册' : '报修照片'}
        </span>
        <span className="up-hint">
          {mode === 'house'
            ? '支持 JPG/PNG/GIF/WebP，单张不超过 5MB。第一张自动作为封面。'
            : '一张照片，往往比几句描述更能说清哪里坏了。'}
        </span>
        <label className={`up-pick ${busy ? 'busy' : ''}`}>
          <input ref={inputRef} type="file" accept="image/*" multiple
                 disabled={!!busy} onChange={pick} />
          {busy || '选择图片'}
        </label>
      </div>

      {error && <div className="reply failed">{error}</div>}

      {images.length === 0 ? (
        <div className="empty">还没有图片</div>
      ) : (
        <div className="up-grid">
          {images.map((image) => (
            <div
              className={`up-cell ${image.is_cover ? 'cover' : ''}`}
              key={image.id}
            >
              <div className="up-thumb">
                <img src={image.url} alt={image.caption || ''} loading="lazy" />
                {image.is_cover && <span className="up-badge">封面</span>}
                {mode === 'house' && (
                  <div className="up-acts">
                    {!image.is_cover && (
                      <button onClick={() => makeCover(image.id)}>设封面</button>
                    )}
                    <button onClick={() => remove(image.id)}>删除</button>
                  </div>
                )}
              </div>
              {/* 图片说明。有就显示，没有就给一个入口——
                  它同时也是 <img alt>，读屏软件念的就是这一句。 */}
              {mode === 'house' && (
                editing?.id === image.id ? (
                  <div className="up-caption edit">
                    <input
                      value={editing.text}
                      maxLength={100}
                      autoFocus
                      placeholder="一句话说清这张图拍的是哪儿"
                      onChange={(e) =>
                        setEditing({ id: image.id, text: e.target.value })}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveCaption(image.id, editing.text)
                        if (e.key === 'Escape') setEditing(null)
                      }}
                    />
                    <button onClick={() => saveCaption(image.id, editing.text)}>
                      保存
                    </button>
                    <button onClick={() => setEditing(null)}>取消</button>
                  </div>
                ) : (
                  <div className="up-caption">
                    <span
                      className={image.caption ? '' : 'muted'}
                      onClick={() =>
                        setEditing({ id: image.id, text: image.caption || '' })}
                    >
                      {image.caption || '还没有说明，点这里写'}
                    </span>
                    <button
                      disabled={drafting === image.id}
                      onClick={() => draftCaption(image)}
                    >
                      {drafting === image.id ? '看图中…' : '✨ 让助手写'}
                    </button>
                  </div>
                )
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
