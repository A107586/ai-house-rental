"""维修数据：人员、物品、结案登记与统计（F-09）。

从 `main.py` 拆出来，见 `app/routers/__init__.py` 的说明。

这一组的请求模型也放在这里而不是 `schemas.py`——它们**只被这一组用**，
搬到公共文件反而要在两个文件之间来回跳。跨模块共享的才值得集中。
"""


from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import leasing
from app.db import get_session
from app.deps import require_login, require_staff
from app.models import (House, Repair, RepairItem, RepairItemUsage, RepairRecord,
                        RepairWorker, User)

router = APIRouter()



class WorkerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(default="", max_length=20)
    skill: str = Field(default="", max_length=30)


class WorkerUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    skill: str | None = None
    status: str | None = None


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    unit: str = Field(default="个", max_length=10)
    unit_price: float = Field(default=0, ge=0)
    stock: int = Field(default=0, ge=0)


class ItemUpdate(BaseModel):
    name: str | None = None
    unit: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)


class UsageIn(BaseModel):
    item_id: int
    quantity: int = Field(default=1, ge=1)


class RecordCreate(BaseModel):
    worker_id: int | None = None
    hours: float = Field(default=0, ge=0)
    labor_cost: float = Field(default=0, ge=0)
    note: str = Field(default="", max_length=500)
    items: list[UsageIn] = []


def _can_handle_repair(user, repair):
    """能处理这张工单的人：管理员，或该房源的房东。"""
    if user.role == "admin":
        return True
    return bool(repair.house and repair.house.landlord_id == user.id)


# ---- 维修人员 ----

@router.get("/api/repair-workers")
def list_workers(user: User = Depends(require_staff),
                 session: Session = Depends(get_session)):
    """维修人员清单。房东派单也要看得到，所以对房东开放。"""
    rows = session.scalars(select(RepairWorker).order_by(RepairWorker.id)).all()
    return {"total": len(rows), "list": [row.to_dict() for row in rows]}


