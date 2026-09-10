import { useEffect, useState } from 'react'

/**
 * 智能体思考指示（H1）。
 *
 * 原先只有一句静止的「思考中…」。找房那一路要跑规则解析、四路召回、
 * RRF 融合、可答性闸门，再交给大模型组织语言，慢的时候十几秒——
 * 这十几秒里界面什么都不说，用户不知道是在算还是卡死了，
 * 于是有人反复点发送，有人直接关掉。
 *
 * 参考支付宝「阿宝」的做法：转圈弧线 + 一句**随步骤变化**的短语。
 * 关键不在于转圈，在于那句话——「梳理中」和「浏览星巴克优惠」传达的
 * 信息完全不同，后者让人知道它在做哪一步，因而愿意再等一会儿。
 *
 * ## 这些短语的诚实性
 *
 * 服务端不是流式的，前端在拿到回复之前不可能知道它此刻具体在做什么。
 * 所以这里**只写流程里确实存在、且顺序固定的阶段**：找房必定是
 * 「解析条件 → 检索 → 排序 → 组织回答」，报修必定是「判断类别 →
 * 定紧急度 → 生成工单」。写成「正在浏览第 3 套房源」那种具体到
 * 对象的句子就成了编造——**编出来的进度比没有进度更糟**，
 * 它让用户以为系统知道一些它并不知道的事。
 *
 * 走到最后一句就停在那里，不循环回第一句：真的慢下来时，
 * 反复轮播只会让人以为它卡在了一个死循环里。
 */

/** 各条路线的真实阶段。键与 supervisor 的 intent 对齐。 */
const SCRIPTS = {
  search: ['读你的条件', '在库里检索房源', '按匹配度排序', '组织回答'],
  repair: ['读故障描述', '判断类别与紧急度', '生成工单'],
  contract: ['定位合同条款', '逐条比对', '组织回答'],
  bill: ['核对账单与缴费记录', '组织回答'],
  support: ['理解你要办的事', '核对办理条件', '准备确认单'],
  default: ['理解你的问题', '查证据', '组织回答'],
}

/** 每句停留多久。太快像跑马灯，太慢又显得没在动。 */
const STEP_MS = 1400

export default function Thinking({ intent = 'default' }) {
  const steps = SCRIPTS[intent] || SCRIPTS.default
  const [i, setI] = useState(0)

  useEffect(() => {
    setI(0)
    // 停在最后一句。到点了还没回来，说明这一步就是慢，
    // 不是流程往下走了
    const timer = setInterval(() => {
      setI((cur) => (cur >= steps.length - 1 ? cur : cur + 1))
    }, STEP_MS)
    return () => clearInterval(timer)
  }, [intent, steps.length])

  return (
    <div className="thinking" role="status" aria-live="polite">
      <span className="think-arc" aria-hidden="true" />
      <span className="think-steps">
        {/* 整句换掉会闪。只让新句子从下方推上来，
            旧句子往上走——读起来像流程在往前推进 */}
        <span className="think-reel"
              style={{ transform: `translateY(calc(${-i} * var(--think-line-h)))` }}>
          {steps.map((s) => (
            <span className="think-line" key={s}>{s}</span>
          ))}
        </span>
      </span>
    </div>
  )
}

/**
 * 在发出请求的那一刻猜这一轮走哪条路，只为选一份阶段清单。
 *
 * **猜错的代价只是短语不贴切，不会影响任何结果**——真正的路由在服务端，
 * 这里既不发指令也不改参数。正因为代价这么低，才可以用一张关键词表
 * 而不是再打一次接口：为了一个加载动画多等一个网络往返是本末倒置。
 */
export function guessIntent(text) {
  const t = text || ''
  if (/(报修|坏了|漏水|不出热水|堵了|修一下|maintenance)/.test(t)) return 'repair'
  if (/(合同|条款|违约|押金退|退租)/.test(t)) return 'contract'
  if (/(账单|房租|缴费|交房租|水电费)/.test(t)) return 'bill'
  if (/(预约|看房|取消|改期|办理|帮我)/.test(t)) return 'support'
  if (/(找|租|房|元|平米|室|区|附近|地铁|推荐)/.test(t)) return 'search'
  return 'default'
}
