"""业务数据模型。

表结构参考团队《项目改造清单》第 6 节，并落实《需求分析说明书》
第 3.1 节的数据安全与数据质量要求：

* 密码只存哈希，不存明文（R-01）
* 金额一律 `Numeric(10, 2)`，不用字符串或整数（R-04）
* 表间用真外键关联，不靠业务编号字符串匹配（R-03、R-06）
* 看房时间用 `DateTime` 而非 `Date`，否则表达不了「下午两点」（AI-06）

交易链三张表 `contracts` / `bills` / `payments` 是主干：
看房通过后生成合同，合同生效时一次性排出整个租期的账单，缴费产生流水。
"""

import json

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        Numeric, String, Text, UniqueConstraint, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """用户。三种角色共用一张表，用 role 区分。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    # 只存 BCrypt 哈希，任何情况下不落明文（R-01）
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    real_name: Mapped[str] = mapped_column(String(50), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(100), default="")

    # 身份证号（R-02）。**只存哈希与掩码，原文从不落库**——
    # 系统对它的全部需求就是「展示」与「查重」，两者都不需要还原，
    # 那就不该保留还原的可能。详见 app/idcard.py。
    # 令牌失效基线（B-2）。改密码、禁用账号、退出所有设备时设为当时，
    # 之后凡是签发时刻早于它的令牌一律失效。
    # 为空表示从未吊销过——绝大多数用户都是这种情况。
    token_valid_after: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    id_card_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    id_card_masked: Mapped[str] = mapped_column(String(32), default="")
    # tenant 租客 / landlord 房东 / agent 经纪人 / admin 管理员
    role: Mapped[str] = mapped_column(String(20), default="tenant", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # 免打扰（J-07）：关掉之后中介不能主动发起会话。
    # **默认开**——默认关等于功能上线当天所有联系按钮一起置灰，
    # 那是把偏好开关做成了功能开关
    accept_contact: Mapped[bool] = mapped_column(Boolean, default=True,
                                                 nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    houses: Mapped[list["House"]] = relationship(back_populates="landlord")

    def to_dict(self):
        """对外输出。**绝不包含 password_hash。**"""
        return {
            "id": self.id,
            "username": self.username,
            "real_name": self.real_name,
            "phone": mask_phone(self.phone),
            # 只给掩码。哈希是内部比对用的，没有任何接口该把它吐出去
            "id_card": self.id_card_masked,
            "role": self.role,
            "status": self.status,
            # 免打扰开关（J-07）。前端据此渲染「我的账号」里的勾选框，
            # 中介端据此决定「联系租客」按钮是不是置灰
            "accept_contact": bool(self.accept_contact),
        }


class House(Base):
    """房源。"""

    __tablename__ = "houses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    province: Mapped[str] = mapped_column(String(30), default="辽宁省")
    city: Mapped[str] = mapped_column(String(30), default="大连市")
    district: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    community: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(String(200), default="")

    # 金额统一 Numeric，不用 varchar / int（R-04）
    rent_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, index=True)
    deposit: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    area: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, index=True)

    layout: Mapped[str] = mapped_column(String(20), default="", index=True)
    rent_type: Mapped[str] = mapped_column(String(20), default="整租", index=True)
    floor: Mapped[str] = mapped_column(String(20), default="")
    orientation: Mapped[str] = mapped_column(String(20), default="")

    description: Mapped[str] = mapped_column(Text, default="")
    # 房源特征以逗号分隔存储，如「电梯,地铁,近学校」。
    #
    # **这里只留「说不清的那些」。** 凡是要拿来筛选的特征都已经拆成了
    # 下面的独立列（K-04）——用 `tags LIKE '%电梯%'` 去筛，
    # 「无电梯」会被「电梯」命中，这不是没做筛选，是做了会错。
    # 剩在 tags 里的是「近学校」「有阳台」这类描述性标签，只作展示。
    tags: Mapped[str] = mapped_column(String(200), default="")
    cover_image: Mapped[str] = mapped_column(String(255), default="")

    # ---------------------------------------------------------- K-04 筛选维度
    #
    # 每一个能被筛的概念都有自己的列。这一组字段是「高级筛选」「费用明细」
    # 「地图打点」「房源核验」四件事共同的地基——字段不落库，这四件事
    # 都做不出来，所以它们必须一次补齐、赶在数据库评审之前定稿。

    elevator: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 精装 / 简装 / 毛坯，空串表示房东没填
    decoration: Mapped[str] = mapped_column(String(20), default="", index=True)

    # 楼层拆成两个整数。原先只有 `floor` 一个字符串（如「11/33层」），
    # 表达得了「第几层」却筛不了「低/中/高楼层」——排序和区间比较
    # 都要数字。`floor` 保留作展示原文，两者不冲突。
    floor_no: Mapped[int] = mapped_column(Integer, nullable=True)
    floor_total: Mapped[int] = mapped_column(Integer, nullable=True)

    # A5：楼栋与单元。
    #
    # 原先地址里这两样是**混在 `address` 那一行自由文本里**的
    # （「中山区青泥洼桥路 12 号楼 3 单元」），而楼层又另有 `floor`
    # 一个自由文本（「2/28层」）。于是同一套房在卡片上出现两处楼层：
    # 地址行里一个「2/28层」，属性行里一个「低楼层」——
    # 看的人会以为是两套房的数据串了。
    #
    # 拆成独立字段之后：楼栋、单元、第几层、共几层各自一个格子，
    # `floor` 那个自由文本退成**由这几项拼出来的展示串**，不再手填。
    # 好处不只是好看：带看要报「几号楼几单元」，而从一行自由文本里
    # 是取不出这两个值的。
    building: Mapped[str] = mapped_column(String(20), default="")
    unit: Mapped[str] = mapped_column(String(20), default="")

    # 集中供暖 / 自采暖。大连的租房广告里这一条几乎每个租客都会问
    heating: Mapped[str] = mapped_column(String(20), default="")

    # 可入住日与最短租期。「这套房什么时候能住进去」是租房独有的问题，
    # 买房没有——空着的房子和还有三个月才到期的房子完全是两回事
    available_from: Mapped[Date] = mapped_column(Date, nullable=True, index=True)
    min_months: Mapped[int] = mapped_column(Integer, default=1)

    # 押付方式。**它不是第二个金额来源**——填了它就同时把 `deposit`
    # 按「月租 × 押金月数」写好，`deposit` 始终是押金唯一的真相（K-02）。
    # 这两列存在只为两件事：筛选「押一付一」，和在卡片上显示「押一付三」。
    # 房东如果填的是不成整数倍的押金（月租 2800、押金 800），
    # 这两列就留空，卡片直接显示金额。
    deposit_months: Mapped[int] = mapped_column(Integer, nullable=True)
    pay_months: Mapped[int] = mapped_column(Integer, nullable=True)
    # 服务费。费用明细要能把「租金 / 押金 / 服务费 / 合计」摊开
    service_fee: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    # 商圈。地址原先是「区 + 小区 + 一串自由文本」，中间缺了商圈这一层，
    # 而找房的人说的正是「青泥洼桥附近」这种商圈名
    business_area: Mapped[str] = mapped_column(String(30), default="", index=True)

    # 经纬度（GCJ-02）。K-04 落库时的用途是「在离线示意图上按位置打点」，
    # 接入高德之后它同时是真实地图的点位来源，两条路共用同一对字段。
    lng: Mapped[float] = mapped_column(Float, nullable=True, index=True)
    lat: Mapped[float] = mapped_column(Float, nullable=True, index=True)

    # 这个坐标是怎么来的。**必须记，不能只存一对数字**——
    # 地理编码出来的坐标能精确到门牌，而演示库里的门牌号是随机生成的，
    # 只能按商圈中心做稳定偏移。两者精度差一个数量级，界面上要不要
    # 画「大致位置」、要不要拿它算通勤距离，全看这个字段。
    # geocoded / approximate / manual，空串表示还没定过位。
    location_source: Mapped[str] = mapped_column(String(20), default="")
    geocoded_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # ---------------------------------------------------------- K-04 房源核验
    #
    # 《待完善清单》D-4 判断得对：不动产登记数据接口我们拿不到。
    # 但**核验的状态机和公开的核验编号是自己能做的**——把核验方从
    # 「登记接口」换成「管理员人工看证件照片」，状态机一模一样。
    # 答辩讲「流程做完整了，只是核验源受资质限制用了人工」，
    # 比讲「我们没做核验」强得多。
    #
    # unverified 未核验 / pending 待核验 / verified 已核验 / rejected 核验失败
    verify_status: Mapped[str] = mapped_column(String(20), default="unverified",
                                               index=True)
    verify_no: Mapped[str] = mapped_column(String(32), default="", index=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    verify_remark: Mapped[str] = mapped_column(String(200), default="")

    # 最近维护时间。贝壳在每张卡片上都印「1天前维护」，看着不起眼，
    # 但它解决的是租房列表里最让人恼火的事：**打电话过去房子早租出去了**。
    # 有这个时间，用户自己就能判断这条信息还值不值得信。
    maintained_at: Mapped[datetime] = mapped_column(DateTime,
                                                    default=datetime.now)

    # available 可租 / rented 已租 / offline 已下架 / pending 待审核
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 当前这版内容**通过审核的时刻**；没通过、被驳回、或改动后待重审时为空。
    #
    # 没有它的时候，「被驳回」和「房东自己下架」落成的是同一个 offline，
    # 上下架接口又不看来路——房东把驳回的房源点一下上架，就绕过了审核；
    # 待审核的也能直接改成 available。见 main.update_house_status。
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # lazy="selectin"：列表接口一次查 12 套房，若按默认的懒加载，
    # to_dict 里每读一次 landlord 就发一条 SQL——实测 12 套房发了 32 条。
    # selectin 会把这一批的关联对象用一条 IN 查询取回来。
    landlord: Mapped["User"] = relationship(back_populates="houses",
                                            lazy="selectin")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="house")
    images: Mapped[list["HouseImage"]] = relationship(
        back_populates="house", order_by="HouseImage.sort_order",
        lazy="selectin")
    reviews: Mapped[list["Review"]] = relationship(lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "house_no": self.house_no,
            "title": self.title,
            "district": self.district,
            "community": self.community,
            "address": self.address,
            "rent_price": float(self.rent_price),
            "deposit": float(self.deposit),
            "area": float(self.area),
            "layout": self.layout,
            "rent_type": self.rent_type,
            # 展示串由楼层数字拼出来；老数据没有数字时才回落到手填原文
            "floor": self.floor_text,
            "orientation": self.orientation,
            "description": self.description,
            "tags": [t for t in self.tags.split(",") if t],
            "cover_image": self.cover_image,
            "images": [i.url for i in sorted(
                self.images, key=lambda x: (not x.is_cover, x.sort_order))],
            "status": self.status,
            "view_count": self.view_count,
            # 上架日期。房东问「哪几套挂了很久没租出去」，助手原先答
            # 「查不到上架时间，没法判断」——库里一直有，只是没给出去
            "published_at": (self.published_at.strftime("%Y-%m-%d")
                             if self.published_at else ""),
            "landlord_name": self.landlord.real_name if self.landlord else "",
            "rating": self.avg_rating,
            "review_count": len([r for r in self.reviews
                                 if not r.deleted_at]),

            # ---- K-04 新增维度。前端筛选栏、费用明细、核验条都读这些 ----
            "elevator": bool(self.elevator),
            "decoration": self.decoration,
            "floor_no": self.floor_no,
            "floor_total": self.floor_total,
            "building": self.building or "",
            "unit": self.unit or "",
            "address_full": self.address_full,
            "floor_level": self.floor_level,
            "heating": self.heating,
            "available_from": (self.available_from.strftime("%Y-%m-%d")
                               if self.available_from else ""),
            "min_months": self.min_months,
            "deposit_months": self.deposit_months,
            "pay_months": self.pay_months,
            "pay_plan": self.pay_plan,
            "service_fee": float(self.service_fee or 0),
            "fee_detail": self.fee_detail,
            "verify_remark": self.verify_remark,
            "business_area": self.business_area,
            "lng": self.lng,
            "lat": self.lat,
            "location_source": self.location_source,
            "verify_status": self.verify_status,
            "verify_no": self.verify_no,
            "verified_at": (self.verified_at.strftime("%Y-%m-%d")
                            if self.verified_at else ""),
            "maintained_at": (self.maintained_at.strftime("%Y-%m-%d")
                              if self.maintained_at else ""),
        }

    # 列表卡片需要的字段。**列表不该返回详情页才用得上的东西。**
    #
    # `to_dict()` 是详情页的完整载荷，K-04/K-07 往里加了二十来个字段之后
    # 单套房的 JSON 长到 1.4KB，一页 12 套就是 15KB。列表接口在 100 并发
    # 下是全站最热的一条路径，这些多出来的字节每个请求都要序列化一遍，
    # 而卡片上一个都用不到——房源描述、整个相册数组、费用明细拆解，
    # 全是点进详情才看的。
    #
    # 这里列的是 `HouseCard` 与 `CompareBar` 真正读到的键，
    # 外加筛选相关的几个标量（测试要拿它们核对筛选结果对不对）。
    CARD_FIELDS = (
        "id", "house_no", "title", "district", "community", "address",
        "rent_price", "deposit", "area", "layout", "rent_type",
        "floor", "orientation", "tags", "cover_image", "status",
        "rating", "review_count",
        "elevator", "decoration", "floor_level", "pay_plan",
        "verify_status", "maintained_at",
    )

    # 地图点位需要的字段。比卡片还要窄——地图上一次要画几百个点，
    # 而气泡里只放得下标题、价格、户型这几样。
    MAP_FIELDS = ("house_no", "title", "district", "community", "address",
                  "rent_price", "area", "layout", "rent_type", "cover_image",
                  "lng", "lat", "location_source")

    def to_map_dict(self):
        """地图点位载荷，同样是 `to_dict()` 的子集。

        和 `to_card_dict()` 一个道理：**不另写一份序列化**，
        否则同一套房在列表、地图、详情三处会慢慢说出三种话。
        """
        full = self.to_dict()
        return {key: full[key] for key in self.MAP_FIELDS if key in full}

    def to_card_dict(self):
        """列表卡片用的精简载荷，`to_dict()` 的子集。

        刻意做成「从完整字典里挑」而不是另写一份：两份独立的序列化
        必然会分叉——加了字段只改一处，列表和详情从此对同一套房
        给出不一样的信息。挑字段的代价是仍然算了一遍完整字典，
        但那部分实测只有 0.2ms/12 套，真正贵的是把它编码成 JSON 发出去。
        """
        full = self.to_dict()
        return {key: full[key] for key in self.CARD_FIELDS if key in full}

    @property
    def address_full(self):
        """带楼栋单元的完整地址。**只在详情页与合同里用**。

        列表卡片上不显示——那里已经有区域和小区，再加「12号楼3单元」
        既占地方又帮不上忙：**在挑房的阶段没有人靠单元号做决定**，
        到了要去看房那一步才需要。
        """
        parts = [self.address or ""]
        if self.building:
            parts.append(self.building)
        if self.unit:
            parts.append(self.unit)
        return " ".join(x for x in parts if x)

    @property
    def floor_text(self):
        """楼层的展示串，由 floor_no / floor_total 拼出来。

        **不再让房东手填。** 手填的那一版什么格式都有——「11/33层」
        「11层」「十一楼」——既没法筛选，也会和按 floor_no 算出来的
        「低楼层」并排出现，同一张卡片上两个楼层说法。
        """
        if self.floor_no and self.floor_total:
            return "%d/%d层" % (self.floor_no, self.floor_total)
        if self.floor_no:
            return "%d层" % self.floor_no
        return self.floor or ""

    @property
    def floor_level(self):
        """低 / 中 / 高楼层。列表卡片和筛选按钮上显示的都是这个。

        按三等分算，不写死「1-3 层是低楼层」——6 层的老楼和 33 层的塔楼，
        同样是第 5 层，一个是中高层一个是底层，固定阈值对不上。
        楼层信息不全时返回空串，前端据此不显示这一项。
        """
        if not self.floor_no or not self.floor_total or self.floor_total < 1:
            return ""
        ratio = self.floor_no / self.floor_total
        if ratio <= 1 / 3:
            return "低楼层"
        if ratio <= 2 / 3:
            return "中楼层"
        return "高楼层"

    @property
    def fee_detail(self):
        """费用明细（K-07）。

        贝壳详情页有一整个「费用详情」标签页，而我们原先只在详情页摆了
        「租金 / 押金」两个数。缺的不是字段，是**口径**——租客真正想知道的
        是「入住前我要掏多少钱」，而这个数要自己拿租金、押金、付款周期、
        服务费四项去算，算错了就是白跑一趟。

        所以这里把它算出来，并且**和合同走同一份数据**：
        `deposit` 就是签约时要付的押金（见 `leasing.resolve_deposit`），
        `pay_months` 就是合同里的付款周期。详情页和合同因此不可能对不上。
        """
        rent = float(self.rent_price)
        deposit = float(self.deposit or 0)
        service_fee = float(self.service_fee or 0)
        # 付款周期没填按一月一付算——这是最常见的情形，也是最保守的估计
        months = self.pay_months or 1
        return {
            "rent": rent,
            "pay_months": months,
            "rent_first_term": rent * months,
            "deposit": deposit,
            "service_fee": service_fee,
            # 首期支付 = 押金 + 首期租金 + 服务费
            "first_payment": deposit + rent * months + service_fee,
        }

    @property
    def pay_plan(self):
        """「押一付三」这样的短语。两列都填了才拼得出来。"""
        if not self.deposit_months or not self.pay_months:
            return ""
        digits = "零一二三四五六七八九"
        def cn(n):
            return digits[n] if 0 <= n < len(digits) else str(n)
        return "押%s付%s" % (cn(self.deposit_months), cn(self.pay_months))

    @property
    def avg_rating(self):
        """平均评分。没有评价时返回 0，前端据此显示「暂无评价」。"""
        alive = [r for r in self.reviews if not r.deleted_at]
        if not alive:
            return 0
        return round(sum(r.rating for r in alive) / len(alive), 1)


class Appointment(Base):
    """看房预约。

    `viewing_time` 必须是 `DateTime`：AI-06 要处理「这周六下午两点」，
    只存日期就丢掉了时刻，也无法做时段冲突检测。
    """

    __tablename__ = "appointments"
    # 刻意不加 UNIQUE(house_id, viewing_time)：
    # 该约束既太严——取消掉的预约会永久占住那个时段，与「取消后释放」的
    # 业务规则冲突；又太松——管不了「一次看房占用一小时」的重叠判断。
    # 真正的规则由接口层的时段冲突校验实现，那里会排除已取消与已拒绝的记录。

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    contact_name: Mapped[str] = mapped_column(String(50), default="")
    contact_phone: Mapped[str] = mapped_column(String(20), default="")
    viewing_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    remark: Mapped[str] = mapped_column(Text, default="")

    # pending 待确认 / confirmed 已确认 / rejected 已拒绝 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reply: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(back_populates="appointments", lazy="selectin")
    # 取租客的账号名与免打扰状态用（J-07）。tenant_id 可空（历史数据里
    # 有匿名预约），所以用它的地方一律要判空
    tenant: Mapped["User"] = relationship(foreign_keys=[tenant_id],
                                          lazy="selectin")

    def to_dict(self, show_contact=True):
        """一条预约。

        `show_contact=False` 时不返回联系人姓名与号码（J-07）——
        受托中介只在**看房当天、且预约已确认**的窗口里看得到，
        窗口外那个号码对他没有用处，只是一份可以慢慢攒的通讯录。
        判定在 `permissions.can_see_tenant_contact`，不在这里。
        """
        return {
            "id": self.id,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            # 光有编号看不出是哪套房。用户在「我的预约」里看到一行
            # 「DL0007」，得再去房源列表里翻一遍才知道约的是什么——
            # 把小区、户型、租金一并带上，列表才读得懂。
            "house_title": self.house.title if self.house else "",
            "community": self.house.community if self.house else "",
            "district": self.house.district if self.house else "",
            "layout": self.house.layout if self.house else "",
            "rent_price": float(self.house.rent_price) if self.house else 0,
            "contact_name": self.contact_name if show_contact else "",
            # 号码在窗口外一律不给（J-07）。**给空串而不是省略这个键**：
            # 前端拿不到键会渲染成 undefined，拿到空串才知道「现在看不到」
            "contact_phone": (mask_phone(self.contact_phone)
                              if show_contact else ""),
            "contact_visible": bool(show_contact),
            # 供中介端把「联系租客」按钮置灰（J-07）。**按钮是提示，
            # 接口才是边界**——真正的拦截在 start_conversation 里
            "tenant_username": self.tenant.username if self.tenant else "",
            "accept_contact": (bool(self.tenant.accept_contact)
                               if self.tenant else True),
            "viewing_time": self.viewing_time.strftime("%Y-%m-%d %H:%M:%S"),
            "remark": self.remark,
            "status": self.status,
            "reply": self.reply,
        }


class Favorite(Base):
    """收藏。"""

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "house_id", name="uq_user_house"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Repair(Base):
    """房屋报修工单。

    `category` / `urgency` / `assignee` 三个字段由 AI-04 报修智能体
    从租客的自由文本描述中分析得出，不需要租客自己填。
    """

    __tablename__ = "repairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    contact_name: Mapped[str] = mapped_column(String(50), default="")
    contact_phone: Mapped[str] = mapped_column(String(20), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 以下三项由智能体分析填充 ----
    # 水电 / 家具 / 门窗 / 家电 / 其他
    category: Mapped[str] = mapped_column(String(30), default="", index=True)
    # 紧急 / 一般 / 可延后
    urgency: Mapped[str] = mapped_column(String(20), default="", index=True)
    assignee: Mapped[str] = mapped_column(String(50), default="")
    analysis: Mapped[str] = mapped_column(Text, default="")

    # pending 待处理 / processing 处理中 / done 已完成
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(lazy="selectin")
    # 报修人。房东要从工单直接联系他，得知道是哪个账号
    tenant: Mapped["User"] = relationship(foreign_keys=[tenant_id], lazy="selectin")
    # 结案登记。uselist=False 是因为 repair_id 上有唯一约束——
    # 一张工单只能结案一次
    record: Mapped["RepairRecord | None"] = relationship(back_populates="repair", uselist=False, lazy="selectin")

    def to_dict(self, with_contact=True):
        """工单。

        `with_contact=False` 用于**公开的房源维修历史**：房源详情页要让
        没登录的访客看到「这套房修过什么、修好没有」，那是对租客透明的
        信息；但**报修人是谁、他的号码后四位，不是**。

        姓名 + 号码后四位 + 具体房源地址凑在一起足以定位到一个真人，
        而这三样此前是任何人打开房源页就能看到的——上一任租客的名字，
        连同「他家卧室门锁坏过」，一起摆在下一位看房人面前。
        """
        contact = {"contact_name": self.contact_name,
                   "contact_phone": mask_phone(self.contact_phone),
                   # 报修人账号：工单详情里「联系报修人」要用它开会话
                   "tenant_id": self.tenant_id,
                   "tenant_username": self.tenant.username if self.tenant else ""}             if with_contact else {}
        return {
            "id": self.id,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            **contact,
            "content": self.content,
            "category": self.category,
            "urgency": self.urgency,
            "assignee": self.assignee,
            "analysis": self.analysis,
            "status": self.status,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            # 处理结果。**刻意不含费用**：维修花了多少是房东的成本，
            # 租客只需要知道谁来修的、修了什么、什么时候完成。
            # 费用要看得走 /api/repair-records（仅房东与管理员）。
            "result": {
                "worker_name": (self.record.worker.name
                                if self.record.worker else "维修人员"),
                "note": self.record.note,
                "finished_at": self.record.finished_at.strftime("%Y-%m-%d %H:%M"),
            } if self.record else None,
        }


class ChatMessage(Base):
    """AI 对话记录，供智能体保存会话上下文与用户偏好。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


