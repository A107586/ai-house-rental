# -*- coding: utf-8 -*-
"""上线自检。

**这一组盯的是「带着开发默认值上公网」这件事。** 代码是公开的，
默认密钥谁都看得到：拿 `AUTH_SECRET` 能自己签一个管理员令牌，
拿 `PAYMENT_SIGN_SECRET` 能伪造支付回调把账单改成已付。

原先只在 `/health` 里返回一句 `auth_secret_is_default: true`。
警告是会被滚上去的，而且它顺带把「这台机器用的是公开默认密钥」
告诉了每一个访问者。现在改成生产环境直接拒绝启动。

本地开发不受影响——不设 `APP_ENV=production` 就什么都不查。
"""

import unittest

from app import prodcheck

GOOD = {
    "APP_ENV": "production",
    "AUTH_SECRET": "x" * 48,
    "PAYMENT_SIGN_SECRET": "y" * 48,
    "DATABASE_URL": "mysql+pymysql://u:p@db:3306/rental?charset=utf8mb4",
    "CORS_ORIGINS": "https://rent.example.com",
    "TRUSTED_PROXY_HOPS": "1",
}


class TestProductionGate(unittest.TestCase):

    def test_development_is_not_blocked(self):
        """**开发环境一行配置都不用写。** 检查再有道理，
        也不该让组员拉下代码跑不起来。

        `problems()` 本身不看环境——它回答的是「按上线标准衡量还差什么」，
        `preflight_prod.py` 正是拿它在部署前预演的。
        拦不拦启动由 `enforce()` 决定，而它只在生产环境拦。
        """
        self.assertTrue(prodcheck.problems({}))      # 按上线标准确实不合格
        prodcheck.enforce({})                        # 但开发环境照常放行

    def test_full_config_passes(self):
        self.assertEqual(prodcheck.problems(GOOD), [])
        prodcheck.enforce(GOOD)

    def test_default_auth_secret_is_rejected(self):
        env = dict(GOOD, AUTH_SECRET=prodcheck.DEV_AUTH_SECRET)
        self.assertTrue(any("AUTH_SECRET" in p for p in prodcheck.problems(env)))

    def test_missing_auth_secret_is_rejected(self):
        env = dict(GOOD)
        env.pop("AUTH_SECRET")
        self.assertTrue(any("AUTH_SECRET" in p for p in prodcheck.problems(env)))

    def test_short_secret_is_rejected(self):
        """**短密钥比没密钥更糟**：它看起来像配过了。"""
        env = dict(GOOD, AUTH_SECRET="abc123")
        self.assertTrue(any("太短" in p for p in prodcheck.problems(env)))

    def test_default_payment_secret_is_rejected(self):
        env = dict(GOOD, PAYMENT_SIGN_SECRET=prodcheck.DEV_PAYMENT_SECRET)
        self.assertTrue(any("PAYMENT_SIGN_SECRET" in p
                            for p in prodcheck.problems(env)))

    def test_sqlite_default_is_rejected(self):
        env = dict(GOOD)
        env.pop("DATABASE_URL")
        self.assertTrue(any("DATABASE_URL" in p for p in prodcheck.problems(env)))

    def test_sqlite_url_is_rejected_too(self):
        """写成 SQLite 和不写一样糟，**甚至更糟**——它看起来像是特意选的。"""
        env = dict(GOOD, DATABASE_URL="sqlite:///./rental.db")
        self.assertTrue(any("SQLite" in p for p in prodcheck.problems(env)))

    def test_localhost_cors_is_rejected(self):
        """线上放行 localhost 没有意义，只会留一个口子。"""
        env = dict(GOOD, CORS_ORIGINS="https://rent.example.com,http://localhost:5173")
        self.assertTrue(any("localhost" in p for p in prodcheck.problems(env)))

    def test_proxy_hops_must_be_stated_explicitly(self):
        """**不许「忘了设」**：限流与操作日志都按这个值取客户端 IP。

        裸跑不带代理也要显式写 0，这样至少说明是想过的。
        """
        env = dict(GOOD)
        env.pop("TRUSTED_PROXY_HOPS")
        self.assertTrue(any("TRUSTED_PROXY_HOPS" in p
                            for p in prodcheck.problems(env)))
        self.assertEqual(prodcheck.problems(dict(GOOD, TRUSTED_PROXY_HOPS="0")), [])

    def test_enforce_raises_with_every_problem_listed(self):
        """报错要一次列全。

        一次只报一条的话，上线的人得改一条、重启一次、再看下一条——
        五条配置能来回折腾五轮。
        """
        env = {"APP_ENV": "production"}
        with self.assertRaises(RuntimeError) as caught:
            prodcheck.enforce(env)
        message = str(caught.exception)
        for key in ("AUTH_SECRET", "PAYMENT_SIGN_SECRET", "DATABASE_URL",
                    "CORS_ORIGINS", "TRUSTED_PROXY_HOPS"):
            self.assertIn(key, message)

    def test_prod_alias_is_accepted(self):
        self.assertTrue(prodcheck.is_production({"APP_ENV": "prod"}))
        self.assertTrue(prodcheck.is_production({"APP_ENV": "Production"}))
        self.assertFalse(prodcheck.is_production({"APP_ENV": "staging"}))


if __name__ == "__main__":
    unittest.main()