@router.post("/api/repair-workers")
def create_worker(payload: WorkerCreate, user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以维护维修人员")
    worker = RepairWorker(name=payload.name, phone=payload.phone,
                          skill=payload.skill)
    session.add(worker)
    session.commit()
    session.refresh(worker)
    return {"ok": True, "worker": worker.to_dict()}


@router.patch("/api/repair-workers/{worker_id}")
def update_worker(worker_id: int, payload: WorkerUpdate,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以维护维修人员")
    worker = session.get(RepairWorker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="维修人员不存在")
    if payload.status is not None and payload.status not in ("active", "off"):
        raise HTTPException(status_code=400, detail="状态值不合法")
    for field in ("name", "phone", "skill", "status"):
        value = getattr(payload, field)
        if value is not None:
            setattr(worker, field, value)
    session.commit()
    return {"ok": True, "worker": worker.to_dict()}


# ---- 维修物品 ----

@router.get("/api/repair-items")
def list_items(user: User = Depends(require_staff),
               session: Session = Depends(get_session)):
    rows = session.scalars(select(RepairItem).order_by(RepairItem.id)).all()
    return {"total": len(rows), "list": [row.to_dict() for row in rows]}


@router.post("/api/repair-items")
def create_item(payload: ItemCreate, user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以维护维修物品")
    item = RepairItem(name=payload.name, unit=payload.unit,
                      unit_price=payload.unit_price, stock=payload.stock)
    session.add(item)
    session.commit()
    session.refresh(item)
    return {"ok": True, "item": item.to_dict()}


@router.patch("/api/repair-items/{item_id}")
def update_item(item_id: int, payload: ItemUpdate,
                user: User = Depends(require_login),
                session: Session = Depends(get_session)):
    """修改物品。调价只影响以后的领用，历史记录里存的是当时的单价快照。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以维护维修物品")
    item = session.get(RepairItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="物品不存在")
    for field in ("name", "unit", "unit_price", "stock"):
        value = getattr(payload, field)
        if value is not None:
            setattr(item, field, value)
    session.commit()
    return {"ok": True, "item": item.to_dict()}


# ---- 维修记录 ----

@router.post("/api/repairs/{repair_id}/record")
def create_record(repair_id: int, payload: RecordCreate,
                  user: User = Depends(require_staff),
                  session: Session = Depends(get_session)):
    """登记维修记录并结案（F-09）。

    一张工单只能结案一次——重复提交会把物料重复扣一遍库存、成本也重复
    计一遍，统计就废了。数据库上有唯一约束兜底，这里先给出可读的报错。
    """
    repair = session.get(Repair, repair_id)
    if not repair:
        raise HTTPException(status_code=404, detail="工单不存在")
    if not _can_handle_repair(user, repair):
        raise HTTPException(status_code=403, detail="只能处理自己房源的工单")

    exists = session.scalar(
        select(RepairRecord).where(RepairRecord.repair_id == repair.id))
    if exists:
        raise HTTPException(status_code=409, detail="该工单已登记过维修记录")

    worker = None
    if payload.worker_id:
        worker = session.get(RepairWorker, payload.worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="维修人员不存在")
        if worker.status != "active":
            # 停用的人员不该再出现在新的维修记录上（工作量统计也会跟着算错）
            raise HTTPException(status_code=409, detail="%s 已停用，不能再派工" % worker.name)

    # **同一物品先合并数量再核库存。** 原先逐行核：同一个水龙头分两行各填 6、
    # 库存 10，两行各自都「够」，扣完库存是 -2（2026-10-22 实测）
    wanted = {}
    for line in payload.items:
        wanted[line.item_id] = wanted.get(line.item_id, 0) + line.quantity

    # 先把物料全部校验完再落库：库存不足要整单拒绝，
    # 不能扣了一半才发现最后一样不够
    planned = []
    for item_id, quantity in wanted.items():
        item = session.get(RepairItem, item_id)
        if not item:
            raise HTTPException(status_code=404,
                                detail="物品 %d 不存在" % item_id)
        if item.stock < quantity:
            raise HTTPException(
                status_code=409,
                detail="%s 库存不足（剩 %d %s，需 %d %s）"
                       % (item.name, item.stock, item.unit,
                          quantity, item.unit))
        planned.append((item, quantity))

    record = RepairRecord(
        repair_id=repair.id,
        worker_id=worker.id if worker else None,
        hours=payload.hours, labor_cost=payload.labor_cost,
        note=payload.note)
    session.add(record)
    session.flush()

    item_cost = 0.0
    for item, quantity in planned:
        price = float(item.unit_price or 0)
        session.add(RepairItemUsage(
            record_id=record.id, item_id=item.id, item_name=item.name,
            # 单价快照：物品调价之后，历史维修的成本不能跟着一起变
            quantity=quantity, unit_price=price))
        item.stock -= quantity
        item_cost += price * quantity

    record.item_cost = round(item_cost, 2)
    repair.status = "done"
    # 结案要告诉报修的人——走状态接口改成「已完成」会通知，走这里登记结案原先不会
    if repair.tenant_id:
        leasing.notify(session, repair.tenant_id, "repair", "报修已处理完成",
                       "你提交的报修（%s）已处理完成。" % repair.content[:20])
    session.commit()
    session.refresh(record)
    return {"ok": True, "record": record.to_dict()}


@router.get("/api/repair-records")
def list_records(limit: int = Query(50, ge=1, le=200),
                 user: User = Depends(require_staff),
                 session: Session = Depends(get_session)):
    stmt = select(RepairRecord)
    if user.role != "admin":
        # 房东只看自己房源的维修记录。**先筛再取前 N 条**：原先先取全平台最近
        # N 条再在内存里筛，别的房东记录一多，这里就只剩几条甚至一条都没有
        mine = select(Repair.id).join(House, Repair.house_id == House.id).where(
            House.landlord_id == user.id)
        stmt = stmt.where(RepairRecord.repair_id.in_(mine))
    rows = session.scalars(
        stmt.order_by(RepairRecord.finished_at.desc()).limit(limit)).all()
    return {"total": len(rows), "list": [row.to_dict() for row in rows]}


@router.get("/api/repair-stats")
def repair_stats(user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    """维修数据统计（F-09）。

    四个口径：物品消耗、人员工作量、故障类型分布、报修最多的房源。
    最后一项才是这份统计真正的用处——同一套房反复报修，说明的往往不是
    「又坏了一次」，而是上一次没修好，或者房子本身有问题。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看维修统计")

    records = session.scalars(select(RepairRecord)).all()
    repairs = session.scalars(select(Repair)).all()

    by_item = {}
    for record in records:
        for usage in record.usages:
            row = by_item.setdefault(
                usage.item_name,
                {"name": usage.item_name, "quantity": 0, "amount": 0.0})
            row["quantity"] += usage.quantity
            row["amount"] += usage.amount

    by_worker = {}
    for record in records:
        name = record.worker.name if record.worker else "未指派"
        row = by_worker.setdefault(
            name, {"name": name, "count": 0, "hours": 0.0, "cost": 0.0})
        row["count"] += 1
        row["hours"] += float(record.hours or 0)
        row["cost"] += record.total_cost

    # 故障类型分布统计的是全部工单而非已结案的，
    # 才看得出真实的报修构成
    by_category = {}
    for repair in repairs:
        name = repair.category or "未分类"
        by_category[name] = by_category.get(name, 0) + 1

    by_house = {}
    for repair in repairs:
        if not repair.house:
            continue
        no = repair.house.house_no
        row = by_house.setdefault(
            no, {"house_no": no, "title": repair.house.title, "count": 0})
        row["count"] += 1

    total_cost = sum(record.total_cost for record in records)
    return {
        "repair_total": len(repairs),
        "record_total": len(records),
        "pending": len([r for r in repairs if r.status != "done"]),
        "total_cost": round(total_cost, 2),
        "avg_cost": round(total_cost / len(records), 2) if records else 0,
        "by_item": sorted(
            [dict(row, amount=round(row["amount"], 2))
             for row in by_item.values()],
            key=lambda row: -row["amount"]),
        "by_worker": sorted(
            [dict(row, hours=round(row["hours"], 1),
                  cost=round(row["cost"], 2)) for row in by_worker.values()],
            key=lambda row: -row["count"]),
        "by_category": sorted(
            [{"category": name, "count": count}
             for name, count in by_category.items()],
            key=lambda row: -row["count"]),
        "top_houses": sorted(by_house.values(),
                             key=lambda row: -row["count"])[:8],
    }