@event.listens_for(House, "before_insert")
def _approved_on_creation(mapper, connection, house):
    """直接以「在架 / 已出租」建出来的房源（管理员发布、免审发布、演示数据）
    视为已通过审核；以 pending 建出来的等管理员审。"""
    if house.approved_at is None and house.status in ("available", "rented"):
        house.approved_at = house.published_at or datetime.now()


class HouseImage(Base):
    """房源相册。

    `is_cover` 标记封面图，列表页只取封面，详情页取全部。
    """

    __tablename__ = "house_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str] = mapped_column(String(100), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_cover: Mapped[bool] = mapped_column(Boolean, default=False)

    # ---- K-04：图片质量与去重需要的元数据 ----
    # 尺寸与体积在上传时算一次存下来。每次要判断「这张图能不能当封面」
    # 都去读一遍文件是不必要的，图多了列表接口会被拖垮。
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # 内容哈希。盗图和「同一张图发十套房」是虚假房源最常见的形态，
    # 有哈希才查得出来；没有它，举报了也只能靠人眼比对。
    hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # pending 待审 / approved 通过 / rejected 驳回。
    # 默认 approved：本项目的图片审核是**事后**的，先展示、被举报或
    # 被管理员发现再下架。默认 pending 会让房东发完图看不到自己的房子。
    audit_status: Mapped[str] = mapped_column(String(20), default="approved")

    house: Mapped["House"] = relationship(back_populates="images")

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "caption": self.caption,
            "is_cover": self.is_cover,
            "width": self.width,
            "height": self.height,
            "audit_status": self.audit_status,
        }


