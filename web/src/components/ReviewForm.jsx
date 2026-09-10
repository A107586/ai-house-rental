import { useState } from 'react'
import { api } from '../api'

const MAX_SHOTS = 6
const MAX_MB = 5

function Stars({ value, onPick, label }) {
  return (
    <div className="star-row">
      <span className="star-label">{label}</span>
      <div className="star-pick">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            type="button"
            className={`star ${n <= value ? 'on' : ''}`}
            onClick={() => onPick(n)}
            aria-label={`${n} 星`}
          >
            ★
          </button>
        ))}
        <span className="star-n">{value ? `${value} 分` : '未评'}</span>
      </div>
    </div>
  )
}

export default function ReviewForm({ contract, onDone }) {
  const [rating, setRating] = useState(0)
  const [house, setHouse] = useState(0)
  const [landlord, setLandlord] = useState(0)
  // 中介那一栏只在这份合同确实有中介时才出现。没有中介却摆一排星星，
  // 用户会以为自己漏填了什么，评完的分也落不到任何人头上
  const [agent, setAgent] = useState(0)
  const [content, setContent] = useState('')
  const [toLandlord, setToLandlord] = useState('')
  const [toAgent, setToAgent] = useState('')
  const hasAgent = Boolean(contract.agent_id)
  const [shots, setShots] = useState([])
  const [uploading, setUploading] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  /** C1：实拍图。**先传图拿到 URL，提交时只带地址**——
   *  把图片塞进提交请求里，一次超时就连评分带图一起丢了。 */
  async function pickShots(event) {
    const files = Array.from(event.target.files || [])
    event.target.value = ''      // 不清空的话连选同一张不会再触发 change
    if (!files.length) return
    setError('')

    const room = MAX_SHOTS - shots.length
    if (room <= 0) {
      setError(`最多 ${MAX_SHOTS} 张`)
      return
    }
    const queue = files.slice(0, room)
    if (files.length > room) setError(`最多 ${MAX_SHOTS} 张，多出的已忽略`)

    for (let i = 0; i < queue.length; i += 1) {
      const file = queue[i]
      // 大小在本地先看一眼。服务端一定要拦，但让人等一个大文件传完
      // 再被拒绝是最差的那种体验
      if (file.size > MAX_MB * 1024 * 1024) {
        setError(`${file.name} 超过 ${MAX_MB}MB，已跳过`)
        continue
      }
      setUploading(`正在上传第 ${i + 1} / ${queue.length} 张…`)
      try {
        const { url } = await api.uploadImage(file)
        setShots((list) => (list.includes(url) ? list : [...list, url]))
      } catch (err) {
        setError(err.message)
        break
      }
    }
    setUploading('')
  }

  const dropShot = (url) => setShots((list) => list.filter((u) => u !== url))

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    if (!rating) {
      setError('请先给一个总体评分')
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.createReview({
        contract_no: contract.contract_no,
        rating,
        rating_house: house,
        rating_landlord: landlord,
        rating_agent: hasAgent ? agent : 0,
        content,
        content_landlord: toLandlord,
        content_agent: hasAgent ? toAgent : '',
        images: shots,
      })
      onDone()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li>
      <div className="record-head">
        <strong>{contract.house_title || contract.house_no}</strong>
        <span className="pill">{contract.start_date} 至 {contract.end_date}</span>
      </div>
      <div className="record-body">
        月租 {contract.rent} 元 · {contract.end_reason || '租约已结束'}
      </div>

      <form onSubmit={submit} style={{ marginTop: 10 }}>
        <Stars label="总体" value={rating} onPick={setRating} />
        <Stars label="房源" value={house} onPick={setHouse} />
        <Stars label="房东" value={landlord} onPick={setLandlord} />
        {hasAgent && (
          <Stars label={`中介${contract.agent_name ? `（${contract.agent_name}）` : ''}`}
                 value={agent} onPick={setAgent} />
        )}

        {/* 三段话分开写，不合成一段。
            合成一段的话，房东概要页要展示「租客怎么评价这个房东」时，
            只能把评中介、评房子的那半句一起贴出去。 */}
        <label className="fld" style={{ marginTop: 8 }}>
          <span>说说这套房（可选）</span>
          <textarea
            rows={3}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="房源与描述是否相符？隔音、采光、周边怎么样？"
          />
        </label>

        <label className="fld">
          <span>说说房东（可选）</span>
          <textarea
            rows={2}
            value={toLandlord}
            onChange={(e) => setToLandlord(e.target.value)}
            placeholder="响应是否及时？报修处理得怎么样？押金退得顺不顺？"
          />
        </label>

        {hasAgent && (
          <label className="fld">
            <span>说说中介（可选）</span>
            <textarea
              rows={2}
              value={toAgent}
              onChange={(e) => setToAgent(e.target.value)}
              placeholder="带看是否准时？信息说得准不准？"
            />
          </label>
        )}

        <div className="shot-pick">
          <div className="shot-pick-head">
            <span>实拍图（可选，最多 {MAX_SHOTS} 张）</span>
            {/* 说清楚图会公开。评价区的图是给下一个租客看的，
                不是发给房东的私信——这件事得在传之前讲 */}
            <em>会随评价一起公开展示，请避开门牌号与个人证件</em>
          </div>
          <div className="shot-grid">
            {shots.map((url) => (
              <div className="shot-cell" key={url}>
                <img src={url} alt="" loading="lazy" />
                <button type="button" className="shot-x"
                        onClick={() => dropShot(url)} aria-label="移除这张">×</button>
              </div>
            ))}
            {shots.length < MAX_SHOTS && (
              <label className={`shot-add ${uploading ? 'busy' : ''}`}>
                <input type="file" accept="image/*" multiple
                       disabled={!!uploading} onChange={pickShots} />
                <span>{uploading ? '上传中…' : '＋ 添加'}</span>
              </label>
            )}
          </div>
        </div>

        {error && <div className="reply failed">{error}</div>}

        <button className="btn small" type="submit" disabled={busy || !!uploading}>
          {busy ? '提交中…' : '提交评价'}
        </button>
      </form>
    </li>
  )
}
