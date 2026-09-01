import { useState } from 'react'
import { api } from '../api'

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
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

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
        content,
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

        <label className="fld" style={{ marginTop: 8 }}>
          <span>说点什么（可选）</span>
          <textarea
            rows={3}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="房源与描述是否相符？房东响应是否及时？"
          />
        </label>

        {error && <div className="reply failed">{error}</div>}

        <button className="btn small" type="submit" disabled={busy}>
          {busy ? '提交中…' : '提交评价'}
        </button>
      </form>
    </li>
  )
}