class Contract(Base):
    """租赁合同。

    这是整条交易链的枢纽：看房通过后生成合同，合同生效后按月产生账单，
    退租时结算押金。房源的 `rented` 状态也由合同状态驱动。

    状态流转::

        pending ──租客确认──► active ──到期/退租──► ended
           │                    │
           └──任一方取消──► cancelled

    `deposit_refund` 记录退租时实际退还的押金，与 `deposit` 的差额即扣款。
    """

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # 促成这单的中介（J-06）。**甲方仍然是房东**——中介不是合同的一方，
    # 这个字段只用来算佣金和「累计成交」，不进合同正文；
    # 房东自管的房源签出来的合同这里就是空的
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                          nullable=True, index=True)
    # 签约时约定的佣金比例（月租的百分比）。空表示按委托上的比例算。
    # 中介费常在签约时才谈定，委托时写的 50% 不一定是最后成交的数（2026-10-22）
    commission_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=True)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 金额一律 Numeric，不用 varchar / int（需求 R-04）
    rent: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    deposit: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    # 押一付三这类付款方式，数字表示每期几个月
    pay_months: Mapped[int] = mapped_column(Integer, default=1)

    # pending 待确认 / active 生效中 / ended 已结束 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    # AI-02 合同智能审阅的**结论摘要**（JSON 字符串）：评分、一句话
    # 总结、风险条数、是否走了大模型。刻意不存风险明细——明细每次
    # 都能重新算出来，存下来只会在规则升级后变成一份过期的旧结论。
    review_result: Mapped[str] = mapped_column(Text, default="")

    # 退租 / 续租申请。**一方提，另一方点确认才算数**——原先租客点「续租一年」，
    # 新合同反倒等他自己确认；点「退租」当场就退、押金全额退，房东全程不知情。
    # end 退租 / renew 续租；空串表示没有待处理的申请
    request_type: Mapped[str] = mapped_column(String(10), default="")
    # 谁提的：tenant / landlord。确认按钮只给另一方
    request_by: Mapped[str] = mapped_column(String(10), default="")
    request_months: Mapped[int] = mapped_column(Integer, default=0)
    request_reason: Mapped[str] = mapped_column(String(100), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 房东提前退租时写明的押金拟扣金额。**租客同意的是这个数**——
    # 原先房东点「退租」当场生效、扣多少自己定，租客事后才知道（2026-10-22）
    request_deduct: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_reason: Mapped[str] = mapped_column(String(100), default="")
    deposit_refund: Mapped[float] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(lazy="selectin")
    bills: Mapped[list["Bill"]] = relationship(back_populates="contract")
    # 受托中介。**必须写明 foreign_keys**：这张表上有三个指向 users 的
    # 外键（tenant_id / landlord_id / agent_id），不指定的话
    # SQLAlchemy 无从判断该走哪一个，映射直接报错。
    # selectin 是因为「待评价」列表要逐条读中介名，懒加载会变成 N+1
    agent: Mapped["User"] = relationship(foreign_keys=[agent_id],
                                         lazy="selectin")

    @property
    def expired(self):
        """**已到期、还没办退租**：状态仍是生效中，但到期日已到。

        系统不会到期自动结束合同——押金要结算、屋子要验收，这一步得房东来办。
        原先这种合同只显示「生效中 · 剩余 0 天」，房源一直「已出租」，
        谁也不会被提醒（2026-10-22）。现在单独标出来，房东工作台列为待办，
        房东办理时不需要租客再确认：到期退租是合同本来的约定，不是提前解约。
        """
        return self.status == "active" and self.end_date.date() <= datetime.now().date()

    @property
    def days_left(self):
        """剩余天数。已结束的合同返回 0。"""
        if self.status != "active":
            return 0
        return max((self.end_date.date() - datetime.now().date()).days, 0)

    @property
    def review_summary(self):
        """AI-02 审阅结论摘要。存的是 JSON 字符串，解不动就当没审过——
        一个坏掉的字段不该让整个合同接口跟着报错。"""
        if not self.review_result:
            return None
        try:
            return json.loads(self.review_result)
        except (ValueError, TypeError):
            return None

    def to_dict(self):
        return {
            "id": self.id,
            "contract_no": self.contract_no,
            "house_id": self.house_id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "tenant_id": self.tenant_id,
            "landlord_id": self.landlord_id,
            "start_date": self.start_date.strftime("%Y-%m-%d"),
            "end_date": self.end_date.strftime("%Y-%m-%d"),
            "rent": float(self.rent),
            # 房源挂牌价。和 rent 不同说明签约时议过价，页面据此标出来
            "list_rent": float(self.house.rent_price) if self.house else None,
            "deposit": float(self.deposit),
            "pay_months": self.pay_months,
            "commission_rate": (float(self.commission_rate)
                                if self.commission_rate is not None else None),
            "status": self.status,
            "content": self.content,
            "days_left": self.days_left,
            "expired": self.expired,
            "end_reason": self.end_reason,
            "deposit_refund": (float(self.deposit_refund)
                               if self.deposit_refund is not None else None),
            "signed_at": self.signed_at.strftime("%Y-%m-%d %H:%M") if self.signed_at else "",
            "review_result": self.review_summary,
            "request": ({"type": self.request_type, "by": self.request_by,
                         "months": self.request_months, "reason": self.request_reason,
                         "deduct": float(self.request_deduct or 0),
                         "at": self.requested_at.strftime("%Y-%m-%d %H:%M")
                         if self.requested_at else ""}
                        if self.request_type else None),
            # 这份合同有没有中介参与。评价表单靠它决定要不要显示
            # 「中介」那一栏——没中介却摆一排星星，用户会以为漏填了什么
            "agent_id": self.agent_id,
            "agent_name": mask_name(self.agent) if self.agent else "",
        }


class Bill(Base):
    """账单。

    合同生效时一次性按租期生成全部租金账单加一条押金账单，
    这样租客能提前看到整个租期的缴费计划，房东也能预估现金流。
    """

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"),
                                             nullable=False, index=True)

    # rent 租金 / deposit 押金 / utility 水电 / other 其它
    type: Mapped[str] = mapped_column(String(20), default="rent", index=True)
    period: Mapped[str] = mapped_column(String(20), default="")  # 如 2026-10
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # unpaid 待缴 / paid 已缴 / cancelled 已作废
    status: Mapped[str] = mapped_column(String(20), default="unpaid", index=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    remark: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    contract: Mapped["Contract"] = relationship(back_populates="bills", lazy="selectin")
    # **每一次支付尝试都留着**（2026-10-22）。原先一张账单只许一条支付记录：
    # 收银台过期或失败后重新发起会撞唯一约束，账单从此付不了；临时修法是
    # 就地覆盖旧记录——能付了，但「付失败过一次」这件事也就从账上消失了。
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="bill", lazy="selectin", order_by="Payment.id")
    # 这张账单上的退款（押金结算、提前退租退回的预付租金）
    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="bill", lazy="selectin", order_by="Refund.id")

    @property
    def payment(self):
        """**这张账单的那一笔支付**：成功的那条；还没成功就是最近一次尝试。

        原先是一对一关系，调用方到处在用 `bill.payment`——保留这个名字，
        语义不变：已付的账单拿到的就是付成的那一笔。
        """
        if not self.payments:
            return None
        succeeded = [p for p in self.payments if p.status == "succeeded"]
        return succeeded[-1] if succeeded else self.payments[-1]

    @property
    def refunded_amount(self):
        return float(sum(Decimal(str(r.refund_amount)) for r in self.refunds
                         if r.status == "succeeded"))

    @property
    def net_amount(self):
        """实收：已缴的减去退回的。**算收入一律用它**——提前退租退回的预付租金
        如果还按账单原额算进「本月收租」，房东看到的收入就比实际多。"""
        if self.status != "paid":
            return 0.0
        return float(Decimal(str(self.amount)) - Decimal(str(self.refunded_amount)))

    @property
    def overdue(self):
        """是否逾期。已缴与已作废的不算。"""
        return (self.status == "unpaid"
                and self.due_date.date() < datetime.now().date())

    def to_dict(self):
        return {
            "id": self.id,
            "bill_no": self.bill_no,
            "contract_id": self.contract_id,
            "contract_no": self.contract.contract_no if self.contract else "",
            # 合同还在不在。**界面要靠它决定给不给「立即缴费」按钮**——
            # 合同结束之后这笔欠款只能线下找房东结，接口也会拒绝
            # （见 /api/bills/{no}/pay）。按钮照摆的话，用户填完收银台
            # 才被告知付不了，白走一趟
            "contract_status": self.contract.status if self.contract else "",
            # 只有 unpaid 能缴。cancelled 也不是 paid，但它是作废，
            # 不是欠款——今天靠「合同必须 active」挡住了，
            # 可那是间接的，写明白比依赖巧合可靠
            "payable": bool(self.contract and self.contract.status == "active"
                            and self.status == "unpaid"
                            and float(self.amount or 0) > 0),
            "house_no": (self.contract.house.house_no
                         if self.contract and self.contract.house else ""),
            "type": self.type,
            "period": self.period,
            "amount": float(self.amount),
            "due_date": self.due_date.strftime("%Y-%m-%d"),
            "status": self.status,
            "overdue": self.overdue,
            "paid_at": self.paid_at.strftime("%Y-%m-%d %H:%M") if self.paid_at else "",
            "remark": self.remark,
            "payment": self.payment.to_dict() if self.payment else None,
            # 每一次发起支付的记录：成功、失败、超时关单都在。
            # 「我明明点过付款」这种纠纷，靠的就是这几行
            "attempts": [{"trade_no": p.trade_no, "method": p.method, "status": p.status,
                          "amount": float(p.amount),
                          "at": p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
                          "failure_reason": p.failure_reason or ""}
                         for p in self.payments],
            "refunded_amount": self.refunded_amount,
        }


