# -*- coding: utf-8 -*-
"""浏览器本地存的东西，不许不分账号。

## 这条为什么要盯

**实测的串号现场**：同一台电脑上退出租客、登录房东，助手面板
恢复出来的是租客那段对话——他找过哪几套房、预算多少、报修留的电话，
连 `pending` 里那张还没点的确认卡片（上面写着账单号和金额）都在。

后端这一侧一直是干净的：`GET /api/chat-messages` 同时按 `user_id`
过滤，别人的记录拿不走。**漏的是浏览器本地那一份**——
`rental_chat_draft`、`rental_chat_session`、`rental_search_history`
三个键都是写死的常量，一台电脑一份，跟谁登录没有关系；
`logout()` 也没清它们。

## 为什么是在 Python 里 grep 前端源码

因为前端现在没有测试框架（`web/package.json` 里只有 vite 和 react），
为这一条引一整套 jsdom + 测试运行器不划算。而这个不变式恰好
**在源码层面就能判**：本地存储的键必须经过 `scopedKey()`，
组件里不许再出现裸的 `localStorage`。

判得糙，但盯住的是真问题：**下一个人随手写一行
`localStorage.getItem('rental_chat_draft')`，这里就会红。**

令牌是例外，它有自己的一套隔离（`sessionStorage` 存「这个标签页
现在是谁」，`localStorage` 只存「上次用的是谁」），全在 `api.js` 里，
所以那个文件不在检查范围内。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "web", "src")

# 这几个文件允许直接碰 localStorage：
#   storage.js  —— 隔离本身就在这里实现
#   api.js      —— 令牌有自己的一套，见模块头注释
ALLOWED = {
    os.path.join(WEB, "storage.js"),
    os.path.join(WEB, "api.js"),
}

# 按账号分开存的那几个键。值是「在哪个文件里定义」，
# 顺便盯住它们没有被搬到别处去。
SCOPED_KEYS = {
    "rental_chat_draft": "components/ChatPanel.jsx",
    "rental_chat_session": "components/ChatPanel.jsx",
    "rental_search_history": "useSearchHistory.js",
}

_STORAGE_CALL = re.compile(r"localStorage\s*\.\s*(getItem|setItem|removeItem)")


def _sources():
    for base, _dirs, files in os.walk(WEB):
        for name in files:
            if name.endswith((".js", ".jsx")):
                yield os.path.join(base, name)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestNoBareLocalStorage(unittest.TestCase):

    def test_only_storage_js_and_api_js_touch_localstorage(self):
        """别的文件一律走 storage.js，那里才有按账号分开的键。"""
        offenders = []
        for path in _sources():
            if path in ALLOWED:
                continue
            for line_no, line in enumerate(_read(path).splitlines(), 1):
                if _STORAGE_CALL.search(line):
                    offenders.append("%s:%d  %s"
                                     % (os.path.relpath(path, ROOT),
                                        line_no, line.strip()))
        self.assertEqual(
            offenders, [],
            "这些地方直接读写了 localStorage，绕过了按账号隔离：\n  "
            + "\n  ".join(offenders)
            + "\n请改用 storage.js 的 scopedKey / readJSON / writeJSON。")


class TestScopedKeys(unittest.TestCase):

    def test_every_per_account_key_goes_through_scopedkey(self):
        """三个键都要被 `scopedKey(...)` 包一层才拿去用。"""
        for key, rel in SCOPED_KEYS.items():
            path = os.path.join(WEB, *rel.split("/"))
            self.assertTrue(os.path.exists(path), path)
            text = _read(path)
            self.assertIn(key, text, "%s 里找不到 %s" % (rel, key))
            self.assertIn("scopedKey", text,
                          "%s 用了 %s，却没有经过 scopedKey" % (rel, key))

    def test_storage_module_keys_identity_not_just_login_state(self):
        """未登录也要有自己的一份，不能和任何一个登录账号混在一起。"""
        text = _read(os.path.join(WEB, "storage.js"))
        self.assertIn("anon", text)
        # `0` 是合法的用户 id，用 `||` 兜底会把它当成未登录
        self.assertNotIn("user?.id ||", text)

    def test_legacy_keys_are_purged_on_startup(self):
        """旧的不带身份的键要删掉——**那是在清数据，不是清代码**。

        不删的话，泄出去的那段对话会一直留在每个用户的浏览器里，
        直到有人手动清缓存。
        """
        storage = _read(os.path.join(WEB, "storage.js"))
        for key in SCOPED_KEYS:
            self.assertIn("'%s'" % key, storage,
                          "storage.js 的 LEGACY_KEYS 少了 %s" % key)
        entry = _read(os.path.join(WEB, "main.jsx"))
        self.assertIn("purgeLegacy()", entry,
                      "应用启动时没有调 purgeLegacy()，旧键不会被清掉")


class TestChatPanelRemountsOnIdentityChange(unittest.TestCase):
    """换账号时助手面板要整个重挂。

    只改存储的键还不够：React 不会因为 `user` 变了就丢掉 state，
    上一个人的消息仍然留在 `messages` 里。挂一个 `key={identity}`
    才会让组件卸载重建，几个 `useState` 初始化函数带着新身份重跑一遍。
    """

    def test_chatpanel_is_keyed_by_identity(self):
        app = _read(os.path.join(WEB, "App.jsx"))
        index = app.find("<ChatPanel")
        self.assertNotEqual(index, -1, "App.jsx 里找不到 ChatPanel")
        # 取开标签这一段来看，别把后面无关的 key= 算进来
        opening = app[index:index + 400]
        self.assertIn("key={identity}", opening,
                      "ChatPanel 没有按身份挂 key，换账号时旧消息会留在界面上")
        self.assertIn("whoami(user)", app,
                      "identity 应当由 storage.js 的 whoami 算出来")


if __name__ == "__main__":
    unittest.main()
