"""可代办的操作——客服从「能查」走到「能做」。

参考通义千问 App、Siri 这类助手的交互：用户说一句「帮我把这个月房租交了」，
助手**不是直接执行**，而是先把要做的事摊开给用户看——交哪一笔、多少钱、
用什么方式——用户点确认才真正落库。

## 两阶段，一步都不能省

    ① 提议 propose()  →  返回一张确认卡片，**不产生任何副作用**
    ② 执行 execute()  →  只有拿到用户确认后才调后端写接口

`agent.run()` **永远只走到第一步**。第二步是独立的接口 `/agent/support/confirm`，
必须由前端在用户点了确认之后才调用。这条边界是硬的：
把执行塞进对话里，等于让一句话就能划走用户的钱。

## 为什么可以把待确认动作交给前端带回来

确认卡片会原样回到前端、再由前端回传。看起来像是可以被伪造，但**不构成
提权**：所有写操作都用用户自己的令牌调后端受控接口，用户本来就能在页面上
点同样的按钮。伪造一个 payload 能做的事，他打开网页也能做。
真正的权限判断在后端，与这里无关。

所以这里不做会话态、不存待确认队列——无状态实现少一整类
「确认的是上一条还是上上条」的竞态问题。

## 多轮槽位填充：助手问了问题，就得接得住答案

实测下来「不好用」的根源不是识别不准，是**每一轮都是孤立的**：

    用户：帮我约看房
    助手：想看哪一套？
    用户：周六下午两点        ← 上一轮的问题整个丢了，跑去查 FAQ

参考拼多多、美团那类客服的做法，缺三样东西：

1. **待办状态**（`pending`）：问了什么、已经填了哪些槽、还缺哪个，
   一路带着走。用户补一句就能接上，不必把整句话重说一遍。
2. **选项可点**（`options`）：「你有 6 条预约，要取消哪一条」后面跟一串
   纯文本，用户只能猜格式。改成结构化选项，前端渲染成按钮，
   点一下等于把值填进槽位。
3. **口头确认**：卡片出来以后用户说「好的」，那就是确认。
   只认按钮不认人话，是把交互做回了表单。

待办状态**不存在服务端**，随响应发给前端、下一轮再带回来——
与确认卡片同一个理由：无状态就没有「接的是上一条还是上上条」的竞态。

## 风险分级决定措辞，不决定要不要确认

    low     收藏、标记已读——做错了随手能撤
    medium  预约、报修、取消预约——有影响但可挽回
    high    缴费、续租、退租——涉及钱或不可逆

**三档都要确认**，分级只影响卡片上的措辞与提示强度。
「低风险就直接执行」听着效率高，但用户对助手的信任是一次性的：
只要有一次「我没让你做你就做了」，之后每一句都得提防。
"""

import re

from app.agents.booking.tools.parse_datetime import parse_datetime
from app.agents.support.tools import account, forms
from app.backend import BackendError

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# 提议阶段的返回状态
CONFIRM = "confirm"        # 已生成确认卡片，等用户点确认
NEED_SLOT = "need_slot"    # 还缺关键信息，得先问用户
NEED_LOGIN = "need_login"
NOT_APPLICABLE = "n/a"     # 这句话不是在要求做事，交回给别的处理路径
FAILED = "failed"


def _rows(data):
    if isinstance(data, dict):
        return data.get("list") or []
    return data or []


from app.agents.support.tools.money import money_text as _money


# ---------------------------------------------------------------- 意图识别

# 动作关键词。要求「动词 + 对象」都出现，**不能只靠对象词**：
# 「我的账单」是查询，「帮我交账单」才是操作。
# 少了动词这一层，用户每问一次余额都会被弹一张缴费确认卡。
_DO_VERBS = ["帮我", "给我", "替我", "去", "把", "请"]

ACTION_KEYWORDS = [
    # **催租排在交租前面。**「提醒他交房租」「催一下房租」里都有「交房租」，
    # 让 pay_bill 先看的话，房东想催租客，卡片上却是替自己交钱。
    ("remind_tenant", ["催租", "催缴", "催一下租", "催房租", "催一下房租",
                       "提醒欠租", "提醒一下欠租", "提醒租客", "提醒他交",
                       "提醒她交", "催他交", "催她交", "催一下欠"]),
    ("pay_bill", ["交房租", "交租", "缴费", "把房租交", "付房租", "交账单",
                  "把账单交", "缴租金", "交一下房租"]),
    ("cancel_viewing", ["取消预约", "取消看房", "别去看了", "不看了",
                        "把预约取消"]),
    # **改约要排在预约之前。** 「把看房改到周日」同时含着「约」和时间，
    # 排在 book_viewing 后面就会被它抢走，变成新约一条。
    #
    # 这里**故意不收「改成」「改到」**：它们太泛了。「把这条工单改成
    # 已完成」「把消息改成已读」都带着这两个字，而这张表是先到先得的，
    # 收进来就会把那两件事抢走。带宾语的说法交给下面的强共现去认。
    ("reschedule_viewing", ["改约", "改期", "改时间", "换个时间",
                            "换一个时间", "换时间", "重新约"]),
    ("book_viewing", ["约看房", "预约看房", "帮我约", "安排看房", "想去看",
                      "我要看房", "要看房", "想看房", "看房第", "约一下看房",
                      "去看看这套", "预约看"]),
    ("submit_repair", ["报修", "报个修", "提交报修", "报一下修", "叫维修",
                       "叫师傅"]),
    ("renew_contract", ["续租", "续签", "接着租", "再租"]),
    ("end_contract", ["退租", "解约", "不租了", "退房"]),
    ("unfavorite_house", ["取消收藏", "不收藏了", "别收藏了", "移出收藏"]),
    # **不能只写一个「收藏」。** 那样「我的收藏」「我收藏了哪些房子」
    # 都会被当成要收藏——它们是查询。所以这里只收明确的祈使说法，
    # 剩下的交给查询路由。「评价」同理。
    ("favorite_house", ["收藏这套", "收藏一下", "加收藏", "收藏它",
                        "帮我收藏", "收藏起来", "加入收藏", "把它收藏",
                        "收藏这个", "收藏那套"]),
    ("read_all_messages", ["消息全部已读", "全部已读", "标记已读",
                           "消息都已读"]),
    ("clear_history", ["清空浏览", "清除足迹", "清空足迹", "删掉浏览记录"]),
    ("submit_review", ["评价一下", "给个好评", "打分", "评个", "写评价",
                       "提交评价", "帮我评价", "帮我评", "去评价",
                       "评价租约", "评价一下房东"]),
    ("handle_appointment", ["同意预约", "确认预约", "通过预约", "拒绝预约",
                            "驳回预约"]),
    ("update_repair", ["工单完成", "维修完成", "修好了", "关闭工单",
                       "处理完了"]),
    ("toggle_house", ["上架", "下架", "把房源上", "把房源下"]),
    # ---- 租客确认签约。房东发起后合同处于「待生效」，要租客点头才生效
    # 并排出整个租期的账单。原先这一步只能回页面点，而它恰恰是最需要
    # 「先把要签的是什么摆清楚再确认」的一步。
    # 长表单的触发语。放在 activate_contract 之前：「发起签约」和
    # 「确认签约」只差一个字，顺序错了房东想发起、却被当成租客确认。
    ("publish_house", ["发布房源", "发个房源", "新增房源", "我要发布",
                       "挂个房子", "上架新房源", "发一套房"]),
    ("sign_contract", ["发起签约", "给租客签约", "签个合同", "起草合同",
                       "发起合同"]),
    ("edit_house", ["修改房源", "改房源", "改一下房源", "编辑房源",
                    "改租金", "改一下租金", "改价格"]),
    ("activate_contract", ["确认合同", "确认签约", "同意签约", "签这份合同",
                           "确认这份合同", "我同意签"]),
    # ---- 房东申请房源核验
    ("verify_request", ["申请核验", "提交核验", "房源核验", "申请认证",
                        "去核验"]),
    # ---- 账号资料。改手机号是最常改的一项，改密码**故意不做**：
    # 密码不该经过对话框，那是登录页该干的事。
    ("update_profile", ["改手机号", "换手机号", "更新手机号", "改联系方式",
                        "改一下手机", "手机号改成"]),
]

# 问句词。带这些的句子是在**问**而不是在**要求做**，
# 「怎么退租」是咨询流程，「帮我退租」才是要执行。
#
# 这张表宁可写长也不能写短。漏掉一个词的代价是不对称的：
# 漏了，用户咨询一次就被弹一张确认卡，重则手滑点确认；
# 多了，无非是把一句指令当成了提问，再说一遍就是。
#
# 后两组是被测试逼出来的——「我的报修到哪一步了」原先命中「报修」
# 触发了提交报修，「续租需要什么手续」原先命中「续租」触发了续租。
_ASKING = [
    # 疑问词
    "怎么", "如何", "能不能", "可以吗", "多久", "什么时候", "要不要",
    "是不是", "有没有", "为什么", "多长时间", "几天", "哪些",
    # 咨询流程
    "流程", "规定", "一般", "需要什么", "手续", "条件", "材料", "注意什么",
    # 问进度（这是查询，不是指令）
    "到哪一步", "处理了吗", "处理好了吗", "回复了吗", "进度", "怎么样了",
    "有结果", "通过了吗", "审核过了吗",
    # 问钱、问责任、问后果。**这一组是一次真实用户测试撞出来的**：
    # 「提前一个月退租要赔多少钱」「押金我能扣多少」都弹出了退租确认卡，
    # 「维修费谁出」弹出了报修卡——三句都是在问规则，句子里只是恰好有
    # 「退租」「维修」。表里原先没有「多少」和「谁」，一个问句都认不出来。
    "多少", "谁出", "谁付", "谁负责", "谁承担", "谁来", "会怎样", "会怎么样",
    "啥时候", "咋",
]

# **软否决词**：只在句子里**没有祈使语气**时才一票否决。
#
# 「吗」和「什么」是最强的疑问信号，但不能无条件否决——
# 「能帮我交下房租吗」是客气的请求，不是提问，否决了就等于逼用户说话生硬。
# 判断标准是有没有「帮我」这类祈使词：
#
#   退租要打扫干净吗    没有祈使词 → 是提问，不执行
#   能帮我交下房租吗    有「帮我」 → 是请求，可以执行
#
# 这一层同样是被验收集逼出来的：「什么情况算紧急报修」曾触发提交报修、
# 「退租要打扫干净吗」曾触发退租、「可以用公积金付房租吗」曾触发缴费。
# 三条都是知识型问题，却因为句子里有业务动词而被当成了指令。
# 「哪」一个字覆盖哪种／哪个／哪些／哪儿，不必逐个列举
_SOFT_ASKING = ["吗", "什么", "哪", "呢", "算不算", "属于", "谁"]

# **要的是一份文书，不是去办这件事。**
#
# 「帮我写一份退租申请书」里有「帮我」、有「退租」，三层规则全部命中，
# 结果是一张**退租确认卡**——用户要的是一段文字，助手却准备替他解约。
# 这类说法的共同点是宾语是一件「文字产物」，把它挑出来一票否决。
# 只收文书类名词和「写一份/写一封」：「帮我写个评价」是真要提交评价，
# 「起草一份合同」是房东真要发起签约，这两类不能被连带否决。
_DOCUMENT = ("申请书", "申请表", "申请信", "模板", "范本", "范文", "文案",
             "说明书", "通知书", "一封信", "写一份", "写一封", "拟一份")


