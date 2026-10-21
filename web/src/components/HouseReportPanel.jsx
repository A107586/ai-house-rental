import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 举报处理（K-08 管理端）。
 *
 * 和「内容审核」（`FlagPanel`）是两条线，所以是两个面板：
 * 那边是机器拦下来的内容，管理员只需判断留或删；
 * 这边是**人报上来的问题**，有举报人、要给结论、结论要通知回去。
 *
 * 界面上最重要的一件事：**处理时必须填结论**。
 * 「已处理」三个字对举报人毫无意义，他想知道的是核实的结果——
 * 这条通知就是整个举报机制的闭环，也是他下次还愿不愿意举报的依据。
 */
const STATUS_TABS = [
  { key: 'pending', label: '待处理' },
  { key: 'accepted', label: '属实' },
  { key: 'rejected', label: '不属实' },
  { key: 'closed', label: '已关闭' },
]

const VERDICTS = [
  { key: 'accepted', label: '判定属实' },
  { key: 'rejected', label: '判定不属实' },
  { key: 'closed', label: '关闭' },
]

export default function HouseReportPanel() {
  const [status, setStatus] = useState('pending')
  const [rows, setRows] = useState([])
  const [pending, setPending] = useState(0)
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  async function load(next = status) {
    setError('')
    try {
      const data = await api.adminReports(next)
      setRows(data.list || [])
      setPending(data.pending || 0)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load(status)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  async function handle(report, verdict) {
    const conclusion = (drafts[report.report_no] || '').trim()
    if (!conclusion) {
      setError('请先填写处理结论，举报人会收到这段话。')
      return
    }
    setBusy(report.report_no)
    setError('')
    setOk('')
    try {
      await api.handleReport(report.report_no, verdict, conclusion)
      setOk(`${report.report_no} 已处理，结论已通知举报人。`)
      setDrafts((prev) => {
        const next = { ...prev }
        delete next[report.report_no]
        return next
      })
      await load(status)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="flags">
      <div className="tabs sub">
        {STATUS_TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${status === t.key ? 'active' : ''}`}
            onClick={() => setStatus(t.key)}
          >
            {t.label}
            {t.key === 'pending' && pending > 0 && (
              <span className="tab-n">{pending}</span>
            )}
          </button>
        ))}
      </div>

      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {rows.length === 0 ? (
        <div className="empty">
          {status === 'pending'
            ? '没有待处理的举报'
            : '这个状态下暂时没有记录。'}
        </div>
      ) : (
        <ul className="record-list">
          {rows.map((row) => (
            <li key={row.report_no}>
              <div className="record-head">
                <strong>{row.house_title || row.house_no}</strong>
                <span className="pill">{row.kind_label}</span>
                <span className="house-no">{row.house_no}</span>
              </div>

              <div className="record-body">
                {row.description || '（举报人未填写说明）'}
              </div>

              <div className="record-body muted-note">
                举报人 {row.reporter_name} · {row.created_at} · 单号 {row.report_no}
              </div>

              {row.evidence.length > 0 && (
                <div className="chips">
                  {row.evidence.map((url) => (
                    <a key={url} className="chip" href={url}
                       target="_blank" rel="noreferrer">证据图片</a>
                  ))}
                </div>
              )}

              {row.status === 'pending' ? (
                <>
                  <label className="fld">
                    <span>处理结论</span>
                    <input
                      value={drafts[row.report_no] || ''}
                      maxLength={300}
                      placeholder="核实结果，这句话会原样发给举报人"
                      onChange={(e) => setDrafts((prev) => ({
                        ...prev, [row.report_no]: e.target.value,
                      }))}
                    />
                  </label>

                  <div className="record-actions">
                    {VERDICTS.map((v) => (
                      <button
                        key={v.key}
                        className={`btn ${v.key === 'accepted' ? '' : 'ghost'} small`}
                        disabled={busy === row.report_no}
                        onClick={() => handle(row, v.key)}
                      >
                        {busy === row.report_no ? '处理中…' : v.label}
                      </button>
                    ))}
                  </div>

                  {/* 判定和处置是分开的。这里只下结论，
                      要不要下架房源去「房源审核」里做——绑在一起的话，
                      管理员为了下架一套信息过期的房源，
                      就得把它判成「虚假房源」，房东从此背着一条假房源 */}
                  <div className="fld-hint">
                    判定属实后如需下架房源，请到「房源审核」处理。
                  </div>
                </>
              ) : (
                <div className="record-body">
                  <b>{STATUS_TABS.find((t) => t.key === row.status)?.label}</b>
                  {row.conclusion && ` · ${row.conclusion}`}
                  {row.handled_at && ` · ${row.handled_at}`}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
