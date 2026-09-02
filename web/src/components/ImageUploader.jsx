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
            : '一张照片往往比几句描述更说得清哪里坏了。'}
        </span>
        <label className={`up-pick ${busy ? 'busy' : ''}`}>
          <input ref={inputRef} type="file" accept="image/*" multiple
                 disabled={!!busy} onChange={pick} />
          {busy || '选择图片'}
        </label>
      </div>

      {error && <div className="reply failed">{error}</div>}

      {images.length === 0 ? (
        <div className="empty">还没有图片。</div>
      ) : (
        <div className="up-grid">
          {images.map((image) => (
            <div
              className={`up-cell ${image.is_cover ? 'cover' : ''}`}
              key={image.id}
            >
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
          ))}
        </div>
      )}
    </div>
  )
}