# 「动词 × 对象」共现规则。固定短语覆盖不了中文的语序自由：
# 「帮我把**这个月**房租交了」里，动词和宾语之间插了三个字，
# 任何写死的短语都对不上。实测就是被这一句打回来的。
#
# 共现只要求两类词都出现在同一句里，不管顺序、不管中间隔多远，
# 这才是中文口语的常态。
# **强共现**：这两类词凑在一起，意思已经足够明确，不需要再要求祈使语气。
#
# 「预约明天上午看房第三套」里没有「帮我」，但没人会用这种句式提问。
# 普通共现规则要求祈使词托底，是为了防「房租什么时候交」被当成要交钱；
# 而「预约 × 看房」不存在这种歧义——真要问规则，句子里会带
# 「怎么」「吗」，那两层否决先拦下了。
STRONG_PAIRS = [
    ("book_viewing", ["预约", "约", "要看", "想看"], ["看房", "看一下房"]),
    # 「取消之前那个周六的看房」——「取消看房」四个字被「之前那个周六的」
    # 隔断了，固定短语对不上；而这句话没有「帮我」，也就走不到下面那层
    # 要求祈使语气的共现规则。**结果是一句再明确不过的取消，一条规则都不命中。**
    # 「取消 × 看房」不存在第二种解释：问规则的说法会带「怎么」「吗」，
    # 前面两层否决已经拦下了。
    ("cancel_viewing", ["取消", "退掉", "撤销", "不去了"],
     ["看房", "预约"]),
    # 「把站内消息都标记成已读」——原先的词表要求「标记已读」四个字连着，
    # 中间多一个「成」就不认了。「已读」这个词除了这件事没别的用法。
    ("read_all_messages", ["标记", "设为", "设成", "改成", "全部", "都"],
     ["已读"]),
    # 「清空我的浏览足迹」——中间夹了「我的」，「清空浏览」就断开了。
    # 清空／清除／删掉本身就是祈使，不需要再要求「帮我」托底。
    ("clear_history", ["清空", "清除", "删掉", "删除"],
     ["浏览", "足迹", "浏览记录"]),
    # 「把这条工单标记为已完成」
    ("update_repair", ["标记", "改成", "设为", "置为", "关掉"],
     ["完成", "已完成", "修好"]),
    # 「帮我把这套收藏了」——固定短语要求「收藏」和指代词连着，中间
    # 夹一个「把…了」就断开了。「收藏 × 这套」不存在第二种解释：
    # 真要问怎么收藏，句子里会带「怎么」，前面那层否决先拦下了。
    ("favorite_house", ["收藏"],
     ["这套", "那套", "这个", "那个", "它", "房源", "一下"]),
    # 「把周六那个看房改到周日」——「改 × 看房」凑在一起没有第二种解释，
    # 不必再要求「帮我」托底。放在最后，前面几条更具体的先挑走。
    ("reschedule_viewing", ["改到", "改成", "换到", "挪到", "推迟到"],
     ["看房", "预约", "那条", "那个时间"]),
]

ACTION_PAIRS = [
    ("remind_tenant", ["提醒", "催"], ["欠租", "租客", "欠费", "拖欠"]),
    ("pay_bill", ["交", "缴", "付", "支付", "结清"],
     ["房租", "租金", "账单", "费用", "这个月"]),
    ("cancel_viewing", ["取消", "退掉", "撤销"], ["预约", "看房"]),
    ("reschedule_viewing", ["改", "换", "调"], ["预约", "看房", "时间", "时段"]),
    ("book_viewing", ["约", "预约", "安排"], ["看房", "看一下", "去看"]),
    ("submit_repair", ["报", "叫", "提交"], ["修", "维修", "师傅"]),
    ("renew_contract", ["续", "接着", "再租"], ["租", "约", "合同"]),
    ("end_contract", ["退", "解", "不租"], ["租", "约", "房"]),
    ("toggle_house", ["上", "下"], ["架"]),
]


# 纯粹的客气话。把它们从句子里去掉之后，如果**再没有别的疑问词**，
# 那这句话就只是说得客气，不是在提问。
_POLITENESS = ("能不能", "可不可以", "可以吗", "行不行", "方便吗", "麻烦你",
               "能否", "可以帮", "能帮")


_CAN_BOOK = re.compile(r"(能|可以|可不可以)(帮我)?(约|预约)")


def asks_rather_than_requests(text):
    """这句话是在**问**，还是在**请我去做**。给「模型挑中了写操作」那道护栏用。

    和 router._looks_like_question 的区别在于**认得客气的请求**：
    「我想明天上午去看这套，可以吗」「明天上午十点吧」是在约看房，
    只是说得客气。护栏第一版用的是 _looks_like_question，它见「可以吗」就判提问、
    见句尾「吧」也判提问——两句正常的约看房被拦成知识问答，回了一句
    「平台资料里没写到这一条」。

    判据：
      1. 要一份文书（申请书、模板）——不是去办，算「问」；
      2. 疑问成分只有客气话（能不能 / 可以吗 / 能帮）——是请求；
      3. 还剩真正的疑问词（多少、谁出、怎么、会怎样）——是问；
      4. 只带软疑问词（吗 / 什么 / 哪 / 谁）又没有祈使词——是问。
    句尾的「吧」不算疑问：「那就十点吧」是拍板，不是提问。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if any(word in raw for word in _DOCUMENT):
        return True
    if any(p in raw for p in _POLITENESS) and _only_politeness(raw):
        return False
    # 「这套能约周六下午看吗」——带着具体时间问能不能约，就是在约。
    # 原先被判成提问，模型挑中的约看房被拦下，对话兜底回了一句
    # 「你告诉我具体是周六哪天、下午几点」：他刚说过。
    # 只放行「约」这一类且必须带时间：「押金能退吗」同样是「能 X 吗」，那是在问规则。
    if (_CAN_BOOK.search(raw) and (parse_datetime(raw) or {}).get("date")
            and not any(word in raw for word in _ASKING)):
        return False
    if any(word in raw for word in _ASKING):
        return True
    return (any(word in raw for word in _SOFT_ASKING)
            and not any(verb in raw for verb in _DO_VERBS))


def _only_politeness(raw):
    """句子里的疑问成分**只有客气话**吗。

    「能不能把看房改成周日」去掉「能不能」就什么疑问词都不剩了——是请求。
    「能不能帮我看看退租要注意什么」去掉之后还剩「注意什么」——是咨询。
    差别就在这里，而不在客气与否。
    """
    stripped = raw
    for word in _POLITENESS:
        stripped = stripped.replace(word, "")
    return not any(word in stripped for word in _ASKING)


def _explicit(raw):
    """只认**说法明确**的那两层：固定短语与强共现。认不出返回空串。

    抽出来是因为「客气的请求」那条路只允许走到这里为止，
    不能再往下落到宽泛的动词×对象共现。
    """
    for name, keywords in ACTION_KEYWORDS:
        if any(kw in raw for kw in keywords):
            return name
    for name, verbs, objects in STRONG_PAIRS:
        if any(v in raw for v in verbs) and any(o in raw for o in objects):
            return name
    return ""


# **「提醒我交房租」不是「帮我交房租」。** 他要的是知道什么时候该交、交多少，
# 原先被当成缴费，弹了一张 750 元的确认卡。高风险卡要点按钮才执行，
# 扣不了钱，但答非所问。去掉动作之后这句话会落到查账单上。
_REMIND_ME = ("提醒我", "提醒一下我", "到时候告诉我", "记得告诉我", "别让我忘了")


def reminds_me(text):
    """「提醒我交房租」这类：要的是提醒，不是现在去办。"""
    return any(word in (text or "") for word in _REMIND_ME)


def detect(text):
    """判断这句话是不是在要求代办一件事，返回动作名或空串。

    三道过滤：

    1. **问句词一票否决**。「怎么退租」问的是流程，不是让你现在就退。
       没有这一层，用户咨询一次退租政策就会收到一张退租确认卡——
       吓人，而且会让人再也不敢问。
    2. **固定短语**：说法明确的直接命中，比如「取消预约」「全部已读」。
    3. **动词 × 对象共现**：兜住语序被打散的口语说法。
       这一层要求句子里带祈使语气（「帮我」「把」之类），
       否则「房租什么时候交」这种陈述也会被当成要交钱。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    if any(word in raw for word in _DOCUMENT):
        return ""
    if any(word in raw for word in _REMIND_ME):
        return ""

    imperative = any(verb in raw for verb in _DO_VERBS)

    if any(word in raw for word in _ASKING):
        # **中文里请人办事，十有八九是包在问句里的。**
        #
        # 「能不能把这个看房改成周日下午三点」——问句词一票否决，于是
        # 这句被当成了咨询，助手回「时间没问题，请到房源页面自己提交」。
        # 用户的原话是「你应该帮我改啊」。他没说错：**这是请求，
        # 不是提问**，只是说得客气。一律否决等于惩罚礼貌的说法。
        #
        # 松口松得很窄：必须**同时**带祈使成分（帮我／把／给我），
        # 而且必须命中下面那两层**明确**的规则；一旦松到最后那层
        # 宽泛的共现，「能不能帮我看看退租要注意什么」里的
        # 「退 × 租」就会变成一张退租确认卡——那正是这道否决要防的事。
        if not imperative or not _only_politeness(raw):
            return ""
        return _explicit(raw)

    if not imperative and any(word in raw for word in _SOFT_ASKING):
        return ""

    explicit = _explicit(raw)
    if explicit:
        return explicit

    # 共现规则需要祈使语气托底。少了这个约束，「账单还有 2 笔没交」
    # 这种陈述句也会被判成「要交钱」——那是查询，不是操作。
    if not any(verb in raw for verb in _DO_VERBS):
        return ""

    for name, verbs, objects in ACTION_PAIRS:
        if any(v in raw for v in verbs) and any(o in raw for o in objects):
            return name
    return ""



# ---------------------------------------------------------------- 多轮槽位

# 肯定与否定。用户对着确认卡片说「好的」，那就是确认——
# 只认按钮不认人话，等于把对话做回了表单。
AFFIRM = ["好", "好的", "行", "可以", "确认", "对", "是的", "嗯", "没问题",
          "就这个", "就它", "同意", "ok", "OK", "yes", "执行", "去吧", "麻烦了"]
DENY = ["不", "不用", "算了", "取消", "先不", "再想想", "不要", "no", "等等"]


# 肯定 / 否定词后面还能跟的东西：语气词、客气话、再叠一个同向的词。
# 除此之外还有别的内容，这句话就不只是「好」或「不」。
_FILLER = set("的吧啊呀了哈嗯呢哦噢喔滴啦嘛，,。.!！~ ")
_AFFIRM_TAIL = ("谢谢", "麻烦了", "麻烦你了", "就这样", "没问题", "一下", "帮我办",
                "办吧", "交吧", "约吧", "提交", "确认") + tuple(AFFIRM)
_DENY_TAIL = ("谢谢", "先这样", "改天", "再说", "需要", "用", "要") + tuple(DENY)
# 「不约了」「先不交了」：不 + 一个动词
_NOT_VERB = re.compile(r"^不[一-鿿]{1,2}了?$")


def _only_tail(rest, tails):
    """`rest` 能不能全部被语气词和 `tails` 里的词吃掉。"""
    rest = rest.strip()
    while rest:
        if rest[0] in _FILLER:
            rest = rest[1:]
            continue
        for word in sorted(tails, key=len, reverse=True):
            if rest.startswith(word):
                rest = rest[len(word):]
                break
        else:
            return False
    return True


def _is_short_reply(text, words, tails, extra=None):
    raw = (text or "").strip().strip("，。！,.!~ ")
    if not raw or len(raw) > 8:
        return False
    # **问句不是应答。** 原先只看开头：「可以养宠物吗」「对面有超市吗」
    # 「行李能放吗」都以肯定词开头，被当成了「好的」——卡片挂着的时候，
    # 中风险的那几类（约看房、收藏、报修）会被这句话**直接执行**。
    if raw[-1] in "吗么呢?？" or "吗" in raw:
        return False
    for word in sorted(words, key=len, reverse=True):
        if raw == word:
            return True
        if raw.startswith(word):
            rest = raw[len(word):]
            if _only_tail(rest, tails) or (extra and extra(rest)):
                return True
    return False


def is_affirm(text):
    """用户是不是在肯定上一张确认卡片。

    要求整句话短——「好的」是确认，「好的，那退租要打扫吗」是新问题。
    也不能只看开头：「好贵啊」「对面有超市吗」开头都是肯定词。
    """
    return _is_short_reply(text, AFFIRM, _AFFIRM_TAIL)


