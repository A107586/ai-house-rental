import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * AI-05 经营分析报告。
 *
 * 需求 AI-05 有一条硬性验收标准：**报告里的数字必须可追溯到数据库记录**。
 * 所以界面上每段结论都把它依据的原始数字并排列出来，答辩时可以当场
 * 对照数据看板核对。返回的 source 字段也直接显示出来，说明这份报告
 * 是纯规则生成的，还是大模型润色过的。
 */
const SOURCE_LABEL = {
  rules: '规则生成，未调用大模型',
  llm_polished: '规则生成数字，大模型润色措辞（数字已校验未被改动）',
}

export default function ReportPanel() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setBusy(true)
    setError('')
    try {
      setData(await api.agentReport())
    } catch (err) {
      setError(err.message)
      setData(null)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function copy() {
    try {
      await navigator.clipboard.writeText(data.text)
      setError('')
    } catch {
      setError('浏览器拒绝了剪贴板访问，请手动选中下方正文复制。')
    }
  }

  return (
    <div className="report">
      <div className="report-head">
        <div>
          <h4>经营分析报告</h4>
          <div className="loc">
            由经营分析智能体生成 · 数据来自平台统计接口
            {data?.source ? ` · ${SOURCE_LABEL[data.source] || data.source}` : ''}
          </div>
        </div>
        <div className="report-acts">
          <button className="btn ghost small" onClick={load} disabled={busy}>
            {busy ? '生成中…' : '重新生成'}
          </button>
          {data?.text && (
            <button className="btn ghost small" onClick={copy}>
              复制全文
            </button>
          )}
        </div>
      </div>

      {error && <div className="reply failed">{error}</div>}

      {busy && !data && <div className="empty">智能体正在分析经营数据…</div>}

      {data && data.status !== 'ok' && (
        <div className="reply failed">{data.message}</div>
      )}

      {data?.status === 'ok' && (
        <>
          {(data.highlights?.length > 0 || data.alerts?.length > 0) && (
            <div className="report-chips">
              {data.highlights.map((h) => (
                <span className="chip" key={h}>{h}</span>
              ))}
              {data.alerts.map((a) => (
                <span className="chip alert" key={a}>⚠ {a}</span>
              ))}
            </div>
          )}

          {data.sections.map((section) => (
            <section className="report-sec" key={section.title}>
              <h5>{section.title}</h5>
              <p>{section.text}</p>
              {section.figures && (
                <div className="report-figs">
                  {Object.entries(section.figures).map(([key, value]) => (
                    <div className="rfig" key={key}>
                      <div className="rk">{key}</div>
                      <div className="rv">{value}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ))}

          <div className="report-foot">
            报告中的每个数字都来自上方「依据数字」，可与数据看板逐项核对。
            大模型不参与任何计算，只允许调整措辞；若润色后数字有出入，
            系统会丢弃润色结果并回退到规则原文。
          </div>
        </>
      )}
    </div>
  )
}
