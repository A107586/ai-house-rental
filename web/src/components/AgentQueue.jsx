import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 中介资质审核（J-11）。
 *
 * 结构照搬已有的「房源审核」，因为管理员做的是同一件事：看一份材料、
 * 通过或驳回。不必发明新的交互。
 *
 * **驳回必须填理由**，和 K-08 同一条理由：只回「未通过」三个字，
 * 被驳回的人不知道该补什么，只会反复提交同一份材料，最后变成
 * 管理员自己的工作量。
 */
export default function AgentQueue({ onReviewed }) {
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, setBusy] = useState(0)

  async function load() {
    setError('')
    try {
      const data = await api.adminAgentQueue()
      setRows(data.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function review(row, approved) {
    let reason = ''
    if (!approved) {
      reason = window.prompt(
        '驳回理由（会通知本人，请写清楚要补什么）',
        '资格证编号无法核验，请上传证书照片')
      if (reason === null) return
      if (!reason.trim()) {
        setError('驳回必须填理由。')
        return
      }
    }
    setBusy(row.user_id)
    setError('')
    setOk('')
    try {
      await api.reviewLicense(row.user_id, approved, reason)
      setOk(approved
        ? `已通过 ${row.name || row.username} 的资质，他现在可以接单了。`
        : '已驳回并通知本人。')
      await load()
      if (onReviewed) onReviewed()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(0)
    }
  }

  return (
    <div className="roster">
      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {rows.length === 0 ? (
        <div className="empty">没有待审的资质。</div>
      ) : (
        <ul className="record-list">
          {rows.map((row) => (
            <li key={row.user_id}>
              <div className="record-head">
                <strong>{row.name || row.username}</strong>
                <span className="pill status-pending">待审</span>
              </div>
              <div className="record-body">
                账号 {row.username} · 电话 {row.phone || '未填'} ·
                从业 {row.years} 年
                {row.districts.length > 0 && ` · 主营 ${row.districts.join('、')}`}
              </div>
              <div className="record-body">
                资格证编号 <span className="mono">{row.license_no || '未填'}</span>
              </div>
              {row.intro && <div className="record-body">{row.intro}</div>}
              {row.license_images.length > 0 && (
                <div className="record-body">
                  {row.license_images.map((src) => (
                    <a key={src} href={src} target="_blank" rel="noreferrer">
                      查看证件照
                    </a>
                  ))}
                </div>
              )}
              <div className="record-actions">
                <button
                  className="btn small"
                  disabled={busy === row.user_id}
                  onClick={() => review(row, true)}
                >
                  通过
                </button>
                <button
                  className="btn ghost small danger"
                  disabled={busy === row.user_id}
                  onClick={() => review(row, false)}
                >
                  驳回
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="rd-note">
        审核通过后他才能接单。**通过意味着他能看到租客联系方式、代房东回复咨询**——请按这个标准审核材料。
      </p>
    </div>
  )
}