def is_deny(text):
    """「不用了」「算了不约了」是否定；「不知道哪套好」「不贵」不是。"""
    return _is_short_reply(
        text, DENY, _DENY_TAIL,
        extra=lambda rest: bool(_NOT_VERB.match(rest.strip("，,。 "))))


# 序数词 -> 下标。用户看到一串选项，最自然的回答就是「第一个」。
_ORDINALS = {
    "第一": 0, "第1": 0, "第一个": 0, "第一条": 0, "1": 0, "一": 0,
    "第二": 1, "第2": 1, "第二个": 1, "第二条": 1, "2": 1, "二": 1,
    "第三": 2, "第3": 2, "第三个": 2, "第三条": 2, "3": 2, "三": 2,
    "第四": 3, "第4": 3, "第四个": 3, "4": 3, "四": 3,
    "第五": 4, "第5": 4, "第五个": 4, "5": 4, "五": 4,
}


# 「都可以」「随便」——用户把选择权交回来了，取第一个。
# 不处理的话会一直卡在追问上：助手问、用户说都行、助手再问一遍，
# 死循环。第一个选项通常也是最近／最相关的那个。
_ANY_OF_THEM = ["都可以", "都行", "随便", "都成", "你定", "你看着办",
                "无所谓", "哪个都行", "都方便"]


def _pick_from_request(text, options):
    """请求原话里是不是已经指明了哪一项。

    和 pick_option 的区别：**不认「都行 / 随便」**。那类词是回答追问时
    「你挑吧」的意思；出现在第一句请求里时，用户并没有在点选项——
    取消、确认这类动作宁可多问一句，也不能替他挑第一条。
    """
    raw = (text or "").strip()
    if not raw or not options or any(word in raw for word in _ANY_OF_THEM):
        return ""
    # **不拿选项的值（预约 id）去比原话。** 请求里常带日期和数字：
    # 「取消 11 月 8 号那个看房」里的 11 会撞上 id=11 的那条——
    # 取消掉的就是另一场看房。回答追问时句子短，那样比没问题；这里不行。
    # 只认两种不会撞车的：「最早 / 最晚」按时间挑；说中了标签里的完整片段
    # （房源编号、日期），且只有一条说中。
    timed = [(m.group(0), o["value"]) for o in options
             for m in [_LEADING_TIME.match(o.get("label") or "")] if m]
    if timed and len(timed) == len(options):
        if any(w in raw for w in ("最早", "最先", "早的那")):
            return min(timed)[1]
        if any(w in raw for w in ("最晚", "最迟", "晚的那")):
            return max(timed)[1]
    scored = []
    for option in options:
        label = (option.get("label") or "").replace("　", " ")
        hit = sum(len(p) for p in label.split() if len(p) >= 5 and p in raw)
        if hit:
            scored.append((hit, option["value"]))
    if len(scored) == 1:
        return scored[0][1]
    return ""


def _value_said(value, raw):
    """用户这句话里是不是真的说出了这个选项的值。

    **纯数字的值必须整段匹配，不能当子串找。**

    预约、工单这些选项的值是数据库自增 id——「20」「17」这种两位数。
    直接 `value in raw` 的话，用户回一句「2026-09-12 09:00 DL0007」，
    里面的「2026」含着「20」，于是选中了 id 为 20 的那一条。
    实测就是这么把**另一场看房**改了期的：卡片上写着 DL0075，
    而他说的是 DL0007。

    房源编号、合同号那类带字母前缀的值不受影响，仍然按子串找——
    「DL0007 那套」本来就该命中。
    """
    value = str(value or "")
    if not value:
        return False
    if not value.isdigit():
        return value in raw
    # 前后不能再挨着数字，否则就是从别的数字里切出来的
    return bool(re.search(r"(?<!\d)%s(?!\d)" % re.escape(value), raw))


def pick_option(text, options):
    """把用户这句话对应到某个选项，返回选项的 value 或空串。

    三种命中方式，按可靠性排序：

    1. 直接说出了值（房源编号、合同号）——最可靠
    2. 说了标签里的一段（「桂林街那套」）
    3. 说了序数（「第一个」）——最省事，也最容易错位，所以放最后
    """
    raw = (text or "").strip()
    if not raw or not options:
        return ""

    if any(word in raw for word in _ANY_OF_THEM):
        return options[0]["value"]

    for option in options:
        if _value_said(option.get("value"), raw):
            return option["value"]

    # 按「他说中了多少」打分，**取唯一的最高分**。
    #
    # 原先是从上往下扫，谁先命中一段就是谁。可几条预约的标签里都写着
    # 同一个房源编号——「2026-09-12 09:00 DL0007」里的 DL0007 三条都有，
    # 于是选中的永远是列表里最靠前的那条。实测就是这么改错了一场看房。
    # 说中得越多越可信；并列第一说明**他说的话本身就分不出来**，
    # 这时候宁可回去再问一次，也不能按顺序猜一个。
    scored = []
    for option in options:
        label = (option.get("label") or "").replace("　", " ")
        hit = sum(len(piece) for piece in label.split()
                  if len(piece) >= 3 and piece in raw)
        if hit:
            scored.append((hit, option["value"]))
    if scored:
        best = max(score for score, _ in scored)
        winners = [value for score, value in scored if score == best]
        if len(winners) == 1:
            return winners[0]
        return ""

    # 「同意最早那个看房申请」——选项标签开头是时间，「最早 / 最晚」
    # 按时间挑，而不是按列表顺序。实测原先追问了一句「要处理哪一条」，
    # 而用户已经说清楚了。「最近」不收：可以是「最新的」也可以是「离现在最近的」。
    timed = [(m.group(0), o["value"]) for o in options
             for m in [_LEADING_TIME.match(o.get("label") or "")] if m]
    if timed and len(timed) == len(options):
        if any(w in raw for w in ("最早", "最先", "早的那")):
            return min(timed)[1]
        if any(w in raw for w in ("最晚", "最迟", "晚的那")):
            return max(timed)[1]

    compact = raw.strip("，。 ")
    for word, index in _ORDINALS.items():
        if compact == word or _ordinal_prefix(compact, word):
            if index < len(options):
                return options[index]["value"]
    return ""


# 光秃秃的数字后面只认量词：「一个」「1号」「二套」是在挑，
# 「一般」「一直」「一下」「10号」不是
_MEASURE = "个条套项号笔场份种间"


def _brief(text, limit=30):
    """复述用的短句：超长时在分句处断开，不从字中间切。

    原先 `content[:30]`，复述成了「……房东消息也不回，处。要拍张照片吗？」——
    「处理一下」被切掉了后三个字。
    """
    text = (text or "").strip().rstrip("，,。.；;！!")
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(p) for p in "，,。；;")
    if cut >= limit // 3:
        return head[:cut]
    return head + "…"


def _ordinal_prefix(text, word):
    """这句话是不是以序数开头。

    **「第 X」开头照旧算；光一个「一」「1」不算，除非后面紧跟量词。**
    原先 `startswith("一")`：报修追问「要拍张照片吗」时用户问
    「一般多久能有人来修啊」——以「一」开头，被当成选了第一个选项
    「跳过，直接提交」，问题没人答，卡片直接出来了。
    """
    if not text.startswith(word):
        return False
    if word.startswith("第"):
        return True
    rest = text[len(word):]
    # 「一，就它」「1 吧」：后面是标点或空格也算在挑
    return bool(rest) and (rest[0] in _MEASURE or rest[0] in "，,。、！! 吧")


_LEADING_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?")


def _option(value, label):
    return {"value": value, "label": label}


def _ask(slot, question, options=None, kind="", carry=None):
    """一次追问：缺哪个槽、问什么、有哪些可点的选项。

    `kind` 是给前端看的提示。目前只有 "upload"：这一项要的不是打字，
    而是选一张图——前端据此渲染上传按钮，而不是让用户对着输入框发愣。
    """
    ask = {"slot": slot, "question": question, "options": options or []}
    if kind:
        ask["kind"] = kind
    if carry:
        # **提议器这一轮算出来的东西，得让下一轮还看得见。**
        #
        # 报修的故障描述是从「当前这句话」现推的。追问照片之后，
        # 「当前这句话」变成了照片答案，描述就丢了——实测的表现是
        # 工单内容变成 `__skip__` 或者一串图片地址。**这是会写坏数据的。**
        # `carry` 里的东西会并进待办的槽位，下一轮原样取回。
        ask["carry"] = dict(carry)
    return ask


# ---------------------------------------------------------------- 提议

def _card(name, label, summary, risk, params, detail=None, warning=""):
    """一张确认卡片。

    `summary` 是给用户看的一句话，**必须把关键事实写全**——
    交哪一笔、多少钱、哪一天。用户点确认时看到的就这一句，
    写得含糊等于骗签字。
    """
    return {
        "name": name,
        "label": label,
        "summary": summary,
        "risk": risk,
        "params": params,
        "detail": detail or {},
        "warning": warning,
    }


# 用户话里点名的账单类型 → 账单上的类型值。
#
# **不认这个的后果实测过**：用户说「帮我把这个月的房租交了」，
# 代办挑的是「押金 0 元」——他说的明明是房租，而系统按「最早到期」
# 挑了一张风马牛不相及的账单，还是一张金额为零的。
_BILL_KIND_WORDS = [
    (("房租", "租金", "月租", "房钱"), ("rent", "租金")),
    (("押金", "保证金"), ("deposit", "押金")),
    (("服务费", "中介费", "佣金"), ("service", "服务费")),
    (("水电", "水费", "电费"), ("utility", "水电费")),
]


def _bill_kind_wanted(text):
    """用户点名了哪一类账单。没点名返回 None。"""
    for words, kinds in _BILL_KIND_WORDS:
        if any(w in (text or "") for w in words):
            return kinds
    return None


def _propose_pay_bill(text, token, backend, context):
    # 只收我要交的：房东账号不带 side 会拿到租客欠他的账单，卡片就成了替租客付钱
    rows = _rows(backend.get("/api/bills/mine", params={"side": "tenant"},
                             token=token))
    # **金额为 0 的不算待缴。**
    #
    # 免押的合同会排出一张 0 元的押金账单——它在库里是一条正常记录，
    # 但「帮我缴这 0 元」是一句没有意义的话，而它恰好因为到期日最早
    # 被排在了第一位。实测「帮我把这个月的房租交了」得到的就是
    # 「要我帮你缴纳押金 0 元吗」。
    #
    # **付不了的也不算。** 租约已结束的欠款后端拒收（只能线下找房东结），
    # 而它往往到期最早、正好排在第一位——旧租约还欠一笔、新租约正常交租的人
    # 说「帮我交房租」，卡片给的是那笔旧欠款，点确认才被告知付不了。
    # 后端没给 payable 字段时按可付处理，和改动前一致
    owed = [b for b in rows
            if b.get("status") == "unpaid" and float(b.get("amount") or 0) > 0]
    unpaid = [b for b in owed if b.get("payable", True)]
    if not unpaid:
        if owed:
            # 只剩旧租约的欠款时不能说「都结清了」——钱确实还欠着，只是线上交不了
            return (NOT_APPLICABLE,
                    "你还有 %d 笔已结束租约的欠款，合计 %s 元。租约结束后线上不能再缴，"
                    "请与房东线下结清。" % (len(owed), _money(sum(float(b.get("amount") or 0)
                                                           for b in owed))),
                    None, None)
        return NOT_APPLICABLE, "你的账单都结清了，没有需要缴的。", None, None

    # 用户点名了类型就先按类型筛。**他说「房租」，就不该给他押金**——
    # 「最早到期」是没有别的线索时的默认，不是可以盖过原话的规则
    wanted = _bill_kind_wanted(text)
    if wanted:
        same = [b for b in unpaid
                if str(b.get("bill_type") or b.get("type") or "") in wanted
                or account.bill_type_text(b) in wanted]
        if same:
            unpaid = same

    # 多笔待缴时选最早到期的那一笔，并在卡片上说明还有几笔。
    # 不自作主张一次全交——那是一笔大得多的钱，必须由用户逐笔决定。
    unpaid.sort(key=lambda b: b.get("due_date") or "")
    chosen = str((context or {}).get("bill_no") or "")
    target = next((b for b in unpaid if str(b.get("bill_no")) == chosen), None)
    wants_choice = any(word in (text or "") for word in
                       ("选择账单", "选一笔", "哪一笔", "哪笔"))
    if len(unpaid) > 1 and target is None and wants_choice:
        options = []
        for bill in unpaid[:5]:
            kind = account.bill_type_text(bill)
            label = "%s · %s 元 · %s到期" % (
                kind, _money(bill.get("amount")), bill.get("due_date") or "待定")
            options.append(_option(bill.get("bill_no"), label))
        question = "你有 %d 笔待缴账单，要支付哪一笔？" % len(unpaid)
        return NEED_SLOT, question, None, _ask("bill_no", question, options)
    target = target or unpaid[0]
    amount = _money(target.get("amount"))
    kind = account.bill_type_text(target)
    due = target.get("due_date") or ""

    summary = "缴纳%s %s 元" % (kind, amount)
    if due:
        summary += "（%s 到期）" % due
    warning = ""
    if len(unpaid) > 1:
        warning = "你还有另外 %d 笔待缴账单，这次只交这一笔。" % (len(unpaid) - 1)

    return CONFIRM, "", _card(
        "pay_bill", "缴费", summary, RISK_HIGH,
        {"bill_no": target.get("bill_no"), "method": "alipay"},
        detail={"amount": target.get("amount"), "bill_type": kind,
                "due_date": due, "unpaid_total": len(unpaid)},
        warning=warning)