class Payment(Base):
    """缴费流水：**每一次发起支付一条**，成功、失败、超时关单都留着。

    单独成表——真实系统里退款、对账、第三方回调都挂在流水上，
    账单只表达「该交多少」。一张账单最多一条 succeeded（由 _guard_payable 保证）。
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), nullable=False,
                                         index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # 课程项目不接真实支付，method 仅作展示：alipay / wechat / bank / offline
    method: Mapped[str] = mapped_column(String(20), default="alipay")

    # 支付单状态机（D-1 三段式改造）：
    #   pending   已发起，等网关回调——**此时账单仍是 unpaid，钱还没到**
    #   succeeded 回调确认到账，账单才置 paid
    #   failed    网关明确回调支付失败
    #   closed    超时未回调，平台主动关单，用户可以重新发起
    #
    # 改造之前这个字段默认就是 succeeded：点一下就算付了，没有「在途」状态。
    # 真实系统里「在途」是必须存在的——用户跳到收银台还没付、网络断了、
    # 回调延迟，这些都停在 pending，既不能算已付，也不能当没发生过。
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    # 幂等键：同一笔缴费重复提交只产生一条流水（前端重试、用户连点）
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True,
                                                        nullable=True)
    failure_reason: Mapped[str] = mapped_column(String(200), default="")

    # ---- 以下四列服务于回调链路 ----
    # 网关侧单号。对账时以它为准——平台单号网关不认。
    gateway: Mapped[str] = mapped_column(String(20), default="mock")
    gateway_trade_no: Mapped[str | None] = mapped_column(String(64), index=True,
                                                          nullable=True)
    # 回调报文原样存档。对不上账时要能拿出网关究竟推了什么，
    # 只存一个「已处理」的结论是查不了问题的。
    notify_payload: Mapped[str] = mapped_column(Text, default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 到账时间。**pending 期间它是空的**——原先这里无条件填当前时间，
    # 等于「发起」和「到账」共用一个时间戳，对账时分不出钱是什么时候到的。
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    bill: Mapped["Bill"] = relationship(back_populates="payments")

    def to_dict(self):
        return {
            "id": self.id,
            "trade_no": self.trade_no,
            "bill_id": self.bill_id,
            "amount": float(self.amount),
            "method": self.method,
            "status": self.status,
            "idempotency_key": self.idempotency_key or "",
            "failure_reason": self.failure_reason,
            "gateway": self.gateway,
            "gateway_trade_no": self.gateway_trade_no or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S")
                          if self.created_at else "",
            "paid_at": self.paid_at.strftime("%Y-%m-%d %H:%M:%S")
                       if self.paid_at else "",
            "expire_at": self.expire_at.strftime("%Y-%m-%d %H:%M:%S")
                         if self.expire_at else "",
        }


class Refund(Base):
    """退款流水；课程项目中记录模拟退款，不调用真实支付渠道。

    两类（2026-10-22 起）：
    - deposit：退租结算押金。original 是押金，deduct 是房东扣款，refund 是实退；
    - rent：提前退租时**已付未住**的租金按天退回。original 是那张账单的金额，
      deduct 是已住天数对应的部分，refund 是退回的部分。原先这笔钱不退——
      系统自己生成的合同里写着「甲方应退还已付但尚未居住月份的租金」。
    """

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refund_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"),
                                             nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(20), default="deposit")
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"),
                                                nullable=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"),
                                                   nullable=True)
    original_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    deduct_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    refund_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="succeeded", index=True)
    refunded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    bill: Mapped["Bill | None"] = relationship(back_populates="refunds", lazy="selectin")
    contract: Mapped["Contract"] = relationship(lazy="selectin")

    def to_dict(self):
        return {
            "refund_no": self.refund_no,
            "type": self.type or "deposit",
            "bill_no": self.bill.bill_no if self.bill else "",
            "contract_no": self.contract.contract_no if self.contract else "",
            "house_no": (self.contract.house.house_no
                         if self.contract and self.contract.house else ""),
            "contract_id": self.contract_id,
            "payment_id": self.payment_id,
            "original_amount": float(self.original_amount),
            "deduct_amount": float(self.deduct_amount),
            "refund_amount": float(self.refund_amount),
            "reason": self.reason,
            "status": self.status,
            "refunded_at": self.refunded_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Notice(Base):
    """平台公告。同时作为 AI-03 智能客服的检索语料。"""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(30), default="公告", index=True)
    publisher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "category": self.category,
            "view_count": self.view_count,
            "published_at": self.published_at.strftime("%Y-%m-%d"),
        }


class Message(Base):
    """站内消息。

    预约被确认、账单到期、工单进度变化都往这里写一条，
    解决目前「房东确认了预约但租客毫不知情」的问题。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # appointment 预约 / bill 账单 / repair 报修 / contract 合同 / system 系统
    type: Mapped[str] = mapped_column(String(20), default="system", index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    # 关联对象编号，前端据此跳转
    link_no: Mapped[str] = mapped_column(String(32), default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "link_no": self.link_no,
            "is_read": self.is_read,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class BrowsingHistory(Base):
    """浏览足迹。

    每个用户对同一房源只保留一条，重复浏览更新时间与次数——
    否则一个用户刷十次详情页就会把足迹列表刷满。
    """

    __tablename__ = "browsing_history"
    __table_args__ = (
        UniqueConstraint("user_id", "house_id", name="uq_history_user_house"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=1)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    house: Mapped["House"] = relationship()


class Review(Base):
    """租后评价。

    只有租约正常结束的租客才能评价，且一份合同只能评一次——
    不然任何人都能刷分，评分就没有参考价值了。
    """

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_review_contract"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # 1–5 星
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    # 分项评分，便于展示「房源真实度」「房东响应」这类维度
    rating_house: Mapped[int] = mapped_column(Integer, default=0)
    rating_landlord: Mapped[int] = mapped_column(Integer, default=0)
    # 受托中介的评分。**和房源评分必须分开。**
    #
    # 中介的口碑原来是拿这套房的总体评分算的，可这两件事经常相反：
    # 房子采光差、隔音烂，中介带看准时、协调修东西很上心——
    # 把房源的 2 分算到他头上，他做得再好也翻不了身；反过来
    # 房子好中介失约，也会给他一个他不配的高分。
    #
    # 0 表示这一栏没评（没有中介参与，或租客跳过了）。评分聚合时
    # 一律过滤掉 0，不能当成「0 分」拉低均值
    rating_agent: Mapped[int] = mapped_column(Integer, default=0)
    # 这份合同当时的受托中介。**在评价上冗余一份，不靠合同去 join。**
    # 委托关系会解除、会换人，而「这条评价评的是谁」是当时就定死的事实
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True,
                                          index=True)
    # 分开写给房东、写给中介的话。挤进同一个 content 里，
    # 房东概要页展示时就得原样把评中介的那半句也贴出去
    content_landlord: Mapped[str] = mapped_column(Text, default="")
    content_agent: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    # C1：随评价上传的实拍图，存 JSON 数组（最多 6 张）。
    #
    # **没有单开一张 review_images 表**，理由和房源相册那边正相反：
    # 房源图要单独排序、设封面、审核、算哈希查盗图，每张图都是一个
    # 有自己生命周期的对象；评价图只跟着评价生灭，既不排序也不换封面，
    # 唯一的操作是「和评价一起展示」和「评价被下架时一起看不见」。
    # 为它建表只会换来一次 join 和一套用不上的增删改接口。
    images: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 软删除（C-5）。审核下架的评价不能真删——误判之后无法恢复，
    # 用户申诉时也拿不出原文。前台查询一律过滤掉非空的记录。
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # **加了 agent_id 之后这两个都必须写明 foreign_keys。**
    # reviews 表现在有两个外键指向 users（tenant_id / agent_id），
    # SQLAlchemy 无从判断 `tenant` 该走哪一个，映射在**第一次真正使用时**
    # 才抛 AmbiguousForeignKeysError——import 阶段是过得去的，
    # 所以「导入成功」证明不了映射是好的。
    tenant: Mapped["User"] = relationship(foreign_keys=[tenant_id],
                                          lazy="selectin")
    agent: Mapped["User"] = relationship(foreign_keys=[agent_id],
                                         lazy="selectin")

    def to_dict(self):
        name = ""
        if self.tenant:
            # 评价对所有人可见，只露姓氏，其余打码
            name = mask_name(self.tenant)
        return {
            "id": self.id,
            "rating": self.rating,
            "rating_house": self.rating_house,
            "rating_landlord": self.rating_landlord,
            "rating_agent": self.rating_agent,
            "content_landlord": self.content_landlord or "",
            "content_agent": self.content_agent or "",
            "content": self.content,
            "images": self.image_list(),
            "tenant_name": name,
            "created_at": self.created_at.strftime("%Y-%m-%d"),
        }

    def image_list(self):
        """把 images 列还原成列表。

        存的是自己写进去的 JSON，正常不会坏；但**列是后加的**，
        老记录里是 NULL，脏数据也不是不可能。一条评价的配图解析失败，
        不该让整个房源详情页 500——返回空列表，图没了，评价还在。
        """
        if not self.images:
            return []
        try:
            data = json.loads(self.images)
        except (ValueError, TypeError):
            return []
        return [u for u in data if isinstance(u, str)] if isinstance(data, list) else []


class OperationLog(Base):
    """操作日志（M-07）。

    只记录**改变了状态的操作**：GET 请求不落库。全量记访问会让日志
    以每天几万条的速度增长，真正需要追溯时反而翻不动；而审计要回答的
    问题从来是「谁改了什么」，不是「谁看了什么」。

    用户名与角色是**冗余存下来的**，不靠外键 join 取。用户改名或被删
    之后，日志仍要能说清当时是谁做的——这正是审计日志存在的意义。
    """

    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(50), default="")
    role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(60), default="")
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(45), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username or "匿名",
            "role": self.role or "-",
            "action": self.action,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "ip": self.ip,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class ContentFlag(Base):
    """待人工复核的内容（M-08）。

    只记「疑似有问题、但没到直接拦」的内容。直接拦下的根本没进库，
    也就不需要复核；真正需要人看一眼的恰恰是机器拿不准的那部分。

    作者名与内容摘要都**冗余存下来**：等管理员来看的时候，原内容可能
    已经被作者自己改掉或删掉了，那时再去 join 就什么都查不到了。
    """

    __tablename__ = "content_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # house / review / notice / chat
    scene: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_key: Mapped[str] = mapped_column(String(64), default="")
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    author_name: Mapped[str] = mapped_column(String(50), default="")
    excerpt: Mapped[str] = mapped_column(String(300), default="")
    categories: Mapped[str] = mapped_column(String(120), default="")
    reasons: Mapped[str] = mapped_column(Text, default="")
    # pending 待复核 / approved 复核通过 / removed 已下架
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    handled_by: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    SCENE_LABEL = {"house": "房源", "review": "评价", "notice": "公告",
                   "chat": "私聊"}

    def to_dict(self):
        return {
            "id": self.id,
            "scene": self.scene,
            "scene_label": self.SCENE_LABEL.get(self.scene, self.scene),
            "target_id": self.target_id,
            "target_key": self.target_key,
            "author_name": self.author_name or "未知",
            "excerpt": self.excerpt,
            "categories": [c for c in (self.categories or "").split(",") if c],
            "reasons": [r for r in (self.reasons or "").split("\n") if r],
            "status": self.status,
            "handled_by": self.handled_by,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
            "handled_at": (self.handled_at.strftime("%Y-%m-%d %H:%M")
                           if self.handled_at else ""),
        }


