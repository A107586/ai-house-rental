import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 房源核验队列（管理员）。
 *
 * 原先核验只有接口：房东在助手里点了「申请核验」，房子就进了待核验，
 * 可管理端没有任何地方能看、能裁定，凭证照片也根本没收（2026-10-24）。
 * 这里摆出房东交上来的产权凭证，看完点通过或驳回；驳回必须写原因，会原样发给房东。
 *
 * 凭证照片只在这一页出现——上面有产权人姓名和证号，不进房源相册。
 */
export default function VerifyQueue({ onChanged }) {
  const [rows, setRows] = useState([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load() {
    try {
      const data = await api.verifyQueue()
      setRows(data.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => { load() }, [])

  async function decide(house, approved) {
    if (busy) return
    let reason = ''
    if (!approved) {
      reason = window.prompt(`驳回 ${house.house_no} 的核验？写一句原因，会发给房东：`,
                             '凭证照片看不清，请重新拍一张')
      if (reason === null) return
      if (!reason.trim()) { setError('驳回要写原因。'); return }
    }
    setBusy(house.house_no)
    setError('')
    setOk('')
    try {
      const r = await api.decideVerify(house.house_no, approved, reason.trim())
      setOk(approved ? `${house.house_no} 已核验，编号 ${r.house.verify_no}。`
                     : `已驳回 ${house.house_no}，房东会收到原因。`)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  return (
    <div>
      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}
      {rows.length === 0 ? (
        <p className="desc">没有待核验的房源。</p>
      ) : (
        <ul className="record-list">
          {rows.map((h) => (
            <li key={h.house_no}>
              <div className="record-head">
                <strong>{h.title}</strong>
                <span className="pill status-pending">待核验</span>
              </div>
              <div className="record-body">
                {h.house_no} · {h.district} {h.community} {h.address}
                {h.area ? ` · ${Math.round(h.area)}㎡` : ''} · 房东 {h.landlord_name || '—'}
              </div>
              {!(h.verify_images || []).length && (
                <div className="reply failed">这份申请没有附产权凭证，无从核对——驳回，让房东上传后重新提交。</div>
              )}
              <div className="verify-proofs">
                {(h.verify_images || []).map((url) => (
                  <a key={url} href={url} target="_blank" rel="noreferrer">
                    <img src={url} alt="产权凭证" loading="lazy" />
                  </a>
                ))}
              </div>
              <div className="record-actions">
                <button className="btn small" disabled={busy === h.house_no}
                        onClick={() => decide(h, true)}>核验通过</button>
                <button className="btn ghost small" disabled={busy === h.house_no}
                        onClick={() => decide(h, false)}>驳回</button>
                <span className="own">核对：凭证上的地址、面积与房源一致，产权人与房东实名一致或有租赁授权</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