def _live_appointments(token, backend):
    # 取消 / 改期的都是**我约的**。不带 side 时，房东账号会拿到别人约他房子的预约——
    # 而后端允许房东取消那些，「取消我的看房」就可能取消掉一个陌生人的（2026-10-22）
    rows = _rows(backend.get("/api/appointments/mine", params={"side": "tenant"},
                             token=token))
    return [r for r in rows if r.get("status") in ("pending", "confirmed",
                                                   "待审核", "已确认")]


def _narrow_by_time(rows, text):
    """用户话里说了时间就按它先筛一遍。筛不出来就原样返回。

    **不筛的代价是把选择题重新丢回给用户。** 实测「取消之前那个周六的
    看房」得到的是七条预约的列表——他已经说了是周六那条，助手却当没听见。
    七选一里挑错一条，取消的就是另一场看房。

    只在筛完仍有结果时才采用：解析错了宁可多问一句，也不能悄悄把
    候选缩到错误的那一条上。
    """
    parsed = parse_datetime(text or "") or {}
    day = parsed.get("date")
    if not day:
        return rows
    stamp = day.strftime("%Y-%m-%d")
    hit = [r for r in rows if str(r.get("viewing_time") or "").startswith(stamp)]
    return hit or rows


def _appointment_options(rows):
    return [_option(str(r.get("id")),
                    "%s %s" % (str(r.get("viewing_time") or "")[:16],
                               r.get("house_no", "")))
            for r in rows]


def _propose_cancel_viewing(text, token, backend, context):
    pending = _live_appointments(token, backend)
    if not pending:
        return NOT_APPLICABLE, "你没有可以取消的预约。", None, None

    chosen = context.get("appointment_id") or ""
    if not chosen:
        pending = _narrow_by_time(pending, text)
    options = _appointment_options(pending)
    if not chosen and len(pending) > 1:
        chosen = _pick_from_request(text, options)
    if not chosen and len(pending) > 1:
        return (NEED_SLOT, "你有 %d 条预约，要取消哪一条？" % len(pending), None,
                _ask("appointment_id", "要取消哪一条预约？", options))

    target = pending[0]
    if chosen:
        for row in pending:
            if str(row.get("id")) == str(chosen):
                target = row
                break

    return CONFIRM, "", _card(
        "cancel_viewing", "取消预约",
        "取消 %s 对 %s 的看房预约"
        % (target.get("viewing_time", ""), target.get("house_no", "")),
        RISK_MEDIUM,
        {"appointment_id": target.get("id"), "status": "cancelled"},
        detail=target), None


def _propose_reschedule_viewing(text, token, backend, context):
    """改约：把已有的一条看房换个时间。

    **这件事之前根本做不了。** 实测用户约完说「能不能改成本周日下午三点」，
    助手回的是「时间没问题，请到具体房源页面点『预约看房』完成提交」——
    他刚在这儿约成了，助手却让他自己回页面重来一遍。他的原话是
    「你应该帮我改啊」。

    后端没有「改时间」这个接口（PATCH 只收 confirmed/rejected/cancelled），
    所以这一项实际是**先约新的、再退旧的**两步。顺序不能反：先取消的话，
    万一新时段建不出来（时段冲突、房源下架），他就两头落空了。
    """
    rows = _live_appointments(token, backend)
    if not rows:
        return NOT_APPLICABLE, "你现在没有可以改期的看房预约。", None, None

    chosen = context.get("appointment_id") or ""
    if not chosen:
        # **改约这一项刻意不按句子里的时间去筛。**
        #
        # 取消时句子里的时间说的是「哪一条」，筛得越准越好；改约不是——
        # 「改成周日下午三点」里的时间是**新时间**。拿它去筛旧预约，
        # 万一正好撞上一条已有的，选中的就是错的那条。
        # 宁可多问一句，也不能默默改错一场看房。
        if len(rows) > 1:
            return (NEED_SLOT, "你有 %d 条预约，要改哪一条？" % len(rows), None,
                    _ask("appointment_id", "要改哪一条预约？",
                         _appointment_options(rows),
                         carry={"new_time": context.get("new_time") or text}))
        chosen = str(rows[0].get("id"))

    target = None
    for row in rows:
        if str(row.get("id")) == str(chosen):
            target = row
            break
    if target is None:
        return FAILED, "没找到那条预约，可能已经取消了。", None, None

    # 新时间：优先用槽位里存的，其次从这句话里读。
    #
    # **旧预约的时间要先排除掉。** 「把周六那条改成周日下午三点」里有
    # 两个时间，`parse_datetime` 只会返回先出现的那个——正好是旧的。
    # 所以拿 new_time 槽位兜住：追问时问的就是新时间。
    raw_new = context.get("new_time") or ""
    parsed = parse_datetime(raw_new or text) or {}
    day, clock = parsed.get("date"), parsed.get("time")
    old_when = str(target.get("viewing_time") or "")[:16]
    same = bool(day and clock) and ("%s %s" % (
        day.strftime("%Y-%m-%d"), clock.strftime("%H:%M"))) == old_when
    if same:
        day = clock = None            # 读到的就是它现在的时间，等于没说

    if not day or not clock:
        # 说清楚为什么还在问。他说「改成周日下午三点」、助手回一句
        # 「改到什么时候？原来是周日下午三点」，看着像没听懂——
        # 实际是这条**本来就约在那个时间**。
        question = ("这条本来就约在 %s，要改到别的时间吗？" % old_when
                    if same else "改到什么时候？原来是 %s。" % old_when)
        return (NEED_SLOT, question, None,
                _ask("new_time", question, [
                    _option("明天下午三点", "明天下午三点"),
                    _option("这周六上午十点", "本周六上午十点"),
                    _option("这周六下午两点", "本周六下午两点"),
                    _option("这周日下午三点", "本周日下午三点")],
                     carry={"appointment_id": chosen}))

    when = "%s %s" % (day.strftime("%Y-%m-%d"), clock.strftime("%H:%M"))
    return CONFIRM, "", _card(
        "reschedule_viewing", "看房改期",
        "把 %s 的看房从 %s 改到 %s"
        % (target.get("house_no", ""), old_when, when),
        RISK_MEDIUM,
        {"appointment_id": target.get("id"), "house_no": target.get("house_no"),
         "viewing_time": when},
        detail=dict(target, new_time=when),
        warning="新时间约上的同时原来那条自动取消；新时间约不上，原来那条照旧保留。"
                "新的一条同样要等房东确认。"), None


def _exec_reschedule_viewing(params, token, backend):
    """**新的一条和退掉旧的一条在后端一个事务里完成**（`replaces`）。

    原先是先建新的、再退旧的：反过来写的话，新时段一旦建不出来（时段冲突、
    房源已下架），用户就从「有一条看房」变成了「一条都没有」。2026-10-22
    后端规定同一套房只能留一条待看的预约，先建新的会被挡住，于是把两步并进
    同一个请求——新的约不上，旧的原样留着；约上了，旧的同时取消。
    """
    created = backend.post("/api/appointments", {
        "house_no": params["house_no"],
        "viewing_time": params["viewing_time"],
        "replaces": params["appointment_id"],
    }, token=token)
    return ("已经改到 %s，原来那条已取消。新的一条要等房东确认。"
            % params["viewing_time"], created)


def _propose_book_viewing(text, token, backend, context):
    house_no = context.get("house_no") or ""
    if not house_no:
        # Prefer the houses shown by the immediately preceding search.  Falling
        # back to favorites/history here can otherwise make a new, unrelated
        # house suddenly appear after the user says only "预约看房".
        options = []
        for no in (context.get("house_nos") or [])[:5]:
            if not no or any(o["value"] == no for o in options):
                continue
            title = ""
            try:
                row = backend.get("/api/houses/%s" % no)
                title = (row.get("title") or "")[:18]
            except Exception:
                pass
            options.append(_option(no, "%s %s" % (no, title)))
        if options:
            question = "想看哪一套？"
            return (NEED_SLOT, question, None,
                    _ask("house_no", question, options))

        # 没有指定房源时，从收藏与浏览足迹里挑几套给用户点。
        #
        # 原先直接回一句「在房源详情页里跟我说」，等于把用户赶出对话框——
        # 他本来就是不想翻页面才来问的。收藏和最近看过的，
        # 多半就是他想约的那几套。
        options = []
        for path in ("/api/favorites", "/api/history"):
            for row in _rows(backend.get(path, token=token)):
                no = row.get("house_no")
                if no and not any(o["value"] == no for o in options):
                    options.append(_option(
                        no, "%s %s" % (no, (row.get("title") or "")[:18])))
            if len(options) >= 5:
                break
        question = ("想看哪一套？" if options
                    else "想看哪一套？先去房源列表挑一套，或者直接告诉我房源编号。")
        return NEED_SLOT, question, None, _ask("house_no", question, options[:5])

    # `parse_datetime` 返回的是 dict 而不是 datetime——它要同时表达
    # 「明确的某一天某一刻」和「最近三天都行」这类模糊范围，
    # 一个 datetime 装不下。这里只接受明确到时刻的表达。
    parsed = parse_datetime(context.get("viewing_time") or text) or {}
    day, clock = parsed.get("date"), parsed.get("time")
    if not day:
        return (NEED_SLOT, "你想什么时候去看？比如「这周六下午两点」。", None,
                _ask("viewing_time", "你想什么时候去看？", [
                    _option("明天下午三点", "明天下午三点"),
                    _option("这周六上午十点", "本周六上午十点"),
                    _option("这周六下午两点", "本周六下午两点"),
                    _option("这周日下午三点", "本周日下午三点")]))
    if not clock:
        # 只说了哪天没说几点。平台可约时段是 9:00–18:00，
        # 与其替用户猜一个，不如给几个常用时段让他点。
        stamp = day.strftime("%Y-%m-%d")
        return (NEED_SLOT,
                "%s 哪个时间点方便？平台可约时段是 9:00 到 18:00。"
                % day.strftime("%m 月 %d 日"), None,
                _ask("viewing_time", "哪个时间点方便？", [
                    _option("%s 10:00" % stamp, "上午 10:00"),
                    _option("%s 14:00" % stamp, "下午 14:00"),
                    _option("%s 16:00" % stamp, "下午 16:00")]))

    when = "%s %s" % (day.strftime("%Y-%m-%d"), clock.strftime("%H:%M"))
    return CONFIRM, "", _card(
        "book_viewing", "预约看房",
        "预约 %s 看房源 %s" % (when, house_no), RISK_MEDIUM,
        {"house_no": house_no, "viewing_time": when},
        detail={"house_no": house_no, "viewing_time": when},
        warning="提交后要等房东确认，结果会通过站内消息通知你。"), None


