import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * AI-03 智能客服的管理端。
 *
 * 三个页签对应管理员真正要做的三件事：
 *
 * - **知识库**：写答案。客服答不上来九成是这里没写，不是模型不行。
 * - **质量分析**：看该往哪儿写。被反复问却答不上来的问题就是下一批语料。
 * - **转人工**：兜底。拒答之后用户不能没有出路。
 *
 * 「质量分析」放在中间而不是最后，是因为它是连接另外两个的桥——
 * 从它的「知识缺口」列表点进去补 FAQ，从它的待处理数点进去回工单。
 */
const CATEGORIES = ['押金', '费用', '看房', '报修', '退租', '合同', '缴费',
  '账号', '房东', '其他']

const EMPTY_FAQ = {
  code: '', question: '', answer: '', category: '押金',
  aliases: '', is_published: true, sort_order: 0,
}

export default function SupportPanel() {
  const [tab, setTab] = useState('knowledge')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')

  const [faqs, setFaqs] = useState([])
  const [keyword, setKeyword] = useState('')
  const [editing, setEditing] = useState(null)

  const [stats, setStats] = useState(null)
  const [handoffs, setHandoffs] = useState([])
  const [replying, setReplying] = useState(null)
  const [replyText, setReplyText] = useState('')

  async function run(task, message) {
    setBusy(true)
    setError('')
    setOk('')
    try {
      const result = await task()
      if (message) setOk(message)
      return result
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  async function loadFaqs() {
    const data = await run(() => api.adminFaqs(keyword))
    if (data) setFaqs(data.list || [])
  }

  async function loadStats() {
    const data = await run(() => api.supportAnalytics(30))
    if (data) setStats(data)
  }

  async function loadHandoffs() {
    const data = await run(() => api.adminHandoffs())
    if (data) setHandoffs(data.list || [])
  }

  useEffect(() => {
    if (tab === 'knowledge') loadFaqs()
    if (tab === 'quality') loadStats()
    if (tab === 'handoff') loadHandoffs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  async function saveFaq() {
    if (!editing.question.trim() || !editing.answer.trim()) {
      setError('问题与答案都不能为空')
      return
    }
    const payload = { ...editing }
    const saved = editing.id
      ? await run(() => api.updateFaq(editing.id, payload), '已保存')
      : await run(() => api.createFaq(payload), '已新增')
    if (saved) {
      setEditing(null)
      loadFaqs()
    }
  }

  async function removeFaq(row) {
    if (!window.confirm(`删除「${row.question}」？删除后智能体不再检索这一条。`)) return
    const done = await run(() => api.deleteFaq(row.id), '已删除')
    if (done) loadFaqs()
  }

  async function toggle(row) {
    const done = await run(() => api.toggleFaq(row.id, !row.is_published),
      row.is_published ? '已下架' : '已上架')
    if (done) loadFaqs()
  }

  async function sendReply(row) {
    if (!replyText.trim()) {
      setError('回复内容不能为空')
      return
    }
    const done = await run(() => api.replyHandoff(row.handoff_no, replyText),
      '已回复，用户会收到站内消息')
    if (done) {
      setReplying(null)
      setReplyText('')
      loadHandoffs()
    }
  }

  /** 从「知识缺口」一键起草一条 FAQ——看到缺口就地补，不用换页签再找。 */
  function draftFrom(question) {
    setEditing({ ...EMPTY_FAQ, question })
    setTab('knowledge')
  }

  return (
    <div className="support-panel">
      <div className="tabs">
        {[['knowledge', '知识库'], ['quality', '质量分析'], ['handoff', '转人工']]
          .map(([key, label]) => (
            <button
              key={key}
              className={`tab ${tab === key ? 'active' : ''}`}
              onClick={() => { setTab(key); setError(''); setOk('') }}
            >
              {label}
            </button>
          ))}
      </div>

      {error && <p className="error">{error}</p>}
      {ok && <p className="ok">{ok}</p>}
      {busy && <p className="muted">处理中…</p>}

      {tab === 'knowledge' && (
        <div>
          <div className="toolbar">
            <input
              placeholder="搜索问题、答案或别名"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') loadFaqs() }}
            />
            <button onClick={loadFaqs}>搜索</button>
            <button onClick={() => setEditing({ ...EMPTY_FAQ })}>新增条目</button>
            <span className="muted">共 {faqs.length} 条</span>
          </div>

          {editing && (
            <div className="faq-editor">
              <label>
                问题
                <input
                  value={editing.question}
                  onChange={(e) => setEditing({ ...editing, question: e.target.value })}
                  placeholder="押金什么时候退？"
                />
              </label>
              <label>
                答案
                <textarea
                  rows={5}
                  value={editing.answer}
                  onChange={(e) => setEditing({ ...editing, answer: e.target.value })}
                  placeholder="第一句话必须是结论——大模型不可用时只会返回这一句。"
                />
              </label>
              <label>
                同义问法（用 / 分隔）
                <input
                  value={editing.aliases}
                  onChange={(e) => setEditing({ ...editing, aliases: e.target.value })}
                  placeholder="保证金什么时候退 / 押金能退吗"
                />
              </label>
              <p className="hint">
                别名直接影响命中率：用户说「保证金」而库里写的是「押金」，就检索不到。一种叫法写一条。
              </p>
              <div className="row">
                <label>
                  分类
                  <select
                    value={editing.category}
                    onChange={(e) => setEditing({ ...editing, category: e.target.value })}
                  >
                    {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </label>
                <label className="inline">
                  <input
                    type="checkbox"
                    checked={editing.is_published}
                    onChange={(e) => setEditing({ ...editing, is_published: e.target.checked })}
                  />
                  已发布
                </label>
              </div>
              <div className="row">
                <button onClick={saveFaq}>保存</button>
                <button className="ghost" onClick={() => setEditing(null)}>取消</button>
              </div>
            </div>
          )}

          <table className="data-table">
            <thead>
              <tr>
                <th>问题</th><th>分类</th><th>引用次数</th><th>状态</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {faqs.map((row) => (
                <tr key={row.id}>
                  <td>
                    {row.question}
                    {row.aliases && <div className="muted small">别名：{row.aliases}</div>}
                  </td>
                  <td>{row.category}</td>
                  <td>{row.hit_count}</td>
                  <td>{row.is_published ? '已发布' : '已下架'}</td>
                  <td>
                    <button className="link" onClick={() => setEditing(row)}>编辑</button>
                    <button className="link" onClick={() => toggle(row)}>
                      {row.is_published ? '下架' : '上架'}
                    </button>
                    <button className="link danger" onClick={() => removeFaq(row)}>删除</button>
                  </td>
                </tr>
              ))}
              {!faqs.length && (
                <tr><td colSpan={5} className="muted">还没有知识条目</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'quality' && stats && (
        <div>
          <div className="stat-row">
            <div className="stat"><b>{stats.total}</b><span>近 {stats.days} 天提问</span></div>
            <div className="stat">
              <b>{(stats.answer_rate * 100).toFixed(1)}%</b><span>答出率</span>
            </div>
            <div className="stat">
              <b>{(stats.refuse_rate * 100).toFixed(1)}%</b><span>拒答率</span>
            </div>
            <div className="stat"><b>{stats.median_latency_ms}ms</b><span>响应中位数</span></div>
            <div className="stat"><b>{stats.pending_handoffs}</b><span>待处理转人工</span></div>
          </div>

          <p className="hint">
            拒答率高说明这些问题库里没有。下面这张表就是该补的语料清单，点「补一条」直接起草。
          </p>

          <h4>知识缺口（被拒答且反复出现的问题）</h4>
          <table className="data-table">
            <thead><tr><th>问题</th><th>次数</th><th /></tr></thead>
            <tbody>
              {stats.top_gaps.map((row) => (
                <tr key={row.question}>
                  <td>{row.question}</td>
                  <td>{row.count}</td>
                  <td>
                    <button className="link" onClick={() => draftFrom(row.question)}>
                      补一条
                    </button>
                  </td>
                </tr>
              ))}
              {!stats.top_gaps.length && (
                <tr><td colSpan={3} className="muted">暂时没有被拒答的问题</td></tr>
              )}
            </tbody>
          </table>

          <h4>最常被引用的条目</h4>
          <ul className="plain">
            {stats.top_faqs.map((row) => (
              <li key={row.code}>{row.question}<span className="muted"> · {row.hit_count} 次</span></li>
            ))}
          </ul>

          {!!stats.unused_faqs.length && (
            <>
              <h4>从未被引用的条目</h4>
              <p className="hint">
                写了却没人问到，可能是问题太书面。可以补几条口语化的别名。
              </p>
              <ul className="plain">
                {stats.unused_faqs.map((row) => <li key={row.code}>{row.question}</li>)}
              </ul>
            </>
          )}

          {!!stats.disliked.length && (
            <>
              <h4>被点「没用」的回答</h4>
              <ul className="plain">
                {stats.disliked.map((row) => (
                  <li key={row.id}>
                    {row.question}
                    <span className="muted"> · 引用 {row.cited.join('、') || '无'}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {tab === 'handoff' && (
        <table className="data-table">
          <thead>
            <tr><th>工单</th><th>问题</th><th>智能体为什么没答</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {handoffs.map((row) => (
              <tr key={row.handoff_no}>
                <td>{row.handoff_no}<div className="muted small">{row.created_at}</div></td>
                <td>
                  {row.question}
                  {row.reply && <div className="muted small">已回复：{row.reply}</div>}
                  {replying === row.handoff_no && (
                    <div className="reply-box">
                      <textarea
                        rows={3}
                        value={replyText}
                        onChange={(e) => setReplyText(e.target.value)}
                        placeholder="回复会通过站内消息发给用户"
                      />
                      <button onClick={() => sendReply(row)}>发送</button>
                      <button className="ghost" onClick={() => setReplying(null)}>取消</button>
                    </div>
                  )}
                </td>
                <td className="muted small">{row.reason || '用户主动要求'}</td>
                <td>{row.status === 'pending' ? '待处理' : '已回复'}</td>
                <td>
                  {row.status === 'pending' && (
                    <button
                      className="link"
                      onClick={() => { setReplying(row.handoff_no); setReplyText('') }}
                    >
                      回复
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!handoffs.length && (
              <tr><td colSpan={5} className="muted">没有转人工工单</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
