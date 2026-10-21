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
  const [category, setCategory] = useState('')

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

  // 顶部的数字卡三个页签都要看，进来先拉一次
  useEffect(() => {
    api.supportAnalytics(30).then(setStats).catch(() => {})
  }, [])

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
    if (!window.confirm(`删除「${row.question}」？删除后助手不会再用这一条回答。`)) return
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

  const shown = category ? faqs.filter((f) => f.category === category) : faqs
  const usedCategories = CATEGORIES.filter((c) => faqs.some((f) => f.category === c))

  return (
    <div className="support-panel">
      {/* 顶部四个数：库里有多少、最近被问多少、答出了多少、有几个人在等人工 */}
      <div className="sp-stats">
        <div><b>{faqs.length || '—'}</b><span>知识条目</span></div>
        <div><b>{stats ? stats.total : '—'}</b><span>近 30 天提问</span></div>
        <div><b>{stats ? `${(stats.answer_rate * 100).toFixed(0)}%` : '—'}</b><span>答出率</span></div>
        <div className={stats?.pending_handoffs ? 'warn' : ''}>
          <b>{stats ? stats.pending_handoffs : '—'}</b><span>待回复的转人工</span>
        </div>
      </div>

      <div className="tabs">
        {[['knowledge', '知识库'], ['quality', '质量分析'],
          ['handoff', `转人工${stats?.pending_handoffs ? `（${stats.pending_handoffs}）` : ''}`]]
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

      {error && <div className="reply failed">{error}</div>}
      {ok && <div className="reply created">{ok}</div>}

      {tab === 'knowledge' && (
        <div>
          <div className="sp-toolbar">
            <input
              placeholder="搜索问题、答案或别名"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') loadFaqs() }}
            />
            <button className="btn ghost small" onClick={loadFaqs} disabled={busy}>搜索</button>
            <button className="btn small" onClick={() => setEditing({ ...EMPTY_FAQ })}>新增条目</button>
          </div>

          <div className="sp-chips">
            <button className={!category ? 'on' : ''} onClick={() => setCategory('')}>
              全部 <em>{faqs.length}</em>
            </button>
            {usedCategories.map((c) => (
              <button key={c} className={category === c ? 'on' : ''} onClick={() => setCategory(c)}>
                {c} <em>{faqs.filter((f) => f.category === c).length}</em>
              </button>
            ))}
          </div>

          {editing && (
            <div className="sp-editor house-form">
              <div className="section-title">{editing.id ? '编辑条目' : '新增条目'}</div>
              <label className="fld">
                <span>问题</span>
                <input
                  value={editing.question}
                  onChange={(e) => setEditing({ ...editing, question: e.target.value })}
                  placeholder="押金什么时候退？"
                />
              </label>
              <label className="fld">
                <span>答案</span>
                <textarea
                  rows={5}
                  value={editing.answer}
                  onChange={(e) => setEditing({ ...editing, answer: e.target.value })}
                  placeholder="第一句话先写结论，再补充细节"
                />
              </label>
              <label className="fld">
                <span>其他问法（用 / 分隔）</span>
                <input
                  value={editing.aliases}
                  onChange={(e) => setEditing({ ...editing, aliases: e.target.value })}
                  placeholder="保证金什么时候退 / 押金能退吗"
                />
                <span className="fld-hint">
                  用户可能换种说法问，比如把「押金」说成「保证金」，想到几种就写几种。
                </span>
              </label>
              <div className="fld-row">
                <label className="fld">
                  <span>分类</span>
                  <select
                    value={editing.category}
                    onChange={(e) => setEditing({ ...editing, category: e.target.value })}
                  >
                    {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </label>
                <label className="fld sp-check">
                  <input
                    type="checkbox"
                    checked={editing.is_published}
                    onChange={(e) => setEditing({ ...editing, is_published: e.target.checked })}
                  />
                  <span>发布（助手可以用它回答）</span>
                </label>
              </div>
              <div className="record-actions">
                <button className="btn small" onClick={saveFaq} disabled={busy}>保存</button>
                <button className="btn ghost small" onClick={() => setEditing(null)}>取消</button>
              </div>
            </div>
          )}

          {shown.length === 0 ? (
            <p className="desc">{faqs.length ? '这个分类下没有条目' : '还没有知识条目'}</p>
          ) : (
            <ul className="record-list sp-faqs">
              {shown.map((row) => (
                <li key={row.id} className={row.is_published ? '' : 'muted-row'}>
                  <div className="record-head">
                    <strong>{row.question}</strong>
                    <span className="sp-meta">
                      <span className="pill">{row.category}</span>
                      <span className={`pill ${row.is_published ? 'status-confirmed' : 'status-cancelled'}`}>
                        {row.is_published ? '已发布' : '已下架'}
                      </span>
                    </span>
                  </div>
                  <div className="record-body sp-answer">{row.answer.replaceAll('**', '')}</div>
                  {row.aliases && <div className="record-body muted-note">也会这么问：{row.aliases}</div>}
                  <div className="record-actions">
                    <span className="sp-hits">被引用 {row.hit_count} 次</span>
                    <button className="btn ghost small" onClick={() => setEditing(row)}>编辑</button>
                    <button className="btn ghost small" onClick={() => toggle(row)}>
                      {row.is_published ? '下架' : '上架'}
                    </button>
                    <button className="btn ghost small danger-text" onClick={() => removeFaq(row)}>删除</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {tab === 'quality' && stats && (
        <div>
          <div className="sp-stats small">
            <div><b>{(stats.refuse_rate * 100).toFixed(1)}%</b><span>没答上的比例</span></div>
            <div><b>{(stats.median_latency_ms / 1000).toFixed(1)} 秒</b><span>回答用时（中位数）</span></div>
            <div><b>{stats.top_gaps.length}</b><span>待补的问题</span></div>
            <div><b>{stats.disliked.length}</b><span>被点「没用」的回答</span></div>
          </div>

          <div className="section-title">知识缺口</div>
          <p className="desc">用户问过、但知识库里还没有答案的问题，点「补一条」就能补上。</p>
          {stats.top_gaps.length === 0 ? (
            <p className="desc">暂时没有答不上来的问题。</p>
          ) : (
            <ul className="record-list compact">
              {stats.top_gaps.map((row) => (
                <li key={row.question}>
                  <div className="record-head">
                    <strong>{row.question}</strong>
                    <span className="pill">问了 {row.count} 次</span>
                  </div>
                  <div className="record-actions">
                    <button className="btn small" onClick={() => draftFrom(row.question)}>补一条</button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className="section-title">最常用的条目</div>
          <ol className="sp-rank">
            {stats.top_faqs.map((row) => (
              <li key={row.code}><span>{row.question}</span><em>{row.hit_count} 次</em></li>
            ))}
          </ol>

          {!!stats.unused_faqs.length && (
            <>
              <div className="section-title">还没被问到过的条目</div>
              <p className="desc">可以给它们补几种口语化的问法。</p>
              <div className="sp-chips static">
                {stats.unused_faqs.map((row) => <span key={row.code}>{row.question}</span>)}
              </div>
            </>
          )}

          {!!stats.disliked.length && (
            <>
              <div className="section-title">被点「没用」的回答</div>
              <ul className="record-list compact">
                {stats.disliked.map((row) => (
                  <li key={row.id}>
                    <div className="record-head"><strong>{row.question}</strong></div>
                    <div className="record-body muted-note">
                      当时用的条目：{row.cited.join('、') || '没有'}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {tab === 'handoff' && (
        handoffs.length === 0 ? (
          <p className="desc">没有转人工的问题。</p>
        ) : (
          <ul className="record-list">
            {handoffs.map((row) => (
              <li key={row.handoff_no} className={row.status === 'pending' ? 'unread' : ''}>
                <div className="record-head">
                  <strong>{row.question}</strong>
                  <span className={`pill ${row.status === 'pending' ? 'status-pending' : 'status-confirmed'}`}>
                    {row.status === 'pending' ? '待回复' : '已回复'}
                  </span>
                </div>
                <div className="record-body muted-note">
                  {row.handoff_no} · {row.created_at} · {handoffReason(row.reason)}
                </div>
                {row.reply && <div className="record-body done-note">已回复：{row.reply}</div>}
                {replying === row.handoff_no ? (
                  <div className="sp-reply">
                    <textarea
                      rows={3}
                      value={replyText}
                      onChange={(e) => setReplyText(e.target.value)}
                      placeholder="回复会通过站内消息发给用户"
                      autoFocus
                    />
                    <div className="record-actions">
                      <button className="btn small" onClick={() => sendReply(row)} disabled={busy}>发送</button>
                      <button className="btn ghost small" onClick={() => setReplying(null)}>取消</button>
                    </div>
                  </div>
                ) : row.status === 'pending' && (
                  <div className="record-actions">
                    <button className="btn small"
                            onClick={() => { setReplying(row.handoff_no); setReplyText('') }}>
                      回复
                    </button>
                    <button className="btn ghost small" onClick={() => draftFrom(row.question)}>
                      写进知识库
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  )
}

/** 转人工的原因存的是检索闸门的判断（「覆盖率 0.37 未达 0.42……」），给人看要换成人话 */
function handoffReason(reason) {
  if (!reason || reason.includes('用户')) return '用户主动要求转人工'
  return '助手没找到可靠的依据'
}