# 用户点「跳过」时填进槽里的值。用一个不可能与图片地址混淆的串，
# 免得把「跳过」两个字当成图片地址发给后端。
SKIP_UPLOAD = "__skip__"


def _image_urls(value):
    """把槽位里的值整理成图片地址列表。跳过、空值都返回空列表。"""
    if not value or value == SKIP_UPLOAD:
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = str(value).split(",")
    # 只收本系统上传的地址。后端也会再校验一次（uploads.is_managed），
    # 这里先过一道，免得把一个外链原样发过去再被拒。
    return [u.strip() for u in items
            if u.strip().startswith(("/static/", "static/"))]


def _propose_submit_repair(text, token, backend, context):
    """报修：先定「坏了什么」，再定「哪一套」，再问照片、出卡片。"""
    context = context or {}

    # 描述优先用已经存下来的那份。第一轮从这句话里推，之后原样取——
    # 否则追问照片时会拿照片答案覆盖掉故障描述。
    saved = context.get("content")
    if saved and str(saved).strip():
        content = str(saved).strip()
    else:
        # 把祈使语摘掉，只留故障描述本身。
        # 不摘的话工单内容会变成「厨房水管漏水了帮我报修」——多出来的三个字
        # 会跟着进 AI-04 的故障分类，也会显示在房东的工单列表里。
        content = (text or "").strip()
        # 长的在前：「需要报修」整个摘掉，不然只摘掉「报修」剩下半截「需要」——
        # 实测复述成了「……踢脚线底部翘起脱落，需要。」
        for word in ("需要报修处理", "需要报修", "需要维修", "需报修", "要报修",
                     "帮我报修", "帮我报个修", "帮我叫维修", "帮我叫师傅",
                     "提交报修", "报一下修", "帮我", "给我", "请", "报修",
                     "报个修", "叫维修", "叫师傅"):
            content = content.replace(word, "")
        content = content.strip("，。,. ")
        if len(content) < 4:
            # 追问必须带 ask，否则用户答的那句「厨房水管漏水」会被当成一次
            # 全新的提问，落到知识检索上——助手问了个自己听不懂的问题。
            question = "哪里出问题了？描述一下，比如「厨房水管漏水」。"
            return NEED_SLOT, question, None, _ask("content", question, [
                _option("厨房水管漏水", "厨房水管漏水"),
                _option("卫生间马桶堵了", "卫生间马桶堵了"),
                _option("空调不制冷", "空调不制冷"),
                _option("跳闸停电了", "跳闸停电了")])

    house_no = context.get("house_no") or ""
    if not house_no:
        # 报修的对象一般是自己正在租的房子，从生效租约里取（只看我作为租客的）
        rows = _rows(backend.get("/api/contracts/mine", params={"side": "tenant"},
                                 token=token))
        active = [c for c in rows if c.get("status") in ("active", "生效中")]
        if not active:
            return (NOT_APPLICABLE,
                    "你名下没有生效中的租约，报修要先有在租的房子。", None)
        house_no, asking = _repair_house(active, text, content)
        if asking:
            return asking

    return _repair_after_content(content, house_no, context)


def _repair_house(active, text, content):
    """在租的不止一套时，问是哪一套坏了。返回 ``(房源编号, 追问)``。

    原先一律取第一份生效租约。实测租客在租 3 套，说「卫生间天花板滴水」，
    工单直接挂到了列表里的第一套——房东收到一张不是自己那套的报修，
    真正漏水的那套没人管。续租、退租早就会问（`_pick_contract`），报修没有。
    """
    from app.memory import house_no_in

    if len(active) == 1:
        return active[0].get("house_no") or "", None
    typed = house_no_in(text)
    named = [r for r in active if typed and r.get("house_no") == typed]
    if len(named) == 1:
        return typed, None
    options = [_option(r.get("house_no"), "%s %s" % (r.get("house_no") or "",
                                                     r.get("house_title") or ""))
               for r in active[:5]]
    question = "收到，%s。你在租的有 %d 套，是哪一套出的问题？" % (_brief(content), len(active))
    return "", (NEED_SLOT, question, None,
                _ask("house_no", question, options, carry={"content": content}))


def _repair_after_content(content, house_no, context):
    """描述已经有了，接着问照片、出卡片。"""
    # ---- 照片是可选的一步，但值得专门问一句 ----
    #
    # 「哪里坏了」用文字往往说不清：「厨房水管漏水」到底是接头渗水
    # 还是管子裂了，师傅带的工具完全不一样。一张照片能省掉一轮上门。
    # 所以问，但必须能一键跳过——为了传张图卡住报修，是本末倒置。
    images = (context or {}).get("images")
    if images is None:
        # **先复述一遍听到的是什么，再问下一步。**
        #
        # 只问「要拍张照片吗」的话，用户刚说完「空调不制冷」，
        # 拿到的第一句是一个不相干的问题——他不知道系统有没有听懂，
        # 也不知道这张照片是要拍什么。复述一句是最便宜的确认：
        # 听错了他当场就能纠正，而不是等卡片出来才发现报错了东西。
        question = "收到，%s。要拍张照片吗？说不清的地方拍一张，师傅能提前备好工具。" % _brief(content)
        return NEED_SLOT, question, None, _ask(
            "images", question,
            [_option(SKIP_UPLOAD, "跳过，直接提交")], kind="upload",
            carry={"content": content, "house_no": house_no})

    urls = _image_urls(images)
    summary = "为 %s 提交报修：%s" % (house_no, content[:40])
    if urls:
        summary += "（附 %d 张照片）" % len(urls)

    return CONFIRM, "", _card(
        "submit_repair", "提交报修", summary, RISK_MEDIUM,
        {"house_no": house_no, "content": content,
         "images": ",".join(urls)},
        detail={"house_no": house_no, "content": content, "images": urls},
        warning="提交后系统会自动判断故障类型与紧急程度并派单。")


def _pick_contract(active, text, context, verb):
    """几份生效中的租约里，这次说的是哪一份。返回 ``(租约, 追问)``，二者有一个是 None。

    原先一律取第一份。实测租客有 3 份在租，说「我的房子快到期了，想续租」，
    卡片上是一份租到明年九月的——**离到期最远的那份**。续租、退租都是
    高风险动作，卡片摆错了对象，用户稍不留神就点了确认。
    说了编号（合同号或房源号）就按编号认；只有一份就是它；否则问。
    """
    from app.memory import house_no_in

    context = context or {}
    picked = str(context.get("contract_no") or "")
    said = house_no_in(text) or str(context.get("house_no") or "").upper()
    for row in active:
        if picked and row.get("contract_no") == picked:
            return row, None
        if row.get("contract_no") and row["contract_no"] in (text or ""):
            return row, None
    by_house = [r for r in active if said and r.get("house_no") == said]
    if len(by_house) == 1:
        return by_house[0], None
    typed = house_no_in(text)
    # 点名了一套，却不是他在租的——别悄悄换成另一份，问清楚
    if len(active) == 1 and not (typed and not by_house):
        return active[0], None
    ordered = sorted(active, key=lambda r: r.get("end_date") or "9")
    options = [_option(r.get("contract_no"), "%s 租到 %s" % (
        r.get("house_no") or r.get("contract_no"), r.get("end_date") or ""))
        for r in ordered[:5]]
    question = "你有 %d 份生效中的租约，要%s哪一份？" % (len(active), verb)
    if typed and not by_house:
        question = "%s 不在你生效中的租约里。%s" % (typed, question)
    return None, (NEED_SLOT, question, None, _ask("contract_no", question, options))


def _propose_renew_contract(text, token, backend, context):
    rows = _rows(backend.get("/api/contracts/mine", token=token))
    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    if not active:
        return NOT_APPLICABLE, "你名下没有生效中的租约。", None, None

    target, asking = _pick_contract(active, text, context, "续")
    if asking:
        return asking
    months = 12
    for candidate in (24, 18, 12, 6, 3, 1):
        if "%d个月" % candidate in text or "%d 个月" % candidate in text:
            months = candidate
            break
    if "一年" in text:
        months = 12
    elif "半年" in text:
        months = 6

    return CONFIRM, "", _card(
        "renew_contract", "续租",
        "把租约 %s 续 %d 个月" % (target.get("contract_no", ""), months),
        RISK_HIGH,
        {"contract_no": target.get("contract_no"), "months": months},
        detail={"contract_no": target.get("contract_no"), "months": months,
                "end_date": target.get("end_date")},
        warning="续租要房东确认。确认后租期在原合同上延长，按原租金排好新增的账单。")


def _propose_end_contract(text, token, backend, context):
    # 只看我作为租客的租约（同 pay_bill）。不带 side 时房东账号会拿到他出租的那些，
    # 「我要退租」就成了替房东把租客退掉
    rows = _rows(backend.get("/api/contracts/mine", params={"side": "tenant"},
                             token=token))
    active = [c for c in rows if c.get("status") in ("active", "生效中")]
    if not active:
        return NOT_APPLICABLE, "你名下没有生效中的租约。", None, None

    target, asking = _pick_contract(active, text, context, "退")
    if asking:
        return asking
    request = target.get("request") or {}
    if request.get("type") == "end" and request.get("by") == "landlord":
        # 房东已经提了提前退租：这时租客点确认就是**同意**，合同当场结束，
        # 押金按房东写明的扣款结算——卡片要把这两件事说在前面（2026-10-22）
        deduct = float(request.get("deduct") or 0)
        return CONFIRM, "", _card(
            "end_contract", "同意退租",
            "同意房东对租约 %s 的提前退租申请" % target.get("contract_no", ""), RISK_HIGH,
            {"contract_no": target.get("contract_no"), "reason": "租客同意房东的提前退租申请"},
            detail=target,
            warning="房东申请提前退租%s，押金拟扣 %s 元。确认即同意，合同立即结束，"
                    "未到期账单作废、已付未住的租金退回。不同意的话，到「我的租约」里点「不同意」。"
                    % ("（%s）" % request["reason"] if request.get("reason") else "",
                       _money(deduct)))
    return CONFIRM, "", _card(
        "end_contract", "退租",
        "申请租约 %s 退租" % target.get("contract_no", ""), RISK_HIGH,
        {"contract_no": target.get("contract_no"), "reason": "租客通过智能客服发起"},
        detail=target,
        warning="退租要房东确认，确认前合同照常有效。提前解约需提前 30 日通知并支付"
                "一个月租金的违约金；确认后未到期账单作废，押金在结清后 7 个工作日内退还。")


def _house_options(rows, limit=5, label=None):
    """把一组房源摆成可点选项，按房源编号去重。"""
    options = []
    for row in rows:
        no = row.get("house_no")
        if not no or any(o["value"] == no for o in options):
            continue
        text = label(row) if label else "%s %s" % (
            no, (row.get("title") or row.get("community") or "")[:18])
        options.append(_option(no, text))
        if len(options) >= limit:
            break
    return options


