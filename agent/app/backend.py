"""业务后端 HTTP 客户端。

智能体不直接连数据库，一律通过后端受控接口取数
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from app import tracing
from app.config import settings

_MOMENT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                   "%Y-%m-%d %H:%M", "%Y-%m-%d")


class BackendError(Exception):
    """后端接口调用失败。"""


def parse_moment(raw):
    """兼容多种时间格式；纯日期按当天 10:00 处理。"""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in _MOMENT_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(hour=10) if fmt == "%Y-%m-%d" else parsed
    return None


class BackendClient(object):

    def __init__(self, base_url=None, timeout=None):
        self.base_url = (base_url or settings.backend_base_url).rstrip("/")
        self.timeout = timeout or settings.backend_timeout

    # ------------------------------------------------------------ 底层

    def _request(self, method, path, params=None, payload=None, token=None):
        # **测试模式下不碰网络。**
        #
        # 项目一直说「测试不依赖网络与数据库」，但那句话原先是靠
        # 「跑测试的机器上恰好起着后端」成立的：后端一关，每次调用都要
        # 等一个连接失败，542 条用例从 0.6 秒变成 41 秒。
        #
        # 要测后端交互的用例本来就该注入假后端（RecordingBackend、
        # FakeBackend 满地都是），模块级的这个 client 在测试里不该有真 I/O。
        # 直接抛 BackendError——所有调用点本来就都处理了这个异常。
        if settings.under_test:
            raise BackendError("测试模式：不访问业务后端")

        url = self.base_url + "/" + path.lstrip("/")
        if params:
            # 过滤空值，避免后端把空串当成有效筛选条件
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = None
        headers = {"Accept": "application/json"}
        # 透传用户令牌。推荐、账单这类接口要读「当前用户自己的」数据，
        # 智能体不持有身份，只能把前端带来的令牌原样转发。
        if token:
            headers["Authorization"] = "Bearer " + token
        # 带上这一轮的请求编号，业务后端沿用它记日志（app/tracing.py）
        if tracing.current():
            headers[tracing.HEADER] = tracing.current()
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:
                pass
            finally:
                exc.close()
            # 422 的 detail 是校验错误列表，原样塞进去用户会看到一串 Python 字面量
            if not isinstance(detail, str):
                detail = ""
            raise BackendError(detail or ("后端返回 HTTP %s" % exc.code))
        except Exception as exc:
            raise BackendError("无法连接业务后端（%s）：%s"
                               % (type(exc).__name__, self.base_url))

    def get(self, path, params=None, token=None):
        return self._request("GET", path, params=params, token=token)

    def post(self, path, payload, token=None):
        return self._request("POST", path, payload=payload, token=token)

    # ------------------------------------------------------------ 推荐所需数据

    def list_history(self, token):
        """用户浏览足迹。未登录或接口不可用时返回空列表。"""
        if not token:
            return []
        try:
            return self.get("/api/history", {"limit": 30}, token=token).get("list") or []
        except BackendError:
            return []

    def list_favorites(self, token):
        if not token:
            return []
        try:
            return self.get("/api/favorites", token=token).get("list") or []
        except BackendError:
            return []

    def list_candidates(self, params=None, limit=100):
        """候选房源池。"""
        body = self.get("/api/houses", dict(params or {}, limit=limit))
        return body.get("list") or []

    def stats(self, token=None):
        """平台概况。

        **带上令牌**：`/api/stats` 分两档返回，不带令牌只给房源侧的
        公开数字（总数、均价、分区分布），钱和工单那一档要管理员身份。
        AI-05 的经营分析本来就只在管理后台里看，令牌由调用方透传过来。
        """
        return self.get("/api/stats", token=token)

    # ------------------------------------------------------------ 客服知识库

    def list_notices(self):
        """读取当前已发布公告，作为 AI-03 的受控知识库。"""
        body = self.get("/api/notices")
        return body.get("list") or []

    # ------------------------------------------------------------ 会话记忆

    def list_chat(self, session_id, token, limit=30):
        """读取会话记录。未登录、或后端不可用时退回空列表——
        记不住上下文只是体验降级，不该让整轮对话失败。"""
        if not token or not session_id or settings.under_test:
            return []
        try:
            body = self.get("/api/chat-messages",
                            {"session_id": session_id, "limit": limit},
                            token=token)
            return body.get("list") or []
        except BackendError:
            return []

    def log_chat(self, session_id, role, content, token, intent=""):
        """落库一轮对话。失败静默——记录写不进去不该拖垮回答。

        **测试模式下直接跳过。** 这两个调用是纯记录性的，答案对不对
        跟它们无关；但后端没起来的时候，每一次都要等一个连接失败，
        实测把 542 条用例从 0.6 秒拖到 41 秒。项目一直说「测试不依赖
        网络与数据库」——那句话原先是靠「本机恰好起着后端」成立的。
        """
        if not token or not session_id or settings.under_test:
            return False
        try:
            self.post("/api/chat-messages",
                      {"session_id": session_id, "role": role,
                       "content": content, "intent": intent}, token=token)
            return True
        except BackendError:
            return False

    # ------------------------------------------------------------ 房源

    def list_properties(self, params):
        """查询房源列表，返回 ``{"list": [...], "total": n}``。"""
        body = self.get("/api/houses", params)
        return {"list": body.get("list") or [], "total": body.get("total") or 0}

    # ------------------------------------------------------------ 预约

    def list_viewings(self, house_no):
        """查询某房源已占用的看房时刻，返回 `datetime` 列表。"""
        if not house_no:
            return []
        body = self.get("/api/appointments", {"house_no": house_no})
        moments = []
        for row in body.get("list") or []:
            parsed = parse_moment(row.get("viewing_time"))
            if parsed:
                moments.append(parsed)
        return moments

    def create_viewing(self, house_no, moment, applicant, token=None):
        """提交看房预约。"""
        applicant = applicant or {}
        return self.post("/api/appointments", {
            "house_no": house_no,
            "viewing_time": moment.strftime("%Y-%m-%dT%H:%M:%S"),
            "contact_name": applicant.get("xingming") or applicant.get("contact_name") or "",
            "contact_phone": applicant.get("shouji") or applicant.get("contact_phone") or "",
            "remark": applicant.get("beizhu") or "",
        }, token=token)

    # ------------------------------------------------------------ 报修

    def create_repair(self, house_no, content, category="", urgency="",
                      assignee="", analysis="", applicant=None):
        """提交报修工单。分类结果由智能体分析得出，一并写入。"""
        applicant = applicant or {}
        return self.post("/api/repairs", {
            "house_no": house_no,
            "content": content,
            "category": category,
            "urgency": urgency,
            "assignee": assignee,
            "analysis": analysis,
            "contact_name": applicant.get("xingming") or applicant.get("contact_name") or "",
            "contact_phone": applicant.get("shouji") or applicant.get("contact_phone") or "",
        })

    # ------------------------------------------------------------ AI-03 客服

    def list_faqs(self):
        """客服知识条目。拿不到时调用方退回本地 Markdown 语料。"""
        data = self.get("/api/faqs")
        return data.get("list", []) if isinstance(data, dict) else (data or [])

    def log_support(self, payload, token=None):
        """回写一次客服问答。

        **写失败绝不能影响回答**——日志是运营用的，用户等的是答案。
        与 `log_chat` 同样的处理：吞掉异常，返回 None。
        """
        try:
            data = self.post("/api/support/logs", payload, token=token)
        except Exception:
            return None
        return data.get("log_id") if isinstance(data, dict) else None

    def create_handoff(self, question, reason="", context="", token=None):
        """建一条转人工工单。需要登录，未登录时后端会返回 401。"""
        try:
            return self.post("/api/support/handoff",
                             {"question": question, "reason": reason,
                              "context": context}, token=token)
        except Exception:
            return None


# 模块级单例，便于测试时整体替换
client = BackendClient()