class RepairImage(Base):
    """报修照片（F-08）。

    单开一张表而不是往 repairs 里加一列。SQLite 的 create_all 只会补建
    缺失的表、不会给已有的表加列——加列会让组员手上的旧库悄悄少一个字段，
    加表则拉下代码就能用。
    """

    __tablename__ = "repair_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"),
                                           nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RepairWorker(Base):
    """维修人员档案（F-09）。"""

    __tablename__ = "repair_workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), default="")
    # 擅长的故障类型，与报修工单的 category 取值一致，便于派单时匹配
    skill: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            # 维修人员的电话只有房东与管理员看得到，同样按 R-02 掩码
            "phone": mask_phone(self.phone),
            "skill": self.skill,
            "status": self.status,
        }


class RepairItem(Base):
    """维修物品／耗材（F-09）。"""

    __tablename__ = "repair_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    unit: Mapped[str] = mapped_column(String(10), default="个")
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "unit_price": float(self.unit_price or 0),
            "stock": self.stock,
        }


class RepairRecord(Base):
    """维修记录：一张工单完成时登记的人、物、钱（F-09）。

    `repair_id` 上有唯一约束——一张工单只能结案一次。少了它，重复提交
    结案会把物料重复扣一遍库存、成本也重复计一遍，统计就废了。
    """

    __tablename__ = "repair_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"),
                                           unique=True, nullable=False)
    worker_id: Mapped[int] = mapped_column(ForeignKey("repair_workers.id"),
                                           nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    labor_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    item_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    repair: Mapped["Repair"] = relationship(back_populates="record", lazy="selectin")
    worker: Mapped["RepairWorker"] = relationship(lazy="selectin")
    usages: Mapped[list["RepairItemUsage"]] = relationship(back_populates="record", cascade="all, delete-orphan", lazy="selectin")

    @property
    def total_cost(self):
        return float(self.labor_cost or 0) + float(self.item_cost or 0)

    def to_dict(self):
        return {
            "id": self.id,
            "repair_id": self.repair_id,
            "house_no": (self.repair.house.house_no
                         if self.repair and self.repair.house else ""),
            "category": self.repair.category if self.repair else "",
            "worker_name": self.worker.name if self.worker else "未指派",
            "hours": float(self.hours or 0),
            "labor_cost": float(self.labor_cost or 0),
            "item_cost": float(self.item_cost or 0),
            "total_cost": round(self.total_cost, 2),
            "note": self.note,
            "items": [usage.to_dict() for usage in self.usages],
            "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M"),
        }