def _propose_favorite(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        # 「在房源详情页里跟我说」是把用户赶出对话框——他本来就是
        # 不想翻页面才来问的。最近看过的那几套，多半就是他想收藏的。
        favorited = {row.get("house_no")
                     for row in _rows(backend.get("/api/favorites", token=token))}
        options = _house_options(
            [row for row in _rows(backend.get("/api/history", token=token))
             if row.get("house_no") not in favorited])
        question = ("要收藏哪一套？" if options
                    else "要收藏哪一套？告诉我房源编号，或者在房源详情页里跟我说。")
        return NEED_SLOT, question, None, _ask("house_no", question, options)

    return CONFIRM, "", _card(
        "favorite_house", "收藏房源",
        "把房源 %s 加入收藏" % house_no, RISK_LOW, {"house_no": house_no})


def _propose_unfavorite(text, token, backend, context):
    house_no = (context or {}).get("house_no") or ""
    if not house_no:
        rows = _rows(backend.get("/api/favorites", token=token))
        if not rows:
            return NOT_APPLICABLE, "你还没有收藏任何房源。", None, None
        options = _house_options(rows)
        question = "要取消收藏哪一套？"
        return NEED_SLOT, question, None, _ask("house_no", question, options)

    return CONFIRM, "", _card(
        "unfavorite_house", "取消收藏",
        "把房源 %s 移出收藏" % house_no, RISK_LOW, {"house_no": house_no})


def _propose_read_all(text, token, backend, context):
    data = backend.get("/api/messages", token=token)
    unread = data.get("unread", 0) if isinstance(data, dict) else 0
    if not unread:
        return NOT_APPLICABLE, "你没有未读消息。", None, None
    return CONFIRM, "", _card(
        "read_all_messages", "消息全部已读",
        "把 %d 条未读消息标记为已读" % unread, RISK_LOW, {})


def _propose_clear_history(text, token, backend, context):
    return CONFIRM, "", _card(
        "clear_history", "清空浏览足迹",
        "清空你的全部浏览记录", RISK_MEDIUM, {},
        warning="清空后无法恢复，智能推荐也会因为缺少足迹而变得不准。")


def _propose_toggle_house(text, token, backend, context):
    context = context or {}
    # **上架还是下架，要在第一句话里就定下来，并带到下一轮。**
    # 原先每轮都从「当前这句话」里找「下架」：先问了「要操作哪一套？」，
    # 用户答「DL0029」，这句话里没有「下架」——卡片就成了**上架**。
    status = context.get("status") or ("offline" if "下架" in (text or "")
                                       else "available")
    offline = status == "offline"
    rows = _rows(backend.get("/api/landlord/houses", token=token))
    mine = {r.get("house_no") for r in rows}
    # 上游给的 house_no 可能是他刚在找房那边看过的别人的房，不是他的就不认
    house_no = context.get("house_no") or ""
    if house_no not in mine:
        house_no = ""
    if not house_no:
        if not rows:
            return NOT_APPLICABLE, "你名下还没有房源。", None, None
        if len(rows) > 1:
            # 要下架就只列在租的，要上架就只列已下架的——把不可能的选项
            # 摆出来，用户点了才发现做不成，等于让他白点一次。
            candidates = [r for r in rows
                          if (r.get("status") == "available") == offline]
            options = _house_options(candidates or rows, label=lambda r:
                                     "%s %s" % (r.get("house_no"),
                                                (r.get("title") or "")[:16]))
            question = "你名下有 %d 套房源，要%s哪一套？" % (
                len(rows), "下架" if offline else "上架")
            return NEED_SLOT, question, None, _ask("house_no", question,
                                                   options,
                                                   carry={"status": status})
        house_no = rows[0].get("house_no")

    return CONFIRM, "", _card(
        "toggle_house", "房源上下架",
        "把房源 %s %s" % (house_no, "下架" if offline else "上架"),
        RISK_MEDIUM, {"house_no": house_no, "status": status},
        warning="下架后房源不再对外展示，已有的预约不受影响。"
                if offline else "")


def _propose_remind_tenant(text, token, backend, context):
    """房东催欠租的租客。**只列真的有逾期账单的人**——没逾期的催了是骚扰。"""
    try:
        rows = _rows(backend.get("/api/landlord/tenants", token=token))
    except BackendError:
        # 租客账号调这个接口是 403。「提醒我交房租」这种话会走到这里
        return (NOT_APPLICABLE,
                "催缴提醒是房东发给租客的。你要交房租的话，说「帮我交房租」就行。",
                None, None)
    owing = [r for r in rows if (r.get("overdue_count") or 0) > 0]
    if not owing:
        return NOT_APPLICABLE, "你的租客都没有逾期账单，不用催。", None, None

    picked = str((context or {}).get("tenant_id") or "")
    target = next((r for r in owing if str(r.get("id")) == picked), None)
    if target is None:
        named = [r for r in owing if r.get("username") and r["username"] in (text or "")]
        if len(named) == 1:
            target = named[0]
    if target is None and len(owing) > 1:
        options = [_option(str(r.get("id")), "%s 逾期 %d 笔，欠 %s 元" % (
            r.get("username") or r.get("real_name") or "",
            r.get("overdue_count") or 0, _money(r.get("unpaid_total") or 0)))
            for r in owing[:5]]
        question = "有 %d 位租客有逾期账单，要提醒哪一位？" % len(owing)
        return NEED_SLOT, question, None, _ask("tenant_id", question, options)
    target = target or owing[0]
    who = target.get("username") or target.get("real_name") or ""
    return CONFIRM, "", _card(
        "remind_tenant", "催缴提醒",
        "给 %s 发一条催缴提醒（逾期 %d 笔）" % (who, target.get("overdue_count") or 0),
        RISK_MEDIUM, {"tenant_id": target.get("id")},
        detail={"tenant": who, "overdue_count": target.get("overdue_count"),
                "unpaid_total": target.get("unpaid_total")},
        warning="租客会收到一条站内消息，列出他的逾期笔数和金额。同一位租客一天只能催一次。")


def _propose_submit_review(text, token, backend, context):
    rows = _rows(backend.get("/api/reviews/pending", token=token))
    if not rows:
        return NOT_APPLICABLE, "你没有待评价的租约。", None, None
    picked = (context or {}).get("contract_no") or ""
    target = next((r for r in rows if r.get("contract_no") == picked), None)
    if target is None and len(rows) > 1:
        # 把选项摆成可点的，而不是拼进一句话里让用户照着打字。
        # 追问不带 ask，下一轮就接不住用户的回答——助手问了个自己听不懂的问题。
        options = [_option(r.get("contract_no", ""),
                           "%s %s" % (r.get("house_no") or "",
                                      r.get("contract_no", "")))
                   for r in rows[:5]]
        question = "你有 %d 份租约待评价，要评哪一份？" % len(rows)
        return NEED_SLOT, question, None, _ask("contract_no", question, options)

    target = target or rows[0]
    rating = 5
    for score in (5, 4, 3, 2, 1):
        if "%d星" % score in text or "%d 星" % score in text or "%d分" % score in text:
            rating = score
            break
    if "好评" in text:
        rating = 5
    elif "差评" in text:
        rating = 1

    return CONFIRM, "", _card(
        "submit_review", "提交评价",
        "给租约 %s 打 %d 星" % (target.get("contract_no", ""), rating),
        RISK_MEDIUM,
        {"contract_no": target.get("contract_no"), "rating": rating,
         "content": text[:200]},
        detail={"contract_no": target.get("contract_no"), "rating": rating},
        warning="评价提交后不能修改，房东会看到。")


def _propose_handle_appointment(text, token, backend, context):
    """房东确认或拒绝看房预约。

    走 `/mine` 而不是 `/api/appointments`。后者是**按房源查已占用时段**的
    公开接口，带上 token 也不会按人过滤——原来这里拿回来的是全平台的
    待处理预约，于是房东会被问「你要处理哪一条」，选项里列着别人的预约；
    真去处理还会 403。`/mine` 才是「与我有关的」，房东、租客、
    受托中介各取各的那一份。
    """
    # 只要「等我确认的」：同一个账号自己约别人房子的那些，他确认不了（后端 403）
    rows = _rows(backend.get("/api/appointments/mine", params={"side": "landlord"},
                             token=token))
    pending = [r for r in rows if r.get("status") in ("pending", "待审核")]
    if not pending:
        return NOT_APPLICABLE, "没有待处理的看房预约。", None, None

    reject = "拒绝" in text or "驳回" in text
    picked = str((context or {}).get("appointment_id") or "")
    target = next((r for r in pending if str(r.get("id")) == picked), None)
    if target is None and len(pending) > 1:
        options = [_option(str(r.get("id")),
                           "%s %s" % ((r.get("viewing_time") or "")[:16],
                                      r.get("house_no", "")))
                   for r in pending[:5]]
        # 第一句话就说清楚了的（「同意最早那个」「同意 DL0057 那个」），
        # 别再反问一遍「要处理哪一条」——实测房东刚说完「最早那个」就被问了。
        said = _pick_from_request(text, options)
        target = next((r for r in pending if str(r.get("id")) == said), None)
    if target is None and len(pending) > 1:
        question = "有 %d 条待处理预约，要处理哪一条？" % len(pending)
        return NEED_SLOT, question, None, _ask("appointment_id", question,
                                               options)

    target = target or pending[0]
    status = "rejected" if reject else "confirmed"
    return CONFIRM, "", _card(
        "handle_appointment", "处理看房预约",
        "%s %s 对 %s 的看房预约"
        % ("拒绝" if reject else "确认", target.get("viewing_time", ""),
           target.get("house_no", "")),
        RISK_MEDIUM,
        {"appointment_id": target.get("id"), "status": status},
        detail=target,
        warning="结果会通过站内消息通知租客。")


def _propose_update_repair(text, token, backend, context):
    """房东把工单标记为已完成。"""
    # 我要处理的工单，不含我自己作为租客报的（那些我处理不了）
    rows = _rows(backend.get("/api/repairs", params={"side": "landlord"}, token=token))
    open_rows = [r for r in rows
                 if r.get("status") not in ("done", "已完成", "closed")]
    if not open_rows:
        return NOT_APPLICABLE, "没有处理中的工单。", None, None
    picked = str((context or {}).get("repair_id") or "")
    target = next((r for r in open_rows if str(r.get("id")) == picked), None)
    if target is None and len(open_rows) > 1:
        options = [_option(str(r.get("id")),
                           "#%s %s" % (r.get("id"),
                                       (r.get("content") or "")[:14]))
                   for r in open_rows[:5]]
        question = "有 %d 条工单在处理中，要关闭哪一条？" % len(open_rows)
        return NEED_SLOT, question, None, _ask("repair_id", question, options)

    target = target or open_rows[0]
    return CONFIRM, "", _card(
        "update_repair", "关闭工单",
        "把工单「%s」标记为已完成" % (target.get("content") or "")[:24],
        RISK_MEDIUM,
        {"repair_id": target.get("id"), "status": "done"},
        detail=target)


def _propose_activate_contract(text, token, backend, context):
    """租客确认签约。**卡片上必须把要签的东西写全**——

    这是整套系统里金额最大、后果最长的一次点击：确认之后合同生效，
    整个租期的账单一次性排出来。摘要写得含糊等于骗签字。
    """
    # 确认签约是租客的动作：房东账号会拿到他自己发起、等对方确认的那些（确认会 403）
    rows = _rows(backend.get("/api/contracts/mine", params={"side": "tenant"},
                             token=token))
    waiting = [c for c in rows
               if c.get("status") in ("pending", "待生效", "待确认")]
    if not waiting:
        return NOT_APPLICABLE, "你名下没有待确认的合同。", None, None

    picked = (context or {}).get("contract_no") or ""
    target = next((c for c in waiting if c.get("contract_no") == picked), None)
    if target is None and len(waiting) > 1:
        options = [_option(c.get("contract_no"), "%s %s" % (
            c.get("house_no") or "", c.get("contract_no")))
            for c in waiting[:5]]
        question = "你有 %d 份合同待确认，是哪一份？" % len(waiting)
        return NEED_SLOT, question, None, _ask("contract_no", question, options)

    target = target or waiting[0]
    # 字段名是 rent 不是 rent_price——房源用 rent_price，合同用 rent。
    # 写错的表现很隐蔽：卡片照样出得来，只是**金额那一项悄悄消失了**，
    # 而这是全系统金额最大的一次确认。
    parts = ["房源 %s" % (target.get("house_no") or "")]
    if target.get("start_date") and target.get("end_date"):
        parts.append("租期 %s 至 %s" % (target["start_date"], target["end_date"]))
    rent = target.get("rent") or target.get("rent_price")
    if rent:
        parts.append("月租 %s 元" % _money(rent))
    if target.get("deposit"):
        parts.append("押金 %s 元" % _money(target["deposit"]))

    return CONFIRM, "", _card(
        "activate_contract", "确认签约",
        "确认合同 %s：%s" % (target.get("contract_no"), "，".join(parts)),
        RISK_HIGH,
        {"contract_no": target.get("contract_no")},
        detail=target,
        warning="确认后合同立即生效，整个租期的账单会一次性生成，不可撤销。")


def _propose_verify_request(text, token, backend, context):
    """房东给自己的房源提交核验申请。"""
    rows = _rows(backend.get("/api/landlord/houses", token=token))
    if not rows:
        return NOT_APPLICABLE, "你名下还没有房源。", None, None

    # 已经在核验中或已通过的就别再报一次了
    pool = [r for r in rows
            if (r.get("verify_status") or "none") in ("none", "", "rejected")]
    if not pool:
        return NOT_APPLICABLE, "你的房源都已经提交过核验了。", None, None

    picked = (context or {}).get("house_no") or ""
    target = next((r for r in pool if r.get("house_no") == picked), None)
    if target is None and len(pool) > 1:
        options = _house_options(pool)
        question = "有 %d 套房源还没核验，先提交哪一套？" % len(pool)
        return NEED_SLOT, question, None, _ask("house_no", question, options)

    target = target or pool[0]
    return CONFIRM, "", _card(
        "verify_request", "申请核验",
        "为房源 %s 提交核验申请" % target.get("house_no"), RISK_LOW,
        {"house_no": target.get("house_no")},
        detail=target,
        warning="管理员核验通过后，房源会带上「已核验」标识。")


# ------------------------------------------------------------- 长表单

def _role_gate(name, token, backend):
    """身份不够就返回一句解释，够就返回空串。

    取不到身份时**放行**：后端每个写接口自己也校验，那一层才是真正的
    安全边界；这里只是为了别让人白填十一项。因为一次查询失败就拒绝服务，
    才是更常见的错误。
    """
    roles = forms.allowed_roles(name)
    if not roles:
        return ""
    try:
        me = backend.get("/api/auth/me", token=token)
    except Exception:
        return ""
    role = (me or {}).get("role") or ""
    if not role or role in roles:
        return ""
    return forms.denied_message(name)


def _form_context(name, token, backend, slots):
    """表单需要现查的东西。写死在表里会过期，所以每次问之前拉一遍。"""
    context = dict(slots or {})
    form = forms.spec(name)
    if any(f.get("source") == "landlord_houses" for f in form["fields"]):
        context["_houses"] = _rows(
            backend.get("/api/landlord/houses", token=token))
    return context


# 「把 DL0023 的租金改成 2300」一句话里三项都齐了。原先表单只看上下文，
# 照样一项项问「改哪一套？」「改哪一项？」——用户刚说过的话被当没听见。
_EDIT_FIELDS = [("rent_price", ("月租", "租金", "房租")), ("deposit", ("押金",)),
                ("title", ("标题",)), ("address", ("地址",)),
                ("description", ("描述", "介绍"))]
# 动词和数字之间允许夹几个字：「降到市场价约 4800」「调到大约 4500」。
# 原先要求数字紧跟动词，实测模型写的就是「降到市场价约4800元/月」，
# 一项都没认出来，回头再问用户「改成什么」——他刚说过「降到市场价」
_EDIT_NUMBER = re.compile(
    r"(?:改成|改为|改到|调成|调为|调到|降到|涨到|设为|设成|定为|定成)"
    r"[^0-9零一二两三四五六七八九十百千万，,。；;]{0,6}?"
    r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万]+)\s*(?:块|元)?")


