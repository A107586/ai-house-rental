"""房源级权限：谁管得了这一套房（J-02）。

**这个模块是中介功能改造的枢纽。**

改造之前，「房源的操作者」和「房东本人」在代码里是同一件事：

    if user.role != "admin" and house.landlord_id != user.id:
        raise HTTPException(403, "只能管理自己的房源")

这个判断散在七八个接口里。加中介时才发现它把两个概念焊死了——
受托中介既不是 admin 也不是 landlord_id，于是所有房源接口对他一律 403。

所以把「能不能管这套房」抽成一处。**判断只写一遍**，以后再加角色
（比如将来的托管公司、物业），改的也只有这一个函数。

三条硬规则，写在代码里而不只是文档里：

1. **房东的权限不因委托而丧失。** 委托是「多一个人能管」，不是「换人管」。
   房东随时能自己回消息、改房源、单方面解除委托。反过来做
   （委托期间房东被锁住）是把工具做成了枷锁。
2. **中介只能管有生效委托的那几套**，而且**不能删房源、不能改归属、
   不能发起签约**——签约是钱和法律责任，必须房东本人点。
   这一条不在这里实现，在于那几个接口继续用 `require_staff`（只放房东）。
3. **委托一解除，权限立刻消失。** 但已产生的带看记录与会话留存，
   问责需要。
"""

from fastapi import HTTPException
from sqlalchemy import select

from app.models import HouseDelegation


def active_delegation(session, house_id, agent_id=None):
    """取这套房当前生效中的委托。

    `agent_id` 给了就再核对受托人是不是他。
    一套房同时只允许一个 active 委托，所以这里返回单条而不是列表——
    返回列表会诱使调用方去处理「多个受托人」，而那种情况不该存在。
    """
    stmt = select(HouseDelegation).where(
        HouseDelegation.house_id == house_id,
        HouseDelegation.status == "active")
    if agent_id is not None:
        stmt = stmt.where(HouseDelegation.agent_id == agent_id)
    return session.scalar(stmt)


def can_manage_house(session, user, house):
    """这个人管不管得了这套房。"""
    if not user:
        return False
    if user.role == "admin":
        return True
    if house.landlord_id == user.id:
        return True          # 房东永远管得了自己的房，与委托无关
    if user.role != "agent":
        return False
    return active_delegation(session, house.id, user.id) is not None


def require_can_manage_house(session, user, house):
    """管不了就 403。

    提示语区分两种情况：中介看到的是「这套房不在你的受托范围内」，
    别人看到的是「只能管理自己的房源」。笼统一句「没有权限」会让
    刚解除委托的中介以为是系统坏了。
    """
    if can_manage_house(session, user, house):
        return
    if user and user.role == "agent":
        raise HTTPException(status_code=403,
                            detail="这套房不在你的受托范围内")
    raise HTTPException(status_code=403, detail="只能管理自己的房源")


def responsible_user_id(session, house):
    """这套房现在由谁答复租客。

    有生效委托就是受托中介，否则是房东。预约通知、会话路由都按它走。
    **房东仍然会收到抄送**——他委托出去的是带看，不是知情权。
    """
    delegation = active_delegation(session, house.id)
    return delegation.agent_id if delegation else house.landlord_id


# ---------------------------------------------------------------- J-07 隐私

def contact_window_open(appointment, now=None):
    """这条预约现在是不是处在「可以看到联系方式」的窗口里。

    规则：**预约已确认，且今天就是看房当天。**

    贝壳用虚拟号，虚拟号有有效期；我们做不了电话，但那条原则可以照搬——
    联系方式是为了「我到楼下了，你在哪」，不是给中介留一份可以慢慢用的
    通讯录。约在下周三的看房，中介今天不需要那个号码。
    """
    from datetime import datetime, timedelta

    if not appointment or appointment.status != "confirmed":
        return False
    if not appointment.viewing_time:
        return False
    now = now or datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start <= appointment.viewing_time < day_start + timedelta(days=1)


def can_see_tenant_contact(session, user, appointment, house=None):
    """这个人现在能不能看到这条预约上的租客联系方式（J-07）。

    **窗口只卡中介，不卡房东。**

    这不是给中介搞特殊，恰恰相反：设计方案 J-07 整节讲的就是
    「租客与中介」这一侧，理由是**中介比房东更有动机把交易引到站外**
    （省掉平台佣金），而房东是潜在合同的对方当事人，本来就要和租客
    建立直接联系，把他也卡住只会逼着双方另找渠道，反而更不可控。
    """
    if not user:
        return False
    if user.role == "admin":
        return True
    if appointment is not None and appointment.tenant_id == user.id:
        return True          # 自己的预约，看的是自己的号码
    if house is not None and house.landlord_id == user.id:
        return True          # 房东不受窗口限制，理由见上
    if not can_manage_house(session, user, house):
        return False
    return contact_window_open(appointment)
