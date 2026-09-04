import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * 举报房源（K-08）。
 *
 * 对标贝壳详情页那个常驻的「我要举报」。我们原先只有机器审核
 * （`server/app/moderation.py`），写得不错，但它只在发布、发评价时
 * 自动跑一遍——**虚假房源、盗图、价格异常这些词表拦不住，
 * 只有真正去看过房的人知道**。全站却没有一个地方能把它说出来。
 *
 * 三个交互上的决定：
 *
 * - **类型从接口取**，不写死在前端。后端加一类举报，这里自动就有，
 *   不用改两处（改一处漏一处是这类清单最常见的下场）。
 * - **未登录先要求登录，而不是提交后再报错**。让人填完一整段
 *   再告诉他要登录，那段话就白写了。
 * - **提交成功后不立刻关窗**，先告诉他单号和「结果会站内通知」。
 *   举报之后石沉大海的话，下次就没人愿意举报了。
 */
export default function ReportDialog({ house, user, onClose, onNeedLogin }) {
  const [kinds, setKinds] = useState([])
  const [kind, setKind] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .reportKinds()
      .then((data) => {
        if (!alive) return
        const list = data.list || []
        setKinds(list)
        if (list.length) setKind(list[0].key)
      })
      .catch(() => setError('举报类型加载失败，请稍后再试。'))
    return () => { alive = false }
  }, [])

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    if (!user) {
      onNeedLogin()
      return
    }
    if (!kind) {
      setError('请选择举报类型。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const data = await api.createReport({
        house_no: house.house_no,
        kind,
        description: description.trim(),
      })
      setDone(data.report)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={() => !busy && onClose()} />
      <div className="dialog" role="dialog" aria-modal="true"
           aria-labelledby="report-title">
        <button className="drawer-close" onClick={onClose} aria-label="关闭"
                disabled={busy}>×</button>
        <h3 id="report-title">举报房源</h3>

        {done ? (
          <>
            <div className="reply created">
              举报已提交，单号 <b>{done.report_no}</b>。
            </div>
            <p className="desc">
              管理员核实后会把结论通过站内消息告诉你，
              在「个人中心 → 消息」里查看。
            </p>
            <button className="btn block" onClick={onClose}>知道了</button>
          </>
        ) : (
          <>
            <div className="loc">
              {house.house_no} · {house.title}
            </div>

            {error && <div className="reply failed">{error}</div>}

            <form onSubmit={submit} className="house-form">
              <label className="fld">
                <span>举报类型</span>
                <select value={kind} onChange={(e) => setKind(e.target.value)}>
                  {kinds.map((item) => (
                    <option key={item.key} value={item.key}>{item.label}</option>
                  ))}
                </select>
              </label>

              <label className="fld">
                <span>具体说明</span>
                <textarea
                  rows={4}
                  maxLength={500}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="例如：打电话过去，房东说这套两周前就租出去了"
                />
                <span className="fld-hint">
                  写清楚时间、经过和联系情况，管理员才核实得动。
                </span>
              </label>

              {!user && (
                <div className="assist warn">
                  <div className="assist-body">
                    举报需要登录后提交，这样处理结果才通知得到你。
                  </div>
                </div>
              )}

              <div className="fld-row">
                <button type="button" className="btn ghost block"
                        onClick={onClose} disabled={busy}>
                  取消
                </button>
                <button type="submit" className="btn block" disabled={busy}>
                  {busy ? '提交中…' : user ? '提交举报' : '登录后举报'}
                </button>
              </div>
            </form>
          </>
        )}
      </div>
    </>
  )
}