def _edit_house_from_text(text):
    """从请求原话里认出房源编号、改哪一项、改成多少。认不出的项不填，照常再问。

    **只认「改成 / 调到 …」后面的绝对值。**「降两百」是相对量，
    要先知道现价才能算——算错了就是按错的价格挂出去，宁可再问一句。
    文本类字段（标题、地址）也只认出是哪一项，值让用户单独说：
    从一句话里截出一段文字当新标题，截错了比多问一句麻烦得多。
    """
    from app.cn_number import cn_to_int
    from app.memory import house_no_in

    text = text or ""
    out = {}
    house_no = house_no_in(text)
    if house_no:
        out["house_no"] = house_no
    hits = [field for field, words in _EDIT_FIELDS if any(w in text for w in words)]
    if len(hits) != 1:
        return out
    out["field"] = hits[0]
    if hits[0] in ("rent_price", "deposit"):
        matched = _EDIT_NUMBER.search(text)
        if matched:
            raw = matched.group(1)
            try:
                value = float(raw) if raw[0].isdigit() else cn_to_int(raw)
            except (TypeError, ValueError):
                value = None
            if value:
                out["value"] = str(int(value)) if float(value).is_integer() else str(value)
    return out


def _propose_form(name):
    """把一张声明式表单变成一个提议器：一次问一项，填完出确认卡。"""

    def proposer(text, token, backend, context):
        form = forms.spec(name)
        # **只收这张表单声明过的字段。**
        #
        # 上游会往 context 里塞 house_no（用户此刻正看着哪套房），
        # 全盘收下的话它会混进卡片参数，执行时被参数白名单拦成
        # 「操作参数不合法：house_no」——**卡片好好的，一点确认就报错**，
        # 是最难查的一类。表单自己的字段表就是唯一的口径。
        allowed = {f["name"] for f in form["fields"]}
        # None 一并滤掉：上游拿不到房源上下文时给的就是 None，
        # 留着它会被当成「填过了」（str(None) == "None"）。
        slots = {k: v for k, v in (context or {}).items()
                 if k in allowed and v is not None and str(v).strip()}
        # 模型直接给的值也要过一遍校验：它把「十二个月」「下月初」原样填进来，
        # 卡片照样出得来，点确认时才在 int()、后端那里炸。不合格的丢掉，回头再问
        for key in [k for k in slots if k != "house_no"]:
            ok, cleaned = forms.validate(name, key, str(slots[key]), slots)
            if ok:
                slots[key] = cleaned
            else:
                slots.pop(key)

        # **先问「你能不能办这件事」，再问第一项。**
        # 顺序反了的代价实测过：租客把十一项填完才收到「没有操作权限」。
        denied = _role_gate(name, token, backend)
        if denied:
            return NOT_APPLICABLE, denied, None, None

        env = _form_context(name, token, backend, slots)
        if name in ("sign_contract", "edit_house") and not env.get("_houses"):
            return NOT_APPLICABLE, "你名下还没有房源。", None, None
        # 上游塞进来的 house_no（用户刚看过的那套）不是他名下的就不认：
        # 卡片照样出得来，要到点确认时后端才拒，是最糟的一种失败
        if name in ("sign_contract", "edit_house") and slots.get("house_no")                 and slots["house_no"] not in {h.get("house_no") for h in env["_houses"]}:
            slots.pop("house_no")
        if name == "edit_house":
            parsed = _edit_house_from_text(text)
            # 编号不是他名下的就不收：卡片能出，执行时后端才拒，是最糟的一种
            mine = {h.get("house_no") for h in env["_houses"]}
            if parsed.get("house_no") not in mine:
                parsed.pop("house_no", None)
            if "value" in parsed:
                ok, cleaned = forms.validate(name, "value", parsed["value"],
                                             {**slots, **parsed})
                if ok:
                    parsed["value"] = cleaned
                else:
                    parsed.pop("value")
            for key, value in parsed.items():
                slots.setdefault(key, value)

        field = forms.next_missing(name, slots)
        if field is not None:
            ask = forms.ask_for(name, field, slots, env)
            return NEED_SLOT, ask["question"], None, ask

        return CONFIRM, "", _card(
            name, form["label"], forms.summary(name, slots), form["risk"],
            dict(slots), detail=dict(slots), warning=form.get("warning", ""))

    proposer.__name__ = "_propose_" + name
    proposer.__doc__ = "长表单「%s」。" % forms.spec(name)["label"]
    return proposer


# ------------------------------------------------------------- 账号资料

_PHONE = re.compile(r"1[3-9]\d{9}")


def _propose_update_profile(text, token, backend, context):
    """改手机号。**只开放这一项**——改密码不该走对话框。"""
    phone = (context or {}).get("phone") or ""
    if not phone:
        found = _PHONE.search(text or "")
        phone = found.group(0) if found else ""
    if not phone:
        question = "新的手机号是多少？发给我 11 位数字就行。"
        return NEED_SLOT, question, None, _ask("phone", question, [])

    if not _PHONE.fullmatch(phone.strip()):
        question = "这个号码看着不像 11 位手机号，再确认一下？"
        return NEED_SLOT, question, None, _ask("phone", question, [])

    masked = phone[:3] + "****" + phone[-4:]
    return CONFIRM, "", _card(
        "update_profile", "修改手机号",
        "把手机号改成 %s" % masked, RISK_MEDIUM,
        {"phone": phone.strip()},
        detail={"phone": masked},
        warning="房东与平台都通过这个号码联系你，改错会收不到通知。")


PROPOSERS = {
    "pay_bill": _propose_pay_bill,
    "cancel_viewing": _propose_cancel_viewing,
    "reschedule_viewing": _propose_reschedule_viewing,
    "book_viewing": _propose_book_viewing,
    "submit_repair": _propose_submit_repair,
    "renew_contract": _propose_renew_contract,
    "end_contract": _propose_end_contract,
    "favorite_house": _propose_favorite,
    "unfavorite_house": _propose_unfavorite,
    "read_all_messages": _propose_read_all,
    "clear_history": _propose_clear_history,
    "submit_review": _propose_submit_review,
    "handle_appointment": _propose_handle_appointment,
    "update_repair": _propose_update_repair,
    "toggle_house": _propose_toggle_house,
    "remind_tenant": _propose_remind_tenant,
    "activate_contract": _propose_activate_contract,
    "verify_request": _propose_verify_request,
    "publish_house": _propose_form("publish_house"),
    "sign_contract": _propose_form("sign_contract"),
    "edit_house": _propose_form("edit_house"),
    "update_profile": _propose_update_profile,
}


def propose(name, text, token, backend, context=None):
    """生成确认卡片。**保证不产生任何副作用**——只读后端，不写。

    返回 ``(status, message, card, ask)``：

    - `card` 是确认卡片，只有 `status == CONFIRM` 时非空
    - `ask` 是一次追问，只有 `status == NEED_SLOT` 时非空，
      形如 ``{"slot", "question", "options"}``。`options` 让前端渲染成
      可点按钮——用户点一下就等于把值填进槽位，不用猜该怎么打字。

    `context` 里既有页面上下文（当前打开的房源），也有前几轮已经填好的槽位，
    两者合流。对 proposer 来说「这个值哪来的」不重要，有就行。

    各个 proposer 返回三元组或四元组都可以，这里统一补齐——
    多数操作不需要追问，让它们为此多写一个 `None` 是噪声。
    """
    if not token:
        return NEED_LOGIN, "这个操作要用你的账号来做，先登录一下。", None, None
    proposer = PROPOSERS.get(name)
    if proposer is None:
        return NOT_APPLICABLE, "", None, None
    try:
        result = proposer(text, token, backend, context or {})
    except BackendError:
        return FAILED, "查你的数据时没连上后端，稍后再试。", None, None
    except Exception:
        return FAILED, "准备这个操作时出了点问题，稍后再试。", None, None

    return tuple(result) + (None,) if len(result) == 3 else tuple(result)



# ---------------------------------------------------------------- 执行

def _exec_pay_bill(params, token, backend):
    data = backend.post("/api/bills/%s/pay" % params["bill_no"],
                        {"method": params.get("method", "alipay")}, token=token)
    return "缴费成功。", data