class RepairItemUsage(Base):
    """一次维修用掉的物品与数量（F-09）。

    单价是**下单当时的快照**，不是外键跟着物品表走。物品调价之后，
    历史维修的成本不能跟着一起变——那等于把已经发生的账改了。
    """

    __tablename__ = "repair_item_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("repair_records.id"),
                                           nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("repair_items.id"),
                                         nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(50), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    record: Mapped["RepairRecord"] = relationship(back_populates="usages")

    @property
    def amount(self):
        return float(self.unit_price or 0) * (self.quantity or 0)

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "unit_price": float(self.unit_price or 0),
            "amount": round(self.amount, 2),
        }


class SystemConfig(Base):
    """系统配置（M-09）。

    值统一用字符串存，类型由 `value_type` 描述、读取时再转换。理由是
    配置项会随需求增删，用一张宽表给每种类型开一列，加一个布尔配置就
    要改表结构；字符串加类型标记则只是插一行数据。

    **配置必须真的生效**——存了值却没人读，那只是个假功能。当前每一项
    都接进了对应的业务逻辑，见 `app/config_store.py` 的说明。
    """

    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), default="")
    value_type: Mapped[str] = mapped_column(String(10), default="text")
    label: Mapped[str] = mapped_column(String(60), default="")
    description: Mapped[str] = mapped_column(String(200), default="")
    group_name: Mapped[str] = mapped_column(String(30), default="通用")
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)

    def to_dict(self):
        return {
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "label": self.label,
            "description": self.description,
            "group": self.group_name,
            "is_public": self.is_public,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M"),
        }


class Faq(Base):
    """客服知识条目——AI-03 智能客服的权威语料源。

    与 `notices` 的分工：公告是**面向全体用户的通告**（有时效、会过期），
    FAQ 是**平台规则的常驻问答**。两者都进检索，但生命周期完全不同，
    混在一张表里会让「公告过期下架」和「FAQ 长期有效」互相打架。

    `code` 而不是自增 id 作为检索侧的标识：智能体回答时要附引用，
    引用 id 出现在提示词与前端卡片里，用 `faq-deposit-refund` 这样
    可读的编码，比 `faq-37` 好排查得多——答辩时一眼看得出引的是哪条。
    """

    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 检索侧标识，全局唯一。种子数据来自 agent/knowledge/faq.md 的 id 字段
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    question: Mapped[str] = mapped_column(String(200), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(30), default="其他", index=True)
    # 同义问法，用 " / " 分隔。检索时每条别名单独建索引
    aliases: Mapped[str] = mapped_column(String(255), default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 被智能体引用的次数。管理员据此知道哪些问题问得最多、
    # 哪些条目写了没人用——这是运营视角，不是技术指标
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "aliases": self.aliases,
            "is_published": self.is_published,
            "sort_order": self.sort_order,
            "hit_count": self.hit_count,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M"),
        }


class SupportLog(Base):
    """客服问答日志——每一次提问的检索与判定过程。

    存它不是为了排错，是为了**回答「这个客服到底行不行」**：
    拒答率是多少、哪些问题反复问却答不出、命中的都是哪几条 FAQ。
    没有这张表，语料该往哪补全靠拍脑袋。

    `status` 与智能体返回的一致：answered / refused / escalate /
    account / need_login。
    """

    __tablename__ = "support_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True,
                                         index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="", index=True)
    # 检索模式：lexical / vector+lexical / account
    mode: Mapped[str] = mapped_column(String(20), default="")
    # 命中的片 id，逗号分隔；拒答时为空
    cited: Mapped[str] = mapped_column(String(255), default="")
    top_id: Mapped[str] = mapped_column(String(60), default="", index=True)
    cover: Mapped[float] = mapped_column(Float, default=0.0)
    # 回答由谁给出：llm / extractive / rules
    answer_source: Mapped[str] = mapped_column(String(20), default="")
    gate_reason: Mapped[str] = mapped_column(String(200), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 用户反馈：1 有用 / -1 没用 / 0 未评价
    feedback: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "question": self.question,
            "status": self.status,
            "mode": self.mode,
            "cited": [c for c in self.cited.split(",") if c],
            "top_id": self.top_id,
            "cover": round(self.cover, 3),
            "answer_source": self.answer_source,
            "gate_reason": self.gate_reason,
            "latency_ms": self.latency_ms,
            "feedback": self.feedback,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class SupportHandoff(Base):
    """转人工工单。

    智能体拒答、或用户明说要人工时，把问题连同上下文留给管理员。
    这是客服闭环里**最不该省**的一环：没有它，拒答就是死路一条，
    用户被告知「我不知道」之后无处可去。
    """

    __tablename__ = "support_handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handoff_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    # 智能体为什么没答上来，供管理员判断是语料缺失还是问题本身超范围
    reason: Mapped[str] = mapped_column(String(200), default="")
    context: Mapped[str] = mapped_column(Text, default="")
    # pending 待处理 / replied 已回复 / closed 已关闭
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reply: Mapped[str] = mapped_column(Text, default="")
    handler_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)
    replied_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "handoff_no": self.handoff_no,
            "user_id": self.user_id,
            "question": self.question,
            "reason": self.reason,
            "context": self.context,
            "status": self.status,
            "reply": self.reply,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
            "replied_at": (self.replied_at.strftime("%Y-%m-%d %H:%M")
                           if self.replied_at else ""),
        }


class Conversation(Base):
    """租客与房东围绕某套房源的会话（C-1）。

    系统里 AI 对话有两套（AI-09 助手、AI-03 客服），但**人与人之间不能
    聊**。现有的人际沟通只有三个孤立触点，每个都只能来回一次：预约的
    备注与回复、转人工的提问与回复、以及单向的站内通知。租客看中一套房
    想问「能不能养猫」「几月能入住」，没有地方问。

    **会话锚定在「房源 + 租客」上，不是锚定在人上。** 租客同时看十套房，
    问的都是「能养猫吗」，按人聚合会混成一团；房东手里二十套房，也需要
    一眼知道对方问的是哪套。会话天然带上下文，进来就显示房源卡片，
    双方不用先对暗号。
    """

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("house_id", "tenant_id", name="uq_house_tenant"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"),
                                          nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                           nullable=False, index=True)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                             nullable=False, index=True)

    # last_message 与两个未读数都是**刻意冗余**的。会话列表要显示
    # 「最后一句 + 未读数」，不冗余就得对每个会话再查一次明细表——
    # 十个会话十次查询。代价是写入时要同步更新。
    last_message: Mapped[str] = mapped_column(String(100), default="")
    last_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                              index=True)
    # 必须分两个字段：同一条消息，对发送方是已读、对接收方是未读
    tenant_unread: Mapped[int] = mapped_column(Integer, default=0)
    landlord_unread: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(lazy="selectin")
    tenant: Mapped["User"] = relationship(foreign_keys=[tenant_id], lazy="selectin")
    landlord: Mapped["User"] = relationship(foreign_keys=[landlord_id], lazy="selectin")

    def to_dict(self, viewer_id=None, responder=None):
        """会话摘要。

        未读数按**查看者**给：同一个会话，租客和房东看到的未读数不同。
        对方姓名只露姓氏——会话列表不是通讯录。

        判断的是「**是不是租客那一侧**」，不是「是不是房东本人」。
        委托期间受托中介也会来读这个会话（J-03），他站在房东一侧，
        对面同样是租客。原来写成 `viewer_id == self.landlord_id`，
        中介进来会被当成第三种人，看到的「对方」变成房东——
        而他要联系的明明是租客。

        `responder` 是当前负责答的那个 User（委托中即受托中介），
        由路由层查好传进来。模型层拿不到 session，也不该为了显示
        一个称谓去查委托。
        """
        def masked(user):
            return mask_name(user)

        is_tenant = viewer_id == self.tenant_id
        if is_tenant:
            # 租客看到的「对方」是当前负责答的人，称谓也随之变。
            # 一直显示「房东」而实际由中介在回，租客会以为在和房东
            # 直接谈价——那是会出纠纷的误导。
            peer = responder or self.landlord
            peer_role = "经纪人" if (responder
                                 and responder.id != self.landlord_id)                 else "房东"
        else:
            peer = self.tenant
            peer_role = "租客"

        return {
            "id": self.id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "cover_image": self.house.cover_image if self.house else "",
            "rent_price": float(self.house.rent_price) if self.house else 0,
            "peer_name": masked(peer),
            "peer_role": peer_role,
            "last_message": self.last_message,
            "last_at": self.last_at.strftime("%Y-%m-%d %H:%M"),
            "unread": (self.tenant_unread if is_tenant
                       else self.landlord_unread),
            "status": self.status,
        }


