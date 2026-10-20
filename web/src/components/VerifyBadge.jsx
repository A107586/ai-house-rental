/**
 * 房源核验条与费用明细（K-07）。
 *
 * 对标贝壳详情页顶部那一行：房源验真编号、房源维护时间、发布人，
 * 以及单独一个「费用详情」标签页。
 *
 * 这两块看着是两件事，实际解决的是同一个问题——**租客凭什么相信
 * 这条信息**。一套房源上，他要判断两件事：
 *
 *   1. 这房子是不是真的（核验条）；
 *   2. 住进去要掏多少钱（费用明细）。
 *
 * 原先详情页只有「租金 / 押金」两个数，第二件要租客自己拿租金、押金、
 * 付款周期、服务费去算——算错了就是白跑一趟；第一件干脆无从判断。
 *
 * 核验做不到真的产权核验（《待完善清单》D-4：不动产登记接口拿不到），
 * 但**状态机是真的**：未核验 → 待核验 → 已核验 / 核验失败，
 * 由管理员人工判定。所以这里如实显示四种状态，
 * **不把「未核验」画成绿色的样子**——那才是真的欺骗。
 */

const VERIFY = {
  verified: {
    cls: 'ok',
    icon: '✓',
    label: '房源已核验',
    note: '平台已核对房东提交的产权凭证',
  },
  pending: {
    cls: 'wait',
    icon: '⋯',
    label: '核验中',
    note: '房东已提交凭证，平台正在核对',
  },
  rejected: {
    cls: 'bad',
    icon: '!',
    label: '核验未通过',
    note: '请谨慎联系，建议实地看房后再签约',
  },
  unverified: {
    cls: 'none',
    icon: '?',
    label: '未核验',
    note: '房东尚未提交产权凭证，建议实地看房后再签约',
  },
}

function money(value) {
  return Math.round(Number(value) || 0).toLocaleString('zh-CN')
}

export function VerifyBar({ house }) {
  const state = VERIFY[house.verify_status] || VERIFY.unverified

  return (
    <div className={`verify-bar ${state.cls}`}>
      <span className="verify-mark" aria-hidden="true">{state.icon}</span>
      <div className="verify-body">
        <div className="verify-head">
          <b>{state.label}</b>
          {/* 核验编号要露出来。它的用处就是被租客拿去和客服核对，
              藏起来的话这套核验对外等于不存在 */}
          {house.verify_no && (
            <span className="verify-no">编号 {house.verify_no}</span>
          )}
        </div>
        <div className="verify-note">{state.note}</div>
        <div className="verify-meta">
          {house.maintained_at && <span>最近维护 {house.maintained_at}</span>}
          {house.verified_at && <span>核验于 {house.verified_at}</span>}
          {house.landlord_name && <span>发布人 {house.landlord_name}</span>}
        </div>
      </div>
    </div>
  )
}

export function FeeTable({ house }) {
  const fee = house.fee_detail
  // 老数据或接口降级时可能没有这个字段，缺了就不渲染，
  // 不要摆一张全是 0 的表——那比不显示更误导
  if (!fee) return null

  const months = fee.pay_months || 1

  return (
    <div className="fee-table">
      <div className="section-title">
        费用明细
        {house.pay_plan && <span className="pill">{house.pay_plan}</span>}
      </div>

      <dl className="fee-lines">
        <div>
          <dt>月租金</dt>
          <dd>{money(fee.rent)} 元 / 月</dd>
        </div>
        <div>
          <dt>首期租金</dt>
          <dd>
            {money(fee.rent_first_term)} 元
            <span className="fee-hint">
              {months > 1 ? `${months} 个月一付` : '一月一付'}
            </span>
          </dd>
        </div>
        <div>
          <dt>押金</dt>
          <dd>
            {fee.deposit > 0 ? `${money(fee.deposit)} 元` : '免押'}
          </dd>
        </div>
        <div>
          <dt>服务费</dt>
          <dd>{fee.service_fee > 0 ? `${money(fee.service_fee)} 元` : '无'}</dd>
        </div>
        <div className="fee-total">
          <dt>入住前共需支付</dt>
          <dd>{money(fee.first_payment)} 元</dd>
        </div>
      </dl>

      {/* 这句话必须写。用户看到一个合计数会当成最终报价，
          而水电燃气是按实际用量走的，不写清楚就是留了个坑 */}
      <div className="fee-note">
        以上为签约时需支付的金额，和合同、账单一致。
        水、电、燃气、宽带按实际用量另计。
      </div>
    </div>
  )
}
