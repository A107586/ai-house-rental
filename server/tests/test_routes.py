# -*- coding: utf-8 -*-
"""路由表本身的体检。

同一个「方法 + 路径」注册两次时，FastAPI 只会命中**先注册的那个**，
后一个既不报错也不生效。这和当初两个迁移都写成 0015 是同一种坑：
第二份安安静静地不干活，改它没有任何反应，而你会先怀疑缓存、
怀疑浏览器、怀疑代理，最后才怀疑到它头上。

实际发生过：`/api/houses/{house_no}/images` 写了两遍，
带注释、带 `_house_or_404` 的那份是死的。
"""

import unittest

from tests.base import app


class TestNoDuplicateRoutes(unittest.TestCase):

    def test_no_duplicate_method_and_path(self):
        seen = {}
        for route in app.routes:
            for method in getattr(route, "methods", None) or ():
                key = (method, getattr(route, "path", ""))
                name = getattr(getattr(route, "endpoint", None), "__name__", "?")
                seen.setdefault(key, []).append(name)

        dups = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertFalse(dups, "以下路由注册了不止一次，只有第一个会生效：\n" +
                         "\n".join("  %s %s → %s" % (m, p, names)
                                   for (m, p), names in sorted(dups.items())))

    def test_api_paths_are_unique_case_insensitively(self):
        """大小写只差一点的两条路径，排查起来同样费劲。"""
        lowered = {}
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/"):
                continue
            lowered.setdefault(path.lower(), set()).add(path)
        clashes = {k: v for k, v in lowered.items() if len(v) > 1}
        self.assertFalse(clashes, "大小写撞车：%s" % clashes)
