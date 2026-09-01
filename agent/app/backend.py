"""业务后端（SpringBoot）HTTP 客户端。

智能体不直接连数据库，一律通过后端受控接口取数
（对应非功能需求 N-06：禁止拼接执行任意 SQL）。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings


class BackendError(Exception):
    """后端接口调用失败。"""


class BackendClient(object):

    def __init__(self, base_url=None, timeout=None):
        self.base_url = (base_url or settings.backend_base_url).rstrip("/")
        self.timeout = timeout or settings.backend_timeout

    def get(self, path, params=None):
        """发起 GET 请求，返回后端 `R` 包装体解析后的 dict。"""
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            # 过滤掉空值，避免后端把空串当成有效筛选条件
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BackendError("后端返回 HTTP %s：%s" % (exc.code, path))
        except Exception as exc:
            raise BackendError("无法连接业务后端（%s）：%s" % (type(exc).__name__, self.base_url))

    def list_properties(self, params):
        """查询房源列表。

        后端 `R` 的结构是 ``{"code":0,"data":{"list":[...],"total":n}}``，
        这里统一拆包，让上层拿到干净的列表与总数。
        """
        body = self.get("/fangwuxinxi/list", params)
        data = body.get("data") or {}
        if isinstance(data, list):
            return {"list": data, "total": len(data)}
        return {
            "list": data.get("list") or [],
            "total": data.get("total") or 0,
        }


# 模块级单例，便于测试时整体替换
client = BackendClient()