class ChatLine(Base):
    """会话里的一条消息。"""

    __tablename__ = "chat_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                           nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 将来要发图片或房源卡片时靠它区分，现在只有 text
    kind: Mapped[str] = mapped_column(String(10), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime,
                                                 default=datetime.now)

    sender: Mapped["User"] = relationship(lazy="selectin")

    def to_dict(self, viewer_id=None, tenant_id=None, sender_label=""):
        """一条消息。

        `mine` 是**按「哪一侧」算的，不是按「谁发的」**。会话只有两侧，
        气泡的左右表达的也是侧，不是人。委托期间中介进来看历史记录时，
        如果按发送者算，房东此前回的那几句会跑到租客那一侧去——
        中介会当场读错是谁说的。

        代价是同一侧内部分不清是房东还是中介说的，所以补一个
        `sender_label`：不是本人发的就标出来是谁。
        """
        if tenant_id is None:
            mine = self.sender_id == viewer_id
        else:
            viewer_is_tenant = viewer_id == tenant_id
            sender_is_tenant = self.sender_id == tenant_id
            mine = viewer_is_tenant == sender_is_tenant
        return {
            "id": self.id,
            "content": self.content,
            "kind": self.kind,
            "mine": mine,
            # 同一侧里不是自己发的才标，否则每条都挂个名字很吵
            "sender_label": (sender_label
                             if mine and self.sender_id != viewer_id else ""),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


def mask_name(user):
    """姓名只露姓氏。

    这个写法原本在评价、会话、带看三处各抄了一遍
    （`raw[0] + "*" * ...`）。抽出来不是为了少写几个字符，
    是为了「露几个字」这件事将来只需要改一处——
    脱敏规则散在三个地方，改的时候必定漏掉一个。
    """
    if not user:
        return ""
    raw = (getattr(user, "real_name", "") or getattr(user, "username", "")
           or "")
    if not raw:
        return ""
    return raw[0] + "*" * max(len(raw) - 1, 1)


def mask_phone(phone):
    """手机号中间四位掩码（R-02）。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]


# ============================================================ K-04 三张新表
#
# 这三张表服务于「保存搜索 / 上新提醒」「用户举报」「找回密码」，
# 三件事都还没写界面，但表必须赶在数据库评审之前定下来——
# 评审之后再改表要重新走一遍评审流程。


class SavedSearch(Base):
    """保存的搜索条件（K-09 的地基）。

    **收藏和保存搜索解决的不是同一个问题。** 收藏是「我看中了这套」，
    保存搜索是「我还没看中，但符合我条件的一出现就告诉我」。
    租房场景里后者才是常态：合适的房子随时上架、也随时被租走，
    用户不可能靠反复刷列表守到。

    提醒走站内消息，不接短信——短信通道要第三方账号，而站内消息模块
    是现成的（`Message` + `/api/messages`），够用且零外部依赖。
    """

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                         nullable=False, index=True)
    # 用户自己起的名字，如「上班近的两室」。没起名就用条件拼一个
    name: Mapped[str] = mapped_column(String(50), default="")
    # 筛选条件原样存 JSON。**不要拆成一堆列**——筛选维度还会继续加，
    # 每加一个就改一次表是不可持续的；而这份条件除了「原样回填到
    # 筛选栏」和「拿去匹配新房源」之外没有别的用法，不需要被单独查询。
    conditions: Mapped[str] = mapped_column(Text, default="{}")

    notify_on: Mapped[bool] = mapped_column(Boolean, default=True)
    last_notified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 已经就这条搜索提醒过的房源编号，逗号分隔。
    # 去重必须落库：同一套房每天提醒一次，用户第二天就把提醒关了。
    notified_house_nos: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship()

    def condition_dict(self):
        """条件 JSON 解析成字典。坏数据不该让整个列表接口崩掉。"""
        try:
            value = json.loads(self.conditions or "{}")
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def notified(self):
        return {n for n in (self.notified_house_nos or "").split(",") if n}

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "conditions": self.condition_dict(),
            "notify_on": self.notify_on,
            "last_notified_at": (self.last_notified_at.strftime("%Y-%m-%d %H:%M")
                                 if self.last_notified_at else ""),
            "created_at": self.created_at.strftime("%Y-%m-%d"),
        }


class HouseReport(Base):
    """用户举报房源（K-08 的地基）。

    **和 `ContentFlag` 是两张表，不要合并。** `ContentFlag` 记的是
    机器审核（`app/moderation.py`）自动拦下或标记的内容，有 `scene`、
    有命中的词、没有举报人；这张表记的是**人主动报上来的问题**，
    有举报人、有证据、要给举报人一个结论回执。两者的生命周期、
    权限和处理动作都不一样，塞进一张表只会让两边的字段互相为空。
    """

    __tablename__ = "house_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                             nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"),
                                          nullable=False, index=True)

    # fake 虚假房源 / stolen_photo 盗用图片 / group_rent 违规群租 /
    # price 价格异常 / other 其他
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # 证据图片 URL，逗号分隔。举报没有证据基本处理不了
    evidence: Mapped[str] = mapped_column(Text, default="")

    # pending 待处理 / accepted 属实 / rejected 不属实 / closed 已关闭
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    handler_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    conclusion: Mapped[str] = mapped_column(String(300), default="")
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    reporter: Mapped["User"] = relationship(foreign_keys=[reporter_id],
                                            lazy="selectin")
    house: Mapped["House"] = relationship(lazy="selectin")

    KINDS = {
        "fake": "虚假房源",
        "stolen_photo": "盗用图片",
        "group_rent": "违规群租",
        "price": "价格异常",
        "other": "其他",
    }

    def to_dict(self):
        return {
            "id": self.id,
            "report_no": self.report_no,
            "kind": self.kind,
            "kind_label": self.KINDS.get(self.kind, self.kind),
            "description": self.description,
            "evidence": [u for u in (self.evidence or "").split(",") if u],
            "status": self.status,
            "conclusion": self.conclusion,
            "reporter_name": (self.reporter.real_name or self.reporter.username
                              if self.reporter else ""),
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "handled_at": (self.handled_at.strftime("%Y-%m-%d %H:%M")
                           if self.handled_at else ""),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class PasswordReset(Base):
    """找回密码的一次性令牌（K-11 的地基）。

    **只存哈希，不存令牌原文。** 原文只在发出去的那封邮件里存在——
    库被读到也不能拿它去改别人的密码。这和 `users.password_hash`
    是同一条原则（R-01），一次性令牌同样是能直接夺号的凭据。

    真实平台走短信验证码，我们没有短信通道；但邮件通道是现成的
    （`app/notifier.py` 的 `SmtpNotifier`），零新增依赖。
    """

    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                         nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                            unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 用过就记时间。**不删行**——「这个链接已经用过了」和
    # 「这个链接从来不存在」要能分开，前者该提示用户重新申请。
    used_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 申请来源，排查异常申请用
    request_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship()

    def is_usable(self, now=None):
        now = now or datetime.now()
        return self.used_at is None and self.expires_at > now


# ============================================================ J 系列 中介
#
# 设计依据见 docs/中介功能设计方案.md。四张表对应一条服务链：
#
#     房东委托 ──> 中介带看 ──> 促成签约 ──> 结算佣金
#     delegations  viewing_records  contracts   commissions
#
# **一个统计字段都不存**（R-05）：在托量、带看量、成交量、好评率
# 全部由这几张明细表实时聚合。存汇总的下场是它和明细对不上，
# 而对不上的时候没人知道该信哪个。


class AgentProfile(Base):
    """中介档案（J-01）。

    和 users 一对一。为什么不把这些列直接加到 users 上：这里绝大多数
    字段只对 role 为 agent 的人有意义，加上去等于让每个租客都背着
    一串永远为空的从业资质字段。
    """

    __tablename__ = "agent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                         nullable=False, unique=True,
                                         index=True)

    # ---- 资质核验。状态机与 K-07 房源核验同构，刻意不另发明一套 ----
    # unsubmitted 未提交 / pending 待审核 / approved 已通过 / rejected 未通过
    license_status: Mapped[str] = mapped_column(String(20),
                                                default="unsubmitted",
                                                index=True)
    license_no: Mapped[str] = mapped_column(String(40), default="")
    license_images: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                             nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(String(200), default="")

    # ---- 对外展示 ----
    # 主营区域，逗号分隔（对标贝壳的「主营商圈」）
    service_districts: Mapped[str] = mapped_column(String(120), default="")
    intro: Mapped[str] = mapped_column(String(300), default="")
    # 从业年限。**这是唯一允许自填的展示字段**，其余全部聚合得出
    years: Mapped[int] = mapped_column(Integer, default=0)
    # 休假或满负荷时关掉，关掉后不出现在派单候选里
    accepting: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship(foreign_keys=[user_id],
                                        lazy="selectin")

    @property
    def districts(self):
        return [d for d in (self.service_districts or "").split(",") if d]

    @property
    def can_take_orders(self):
        """能不能接单。

        **未核验一律不许接单。** 这和 K-07「未核验不画成已核验的样子」
        是同一条原则的两面：那边是不美化，这边是不放行——一个没验过
        资质的人代表平台去见租客，风险比一条假房源大得多。
        """
        return self.license_status == "approved" and self.accepting

    def to_dict(self, contact=False):
        data = {
            "user_id": self.user_id,
            "username": self.user.username if self.user else "",
            "name": self.user.real_name if self.user else "",
            "license_status": self.license_status,
            "license_no": self.license_no,
            "districts": self.districts,
            "intro": self.intro,
            "years": self.years,
            "accepting": self.accepting,
            "can_take_orders": self.can_take_orders,
            "reject_reason": self.reject_reason,
            "reviewed_at": (self.reviewed_at.strftime("%Y-%m-%d")
                            if self.reviewed_at else ""),
        }
        # 手机号只在明确需要联系时给，且仍然是掩码（R-02、J-07）。
        # 平台的价值就在于居间，把号码摆出来等于鼓励绕过平台私下成交
        if contact and self.user:
            data["phone"] = mask_phone(self.user.phone)
        return data


class HouseDelegation(Base):
    """房源委托（J-04）——中介这条链的核心表。

    三条硬约束，都在业务层强制：

    1. **双向确认才生效。** 房东可以邀请中介、中介也可以申请接单，
       但都要另一方点头。单方面就能建立委托，等于允许中介给自己派活。
    2. **一套房同时只能有一个 active 委托。** 两个中介同时在托，
       带看归谁、佣金归谁就说不清了——这正是贝壳要用 ACN 那一整套
       规则去解决的问题，而我们没有解决它的必要。
    3. **房源下架或签约生效时自动结束**，不留悬空关系。
    """

    __tablename__ = "house_delegations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"),
                                          nullable=False, index=True)
    landlord_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                             nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                          nullable=False, index=True)

    # pending 待确认 / active 生效中 / rejected 已拒绝 / ended 已结束
    status: Mapped[str] = mapped_column(String(20), default="pending",
                                        index=True)
    # landlord 或 agent，决定该由谁来确认
    initiated_by: Mapped[str] = mapped_column(String(10), default="landlord")

    # 佣金比例，按月租金的百分比。50 表示半个月租金
    commission_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=50)

    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_reason: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    house: Mapped["House"] = relationship(lazy="selectin")
    agent: Mapped["User"] = relationship(foreign_keys=[agent_id],
                                         lazy="selectin")
    landlord: Mapped["User"] = relationship(foreign_keys=[landlord_id],
                                            lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "district": self.house.district if self.house else "",
            "rent_price": float(self.house.rent_price) if self.house else 0,
            "agent_id": self.agent_id,
            "agent_name": (self.agent.real_name or self.agent.username
                           if self.agent else ""),
            "landlord_id": self.landlord_id,
            "landlord_name": (self.landlord.real_name or self.landlord.username
                              if self.landlord else ""),
            "status": self.status,
            "initiated_by": self.initiated_by,
            "commission_rate": float(self.commission_rate),
            "end_reason": self.end_reason,
            "start_at": (self.start_at.strftime("%Y-%m-%d")
                         if self.start_at else ""),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class ViewingRecord(Base):
    """带看记录（J-05）。

    贝壳把带看当成一等数据（ACN 里它直接参与分佣），我们也该如此。
    改造前系统里只有「预约」，**没有「预约之后发生了什么」**——
    房东看得到有人约，看不到有没有来、看了觉得怎么样。
    而后者才是他把带看委托出去之后唯一想知道的事。
    """

    __tablename__ = "viewing_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"),
                                                nullable=True, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"),
                                          nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                           nullable=False, index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                          nullable=False, index=True)

    # 租客有没有到场。放鸽子率是有意义的信号，但**要和中介的服务质量
    # 区分开**——这个数高通常说明预约确认流程有问题，不是中介的错
    showed_up: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    feedback: Mapped[str] = mapped_column(Text, default="")
    # 结构化的关注点与异议，逗号分隔。同一套房被三个人嫌厨房小，
    # 房东就该降价或在描述里说明了
    concerns: Mapped[str] = mapped_column(String(200), default="")
    # high 意向强 / medium 一般 / low 意向弱
    intention: Mapped[str] = mapped_column(String(10), default="medium",
                                           index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 index=True)

    house: Mapped["House"] = relationship(lazy="selectin")
    tenant: Mapped["User"] = relationship(foreign_keys=[tenant_id],
                                          lazy="selectin")

    INTENTIONS = {"high": "意向强", "medium": "一般", "low": "意向弱"}

    def to_dict(self):
        return {
            "id": self.id,
            "house_no": self.house.house_no if self.house else "",
            "house_title": self.house.title if self.house else "",
            "tenant_name": mask_name(self.tenant) if self.tenant else "租客",
            "showed_up": self.showed_up,
            "duration_minutes": self.duration_minutes,
            "feedback": self.feedback,
            "concerns": [c for c in (self.concerns or "").split(",") if c],
            "intention": self.intention,
            "intention_label": self.INTENTIONS.get(self.intention,
                                                   self.intention),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class AgentCommission(Base):
    """佣金（J-06）。

    **不复用 bills / payments。** 那两张表描述的是「租客付给房东」的
    交易链，字段（type、due_date、contract_id 的语义）全是按那条链
    设计的；佣金是「房东付给中介」，付款方、收款方、触发条件都不同，
    塞进去只会让两边的字段互相为空——和 K-08 不复用 content_flags
    是同一条理由。

    结算不接真实资金，与《待完善清单》D-1 同一个边界：
    状态机做全，钱线下结。
    """

    __tablename__ = "agent_commissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"),
                                             nullable=False, index=True)
    delegation_id: Mapped[int] = mapped_column(
        ForeignKey("house_delegations.id"), nullable=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                          nullable=False, index=True)

    # 计佣基数（月租金）与比例分开存。只存最终金额的话，事后想核对
    # 「这笔是怎么算出来的」就查不了了
    base_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    # pending 待结算 / settled 已结算 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(20), default="pending",
                                        index=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    contract: Mapped["Contract"] = relationship(lazy="selectin")

    def to_dict(self):
        contract = self.contract
        return {
            "id": self.id,
            "contract_no": contract.contract_no if contract else "",
            "house_no": (contract.house.house_no
                         if contract and contract.house else ""),
            "base_amount": float(self.base_amount),
            "rate": float(self.rate),
            "amount": float(self.amount),
            "status": self.status,
            "settled_at": (self.settled_at.strftime("%Y-%m-%d")
                           if self.settled_at else ""),
            "created_at": self.created_at.strftime("%Y-%m-%d"),
        }


class RoleRequest(Base):
    """角色升级申请（D1）。

    原先「成为房东」是**点一下当场生效**的：任何注册用户点一次就能发布
    房源、签合同、收租。放在演示里没问题，放在一个要签合同收钱的平台上
    不成立——房东是合同甲方，平台至少要看一眼这个人是谁。

    所以升级改成申请制：提交后进 pending，管理员在后台通过或驳回。
    **在获批之前用户仍是租客**，找房、收藏、预约照常，只是发不了房源。
    这一点必须在提交时就说清楚，否则用户会以为账号坏了。

    **退回不需要审核（D2）。** 房东想变回租客，随时可以——
    这不是权限升级，是主动放弃权限，没有需要把关的东西。
    唯一的前置是手上不能还有在租房源与生效合同，
    那是数据完整性要求，不是审批。
    """

    __tablename__ = "role_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    # 申请成为哪种角色：landlord / agent
    target_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # pending 待审 / approved 通过 / rejected 驳回 / withdrawn 用户自己撤回
    status: Mapped[str] = mapped_column(String(20), default="pending",
                                        nullable=False, index=True)
    # 申请理由，用户自己写。管理员据此判断，别只给一个用户名就让人拍板
    reason: Mapped[str] = mapped_column(Text, default="")
    # 驳回理由。**驳回必须填**——只说「不通过」等于让用户去猜
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                             nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id],
                                        lazy="selectin")

    def to_dict(self):
        u = self.user
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": u.username if u else "",
            "real_name": u.real_name if u else "",
            # 手机号给掩码。审核看的是「这个人填没填、填得像不像」，
            # 不需要完整号码；管理端也不该把它到处摊开
            "phone": (u.phone[:3] + "****" + u.phone[-4:]
                      if u and len(u.phone) == 11 else (u.phone if u else "")),
            "target_role": self.target_role,
            "target_role_label": {"landlord": "房东",
                                  "agent": "经纪人"}.get(self.target_role,
                                                       self.target_role),
            "status": self.status,
            "reason": self.reason,
            "review_note": self.review_note,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
            "reviewed_at": (self.reviewed_at.strftime("%Y-%m-%d %H:%M")
                            if self.reviewed_at else ""),
        }
