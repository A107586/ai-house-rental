"""房源举报（K-08）。

对标贝壳详情页那个常驻的「我要举报」，以及它背后整整一页的
《真房源/合规性，有问题赔百元》规则。

**为什么不复用 `content_flags`。** 那张表记的是机器审核
（`app/moderation.py`）自动拦下或标记的内容：有 `scene`、有命中的词、
没有举报人，也不需要给谁一个交代。这里记的是**人主动报上来的问题**：
有举报人、有证据、要有结论回执。两者的生命周期、权限和处理动作都不同，
塞进一张表只会让两边的字段互相为空。

单独成 router 而不是塞进 `main.py`，理由和 `support.py`、
`repair_data.py` 一样——那个文件曾经三千多行，谁都不想再往里加。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import leasing, uploads
from app.db import get_session
from app.deps import require_login
from app.models import House, HouseReport, User

router = APIRouter()


class ReportCreate(BaseModel):
    house_no: str
    kind: str
    description: str = Field(default="", max_length=500)
    # 证据图片 URL，走已有的 /api/upload。没有证据的举报基本处理不了，
    # 但也不能强制——「打电话过去说早租出去了」这种就是拿不出图
    evidence: list[str] = Field(default_factory=list)


class ReportHandle(BaseModel):
    status: str
    conclusion: str = Field(default="", max_length=300)


# 处理动作。**accepted 不等于删房源**——「属实」之后要做什么由管理员
# 另行决定（下架、联系房东整改、警告）。把「结论」和「处置」绑死，
# 会逼着管理员为了下架而把一条其实只是信息过期的举报判成「虚假房源」。
HANDLED = ("accepted", "rejected", "closed")


@router.post("/api/reports")
def create_report(payload: ReportCreate,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """提交一条举报。必须登录——匿名举报没法回执，也挡不住刷单。"""
    if payload.kind not in HouseReport.KINDS:
        raise HTTPException(status_code=422, detail="举报类型不正确")

    house = session.scalar(
        select(House).where(House.house_no == payload.house_no))
    if not house:
        raise HTTPException(status_code=404, detail="房源不存在")
    if house.landlord_id == user.id:
        raise HTTPException(status_code=400, detail="不能举报自己发布的房源")

    # 同一个人对同一套房只留一条待处理的举报。不拦的话，一个人连点五次
    # 就把管理员的待办列表刷满了，真正需要看的举报反而被埋掉
    exists = session.scalar(select(HouseReport).where(
        HouseReport.reporter_id == user.id,
        HouseReport.house_id == house.id,
        HouseReport.status == "pending"))
    if exists:
        raise HTTPException(
            status_code=409,
            detail="你对这套房源的举报正在处理中，请等待处理结果")

    # **证据图只收本系统上传的地址。**
    #
    # 房源相册、评价配图、报修照片三处都有这道校验，理由写在
    # `POST /api/houses/{no}/images` 上：放开成任意 URL，那个站点就能
    # 拿到每个访客的 IP 与访问时间，还能随时换掉图上的内容。
    #
    # 举报这条路更值得防——**看这张图的是管理员**，而提交的人
    # 恰恰是平台要审查的对象。原先这里一个字都不校验，
    # 实测 `https://evil.example.com/a.png` 直接入库并显示在管理端。
    evidence = []
    for url in payload.evidence[:6]:
        url = (url or "").strip()
        if not url:
            continue
        if not uploads.is_managed(url):
            raise HTTPException(status_code=400,
                                detail="证据图片只能用本系统上传的，请先上传再提交")
        if url not in evidence:
            evidence.append(url)

    report = HouseReport(
        report_no="JB%s%s" % (datetime.now().strftime("%y%m%d"),
                              uuid.uuid4().hex[:6].upper()),
        reporter_id=user.id,
        house_id=house.id,
        kind=payload.kind,
        description=payload.description.strip(),
        evidence=",".join(evidence),
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return {"ok": True, "report": report.to_dict()}


@router.get("/api/reports/mine")
def my_reports(user: User = Depends(require_login),
               session: Session = Depends(get_session)):
    """我举报过什么、处理得怎么样了。

    举报之后石沉大海，下次就没人愿意举报了——而举报量正是这套机制
    唯一的输入。
    """
    rows = session.scalars(
        select(HouseReport)
        .where(HouseReport.reporter_id == user.id)
        .order_by(HouseReport.created_at.desc())).all()
    return {"total": len(rows), "list": [r.to_dict() for r in rows]}


@router.get("/api/admin/reports")
def list_reports(status: str = "pending",
                 limit: int = Query(50, ge=1, le=200),
                 user: User = Depends(require_login),
                 session: Session = Depends(get_session)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查看举报")

    query = select(HouseReport)
    if status:
        query = query.where(HouseReport.status == status)
    rows = session.scalars(
        query.order_by(HouseReport.created_at.desc()).limit(limit)).all()
    pending = len(session.scalars(select(HouseReport).where(
        HouseReport.status == "pending")).all())
    return {"total": len(rows), "pending": pending,
            "list": [r.to_dict() for r in rows]}


@router.patch("/api/admin/reports/{report_no}")
def handle_report(report_no: str, payload: ReportHandle,
                  user: User = Depends(require_login),
                  session: Session = Depends(get_session)):
    """处理一条举报，并把结论通知举报人。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以处理举报")
    if payload.status not in HANDLED:
        raise HTTPException(status_code=422, detail="处理结果不正确")

    report = session.scalar(
        select(HouseReport).where(HouseReport.report_no == report_no))
    if not report:
        raise HTTPException(status_code=404, detail="举报不存在")
    if report.status != "pending":
        raise HTTPException(status_code=409, detail="这条举报已经处理过了")

    report.status = payload.status
    report.conclusion = payload.conclusion.strip()
    report.handler_id = user.id
    report.handled_at = datetime.now()

    verdict = {"accepted": "举报属实", "rejected": "举报不属实",
               "closed": "已关闭"}[payload.status]
    leasing.notify(
        session, report.reporter_id, "report",
        "举报处理结果：%s" % verdict,
        "你对房源 %s 的举报已处理。%s" % (
            report.house.house_no if report.house else "",
            report.conclusion or "感谢你的反馈。"),
        report.report_no)
    # 操作留痕不用在这里写：`app/oplog.py` 的中间件会记下所有 /api/ 上的
    # 写请求。只要在 ACTION_RULES 里给这条路径起个中文名，
    # 「谁把它判成属实的」在管理端的日志里就查得到。

    session.commit()
    session.refresh(report)
    return {"ok": True, "report": report.to_dict()}


@router.get("/api/report-kinds")
def report_kinds():
    """举报类型清单。前端不写死，加一类不用改两处。"""
    return {"list": [{"key": k, "label": v}
                     for k, v in HouseReport.KINDS.items()]}
