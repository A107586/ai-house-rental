# -*- coding: utf-8 -*-
"""交付文档不许和代码分叉。

## 为什么要用测试来盯文档

因为靠人记得是盯不住的。**这次做数据库设计与概要设计时就撞见了现场**：

- 《数据库设计说明书》第三章少了 `role_requests` 整张表，
  还少了 `users.accept_contact`、`houses.building` / `unit`、
  `reviews` 的五个字段——那一章本来就是生成的，只是模型改了没人重跑；
- 《技术架构说明》里写着「145 个接口 / 35 张表」，实际是 150 / 36；
- 概要设计初稿手写了三条**根本不存在**的接口路径，
  还有一张模块统计表，九个模块加起来 160，而全站只有 150。

这几处没有一处会让程序出错，所以永远不会有人发现——
**直到评审时有人照着文档去调那个 404 的接口。**

## 这里跑的是什么

四个生成脚本的 `--check`。它们各自重新生成一遍，和已提交的文件比对：

| 脚本 | 盯着 |
|---|---|
| `gen_db_sql.py --check` | 两份建库脚本与模型一致 |
| `gen_db_xlsx.py --check` | 交付用数据库设计书与模型一致 |
| `gen_api_list.py --check` | 概要设计的接口清单与模块统计表最新，**且正文引用的接口真的存在** |
| `gen_db_doc.py`（比对模式） | 《数据库设计说明书》第三章与模型一致 |

改了模型或路由之后，这几条会红。**红的时候不要改测试，去重跑生成脚本。**

## 为什么放在 server 的测试套件里

这些文档描述的是业务后端的结构，改动也总是从这里开始。
放在这里，改完模型跑一次 `python -m unittest discover -s tests -t .` 就会被提醒。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts")


def run(script, *args):
    """跑一个生成脚本，返回 (退出码, 输出)。

    **在子进程里跑**：这些脚本会 `sys.path.insert` 并导入两个不同项目的
    `app` 包，在测试进程里直接导会把 server 的 `app` 换掉，
    后面的用例就全乱了。
    """
    done = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script)] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return done.returncode, (done.stdout or "") + (done.stderr or "")


class TestGeneratedArtifactsAreFresh(unittest.TestCase):

    def test_build_scripts_match_the_models(self):
        """两份建库脚本必须和模型一致。

        它们是交付物里「能直接建库」的那一份，**过期的建库脚本比没有更糟**：
        照着它建出来的库少一张表，而问题要到运行时才暴露。
        """
        code, output = run("gen_db_sql.py", "--check")
        self.assertEqual(code, 0, output)

    def test_database_design_workbook_matches_the_models(self):
        code, output = run("gen_db_xlsx.py", "--check")
        self.assertEqual(code, 0, output)

    def test_outline_api_list_is_fresh_and_has_no_phantom_endpoints(self):
        """概要设计正文里引用的接口必须真的存在。

        「不存在的接口」这一条比「清单过期」更值得盯：清单过期只是少几行，
        而正文里写了一条 404 的接口，是**在指着人往沟里走**。
        """
        code, output = run("gen_api_list.py", "--check")
        self.assertEqual(code, 0, output)

    def test_database_doc_chapter_matches_the_models(self):
        """《数据库设计说明书》第三章是生成的，不能和模型对不上。

        `gen_db_doc.py` 本身没有 `--check`（它只会重写那一章），
        所以这里的做法是：跑一遍，然后问 git 那个文件有没有变。
        变了就说明提交进去的那一版是旧的。
        """
        doc = os.path.join("docs", "数据库设计说明书.md")
        before = subprocess.run(["git", "diff", "--quiet", "--", doc],
                                cwd=ROOT).returncode
        if before != 0:
            self.skipTest("这份文档当前有未提交的改动，跳过——"
                          "此时比对不出「生成器是否与模型一致」")
        code, output = run("gen_db_doc.py")
        self.assertEqual(code, 0, output)
        changed = subprocess.run(["git", "diff", "--quiet", "--", doc],
                                 cwd=ROOT).returncode
        if changed != 0:
            subprocess.run(["git", "checkout", "--", doc], cwd=ROOT)
            self.fail("《数据库设计说明书》第三章与模型对不上了。"
                      "重跑：python scripts/gen_db_doc.py")


if __name__ == "__main__":
    unittest.main()
