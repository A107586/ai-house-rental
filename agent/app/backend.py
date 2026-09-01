"""业务后端 HTTP 客户端。

智能体不直接连数据库，一律通过后端受控接口取数
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

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

    def _request(self, method, path, params=None, payload=None):
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            # 过滤空值，避免后端把空串当成有效筛选条件
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = None
        headers = {"Accept": "application/json"}
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
            raise BackendError(detail or ("后端返回 HTTP %s" % exc.code))
        except Exception as exc:
            raise BackendError("无法连接业务后端（%s）：%s"
                               % (type(exc).__name__, self.base_url))

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, payload):
        return self._request("POST", path, payload=payload)

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

    def create_viewing(self, house_no, moment, applicant):
        """提交看房预约。"""
        applicant = applicant or {}
        return self.post("/api/appointments", {
            "house_no": house_no,
            "viewing_time": moment.strftime("%Y-%m-%dT%H:%M:%S"),
            "contact_name": applicant.get("xingming") or applicant.get("contact_name") or "",
            "contact_phone": applicant.get("shouji") or applicant.get("contact_phone") or "",
            "remark": applicant.get("beizhu") or "",
        })


# 模块级单例，便于测试时整体替换
client = BackendClient()
