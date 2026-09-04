"""保存搜索与上新提醒（K-09）。

**租房和买房不一样。** 合适的房子随时会上架，也随时会被租走，
所以用户不可能靠反复刷列表找到房。收藏解决的是「我看中了这套」，
保存搜索解决的是「我还没看中，但符合我条件的一出现就告诉我」——
后者才是租房用户的常态，贝壳把它单列在个人中心第三栏就是这个道理。

两个实现上的要点：

**一、匹配用的是列表页那条 SQL，不是另写一份判断。**
条件构造统一在 `app/house_query.py`。两份实现一旦分叉，表现是最难查的
那种：用户存的条件在列表页筛得到这套房，上新提醒却不发；
或者提醒说匹配上了，点进去列表里根本没有。

**二、提醒走站内消息，不接短信。**
短信通道要第三方账号（见《待完善清单》C-2），而站内消息模块是现成的。
真实平台用短信是因为用户不会天天开网站，这一点课程项目里不成立。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import house_query, leasing
from app.db import get_session
from app.deps import require_login
from app.models import House, SavedSearch, User

router = APIRouter()

# 一个人最多存多少条。不设上限的话，前端一个循环就能把表刷爆，
# 而每来一套新房都要拿所有条件跑一遍匹配——这是有成本的
MAX_PER_USER = 20


class SavedSearchCreate(BaseModel):
    name: str = Field(default="", max_length=50)
    conditions: dict = Field(default_factory=dict)
    notify_on: bool = True


class SavedSearchUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    notify_on: bool | None = None


@router.post("/api/saved-searches")
def create_saved_search(payload: SavedSearchCreate,
                        user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """保存当前的筛选条件。"""
    conditions = house_query.clean_conditions(payload.conditions)
    if not conditions:
        raise HTTPException(status_code=400,
                            detail="请先设置至少一个筛选条件再保存")

    count = len(session.scalars(select(SavedSearch).where(
        SavedSearch.user_id == user.id)).all())
    if count >= MAX_PER_USER:
        raise HTTPException(
            status_code=409,
            detail="最多只能保存 %d 条搜索，请先删掉不用的" % MAX_PER_USER)

    row = SavedSearch(
        user_id=user.id,
        # 不起名就用条件拼一句人话。「未命名搜索 3」对用户毫无意义，
        # 他需要一眼认出这是哪一组条件
        name=payload.name.strip() or house_query.describe(conditions),
        conditions=json.dumps(conditions, ensure_ascii=False),
        notify_on=payload.notify_on,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"ok": True, "saved_search": row.to_dict()}


@router.get("/api/saved-searches")
def list_saved_searches(user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    rows = session.scalars(
        select(SavedSearch)
        .where(SavedSearch.user_id == user.id)
        .order_by(SavedSearch.created_at.desc())).all()
    return {"total": len(rows), "list": [r.to_dict() for r in rows]}


@router.patch("/api/saved-searches/{search_id}")
def update_saved_search(search_id: int, payload: SavedSearchUpdate,
                        user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    """改名或开关提醒。

    **关掉提醒而不是只能删除**：用户可能暂时不想被打扰，但条件还想留着
    下次一键恢复。只给「删除」这一个选项，等于逼他把条件也一起丢掉。
    """
    row = session.get(SavedSearch, search_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="搜索条件不存在")

    if payload.name is not None:
        row.name = payload.name.strip() or house_query.describe(
            row.condition_dict())
    if payload.notify_on is not None:
        row.notify_on = payload.notify_on

    session.commit()
    session.refresh(row)
    return {"ok": True, "saved_search": row.to_dict()}


@router.delete("/api/saved-searches/{search_id}")
def delete_saved_search(search_id: int,
                        user: User = Depends(require_login),
                        session: Session = Depends(get_session)):
    row = session.get(SavedSearch, search_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="搜索条件不存在")
    session.delete(row)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 上新匹配

def notify_new_house(session, house):
    """一套房源上架时，通知所有条件匹配的用户。

    返回实际发出的提醒条数，方便接口层与测试断言。

    三条规则：

    - **只给别人发，不给房东自己发**——他刚发布完，再收到一条
      「有新房源符合你的条件」很荒唐；
    - **同一套房对同一条搜索只提醒一次**，已提醒的房号记在
      `notified_house_nos` 里。不去重的话，房源每次下架再上架都会
      重发一遍，用户第二天就把提醒关了；
    - **关掉提醒的搜索不发**，但仍然保留条件。
    """
    if house.status != "available":
        return 0

    rows = session.scalars(select(SavedSearch).where(
        SavedSearch.notify_on.is_(True))).all()

    # 同一组条件只判一次。
    #
    # 匹配必须走 `house_query.apply_filters`（否则就和列表页分叉了），
    # 而那是一条 SQL——**每条保存搜索一次往返**。这在真实数据下会变成
    # 问题：一套房上架要跑遍所有人的所有条件，而「中山区 + 两室」
    # 这种条件会有很多人存下同一份。
    #
    # 按条件 JSON 做一次记忆，语义完全不变（同样的条件对同一套房
    # 必然得出同样的结论），但重复的那些不再各查一次库。
    matched_cache = {}

    sent = 0
    for row in rows:
        if row.user_id == house.landlord_id:
            continue
        if house.house_no in row.notified():
            continue

        conditions = row.condition_dict()
        if not conditions:
            continue

        # 与列表接口同一条 SQL，再加一句「就这套房」。
        # 查得到就说明它满足用户存下的全部条件
        cache_key = row.conditions
        if cache_key not in matched_cache:
            stmt = house_query.apply_filters(
                select(House.id).where(House.id == house.id), conditions)
            matched_cache[cache_key] = session.scalar(stmt) is not None
        if not matched_cache[cache_key]:
            continue

        leasing.notify(
            session, row.user_id, "house",
            "有新房源符合「%s」" % row.name,
            "%s，%d 元/月。这是你保存的搜索条件匹配到的新房源。" % (
                house.title, int(float(house.rent_price))),
            house.house_no)

        row.notified_house_nos = ",".join(
            sorted(row.notified() | {house.house_no}))
        row.last_notified_at = datetime.now()
        sent += 1

    return sent