def _exec_cancel_viewing(params, token, backend):
    data = backend._request(
        "PATCH", "/api/appointments/%s" % params["appointment_id"],
        payload={"status": "cancelled"}, token=token)
    return "预约已取消，该时段重新释放。", data


def _exec_book_viewing(params, token, backend):
    data = backend.post("/api/appointments", {
        "house_no": params["house_no"],
        "viewing_time": params["viewing_time"],
    }, token=token)
    return "预约已提交，等房东确认，结果会通过站内消息通知你。", data


def _exec_submit_repair(params, token, backend):
    data = backend.post("/api/repairs", {
        "house_no": params["house_no"],
        "content": params["content"],
    }, token=token)

    # 照片挂在工单建好之后——后端要工单 id 才收得下。
    # **挂图失败不能让整单失败**：工单已经建出来了，这时候报错会让用户
    # 以为报修没成，再报一遍就是两条重复工单。
    urls = _image_urls(params.get("images"))
    repair_id = ((data or {}).get("repair") or {}).get("id")
    attached = 0
    if repair_id and urls:
        for url in urls[:6]:
            try:
                backend.post("/api/repairs/%s/images" % repair_id,
                             {"url": url}, token=token)
                attached += 1
            except Exception:
                continue

    message = "报修已提交，可以在「我的租约 → 报修」里看进度。"
    if urls:
        message += ("照片也传上去了（%d 张）。" % attached if attached
                    else "照片没传上去，可以到工单里再补。")
    return message, data


def _exec_renew_contract(params, token, backend):
    data = backend.post("/api/contracts/%s/renew" % params["contract_no"],
                        {"months": params["months"]}, token=token)
    # 对方已经提过续租，这一下就是确认；否则是我方的申请，要等对方点
    if isinstance(data, dict) and data.get("renewed"):
        end = (data.get("contract") or {}).get("end_date", "")
        return "已续租，租期延长到 %s。" % end if end else "已续租。", data
    return "续租申请已提交，等对方确认后生效。", data


def _exec_end_contract(params, token, backend):
    data = backend.post("/api/contracts/%s/end" % params["contract_no"],
                        {"reason": params.get("reason", "")}, token=token)
    if isinstance(data, dict) and data.get("requested"):
        return "退租申请已提交，等房东确认。确认前合同照常有效，可以先约房东验房。", data
    return "已退租。", data


def _exec_favorite(params, token, backend):
    data = backend.post("/api/favorites/%s" % params["house_no"], {},
                        token=token)
    return "已收藏。", data


def _exec_unfavorite(params, token, backend):
    data = backend._request("DELETE", "/api/favorites/%s" % params["house_no"],
                            token=token)
    return "已取消收藏。", data


def _exec_read_all(params, token, backend):
    data = backend.post("/api/messages/read-all", {}, token=token)
    return "消息已全部标记为已读。", data


def _exec_clear_history(params, token, backend):
    data = backend._request("DELETE", "/api/history", token=token)
    return "浏览足迹已清空。", data


def _exec_toggle_house(params, token, backend):
    data = backend._request(
        "PATCH", "/api/houses/%s/status" % params["house_no"],
        params={"status": params["status"]}, token=token)
    # 没通过审核的房源，「上架」在后端会变成「提交审核」——照后端说的讲，
    # 别告诉房东「已上架」而页面上显示的是待审核
    if isinstance(data, dict) and data.get("message"):
        return data["message"], data
    return ("房源已%s。" % ("下架" if params["status"] == "offline" else "上架"),
            data)


def _exec_remind_tenant(params, token, backend):
    data = backend.post("/api/landlord/tenants/%s/remind" % params["tenant_id"],
                        {}, token=token)
    return ("已发出催缴提醒：%s 笔逾期、共 %s 元。租客那边会收到站内消息。"
            % ((data or {}).get("count", ""), _money((data or {}).get("amount") or 0)),
            data)


def _exec_submit_review(params, token, backend):
    data = backend.post("/api/reviews", {
        "contract_no": params["contract_no"],
        "rating": params["rating"],
        "content": params.get("content", ""),
    }, token=token)
    return "评价已提交，谢谢反馈。", data


def _exec_handle_appointment(params, token, backend):
    data = backend._request(
        "PATCH", "/api/appointments/%s" % params["appointment_id"],
        payload={"status": params["status"]}, token=token)
    return ("预约已%s，租客会收到站内消息。"
            % ("拒绝" if params["status"] == "rejected" else "确认"), data)


def _exec_update_repair(params, token, backend):
    data = backend._request(
        "PATCH", "/api/repairs/%s" % params["repair_id"],
        payload={"status": params.get("status", "done")}, token=token)
    return "工单已标记为完成。", data


def _exec_activate_contract(params, token, backend):
    data = backend.post("/api/contracts/%s/activate" % params["contract_no"],
                        {}, token=token)
    return "合同已生效，整个租期的账单已经排好，可以在「我的租约」里看。", data


def _exec_verify_request(params, token, backend):
    data = backend.post("/api/houses/%s/verify-request" % params["house_no"],
                        {}, token=token)
    return "核验申请已提交，等管理员处理。", data


def _exec_publish_house(params, token, backend):
    data = backend.post("/api/houses", dict(params), token=token)
    no = (data or {}).get("house_no") or (data or {}).get("house", {}).get("house_no", "")
    return "房源已提交%s，等管理员审核通过后就会对外展示。" % (
        "（编号 %s）" % no if no else ""), data


def _exec_sign_contract(params, token, backend):
    body = {
        "house_no": params["house_no"],
        "tenant_username": params["tenant_username"],
        "start_date": params["start_date"],
        "months": int(params["months"]),
    }
    if params.get("rent"):
        body["rent"] = float(params["rent"])
    data = backend.post("/api/contracts", body, token=token)
    return "合同已发起，等租客确认后生效。", data


def _exec_edit_house(params, token, backend):
    data = backend._request(
        "PUT", "/api/houses/%s" % params["house_no"],
        payload={params["field"]: params["value"]}, token=token)
    return "改好了。**房源已退回待审核**，通过后才重新对外展示。", data


def _exec_update_profile(params, token, backend):
    data = backend._request("PUT", "/api/auth/profile",
                            payload={"phone": params["phone"]}, token=token)
    return "手机号已更新。", data


EXECUTORS = {
    "pay_bill": _exec_pay_bill,
    "cancel_viewing": _exec_cancel_viewing,
    "reschedule_viewing": _exec_reschedule_viewing,
    "book_viewing": _exec_book_viewing,
    "submit_repair": _exec_submit_repair,
    "renew_contract": _exec_renew_contract,
    "end_contract": _exec_end_contract,
    "favorite_house": _exec_favorite,
    "unfavorite_house": _exec_unfavorite,
    "read_all_messages": _exec_read_all,
    "clear_history": _exec_clear_history,
    "submit_review": _exec_submit_review,
    "handle_appointment": _exec_handle_appointment,
    "update_repair": _exec_update_repair,
    "toggle_house": _exec_toggle_house,
    "remind_tenant": _exec_remind_tenant,
    "activate_contract": _exec_activate_contract,
    "verify_request": _exec_verify_request,
    "publish_house": _exec_publish_house,
    "sign_contract": _exec_sign_contract,
    "edit_house": _exec_edit_house,
    "update_profile": _exec_update_profile,
}

# 每个动作允许出现的参数名。**白名单，不是黑名单**：
# 确认卡片是从前端回传的，多一个没见过的字段就应当拒绝，
# 而不是原样透传给后端。
# 动作 -> 中文名。卡片上本来就带 label，但打断后要说「刚才我们在办
# 「发布房源」」时手上只有动作名，得能反查。
LABELS = {
    "pay_bill": "缴费", "cancel_viewing": "取消预约", "book_viewing": "预约看房",
    "reschedule_viewing": "看房改期",
    "submit_repair": "提交报修", "renew_contract": "续租", "end_contract": "退租",
    "favorite_house": "收藏房源", "unfavorite_house": "取消收藏",
    "read_all_messages": "消息全部已读", "clear_history": "清空浏览足迹",
    "submit_review": "提交评价", "handle_appointment": "处理看房预约",
    "update_repair": "关闭工单", "toggle_house": "房源上下架",
    "activate_contract": "确认签约", "verify_request": "申请核验",
    "update_profile": "修改手机号", "publish_house": "发布房源",
    "sign_contract": "发起签约", "edit_house": "修改房源",
    "remind_tenant": "催缴提醒",
}


ALLOWED_PARAMS = {
    "pay_bill": {"bill_no", "method"},
    "cancel_viewing": {"appointment_id", "status"},
    "reschedule_viewing": {"appointment_id", "house_no", "viewing_time"},
    "book_viewing": {"house_no", "viewing_time"},
    "submit_repair": {"house_no", "content", "images"},
    "renew_contract": {"contract_no", "months"},
    "end_contract": {"contract_no", "reason"},
    "favorite_house": {"house_no"},
    "unfavorite_house": {"house_no"},
    "read_all_messages": set(),
    "clear_history": set(),
    "submit_review": {"contract_no", "rating", "content"},
    "handle_appointment": {"appointment_id", "status"},
    "update_repair": {"repair_id", "status"},
    "toggle_house": {"house_no", "status"},
    "remind_tenant": {"tenant_id"},
    "activate_contract": {"contract_no"},
    "verify_request": {"house_no"},
    "publish_house": {"district", "community", "address", "layout",
                      "area", "rent_price", "deposit", "rent_type",
                      "floor", "orientation", "title"},
    "sign_contract": {"house_no", "tenant_username", "start_date",
                      "months", "rent"},
    "edit_house": {"house_no", "field", "value"},
    # **只放行 phone。** 这张白名单是这一层真正的安全边界：
    # 即使有人构造出 {"password": "..."}，也进不了后端。
    "update_profile": {"phone"},
}


def execute(name, params, token, backend):
    """执行一个**已被用户确认**的动作。返回 ``(ok, message, data)``。

    只有 `/agent/support/confirm` 这一个入口会调到这里，
    对话主流程永远走不到——这条边界是刻意的。
    """
    if not token:
        return False, "登录状态已失效，请重新登录后再试。", {}

    executor = EXECUTORS.get(name)
    if executor is None:
        return False, "不支持这个操作。", {}

    params = params or {}
    allowed = ALLOWED_PARAMS.get(name, set())
    unexpected = set(params) - allowed
    if unexpected:
        return False, "操作参数不合法：%s" % "、".join(sorted(unexpected)), {}
    missing = allowed - set(params)
    # method / reason 有默认值，缺了不影响
    missing -= {"method", "reason", "status"}
    if missing:
        return False, "操作缺少参数：%s" % "、".join(sorted(missing)), {}

    try:
        message, data = executor(params, token, backend)
    except BackendError as exc:
        # 后端的拒绝原因要如实转述：可能是账单已被支付、预约已过期、
        # 或者根本不是他的数据。含糊成「操作失败」会让用户反复重试。
        return False, "没能完成：%s" % str(exc), {}
    except Exception:
        return False, "执行时出了点问题，请到对应页面手动操作。", {}

    return True, message, data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 刻意不接的

# 以下操作**故意不做成可代办**，不是漏了：
#
# - **改密码**（`/api/auth/password`）：口令不该经过对话框，也不该出现在
#   任何一条日志里。用户在「账号设置」里自己改，一步都不多。
# - **填身份证号**（`ProfileUpdate.id_card`）：证件原文属于最敏感的一类
#   个人信息，平台本身都只存哈希与掩码（R-02），没有理由让它先在
#   聊天记录里走一遭。
# - **发布房源**（`/api/houses`）：十几个必填字段，靠一句话对话填完只会
#   反复追问，体验比表单差得多。发布页已经有 AI 定价建议与描述生成，
#   那才是这个场景该用 AI 的地方。
#
# 判断标准是同一条：**代办要让事情更省，而不是把表单改造成审问。**
# 涉及凭据与证件的，无论多省都不做。
NOT_EXPOSED = ("change_password", "set_id_card", "publish_house")
