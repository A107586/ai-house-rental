# -*- coding: utf-8 -*-
"""上线前预演：把该检查的一次检查完，不合格就别发。

    # 读一个环境变量文件来检查（推荐：正式部署用的那一份）
    python scripts/preflight_prod.py --env-file /etc/rental/rental.env

    # 或者检查当前 shell 里已经导出的变量
    python scripts/preflight_prod.py

## 和 `app/prodcheck.py` 的分工

`prodcheck` 在**服务启动时**把关，不合格拒绝启动——那是最后一道闸。
这个脚本是**发布之前**先跑一遍，把「等到启动才发现」提前到「还没上线就知道」。

两者共用同一份判据（这个脚本直接调 `prodcheck.problems`），
所以不会出现「预演说行、启动说不行」。

## 它还多查四件事

启动时查不了、或查了代价太大的：

1. **数据库连得上吗**——连接串写错、密码错、库没建、防火墙没放行，
   这四种都要等到第一个请求才暴露；
2. **管理员口令还是种子里的默认值吗**——`admin / admin123` 写在 README 里，
   公网上线第一件被试的就是它；
3. **前端构建产物在不在**——`web/dist` 没构建，访问首页只会看到一句 Not Found；
4. **建库脚本与模型是否一致**——拿一份过期的 SQL 去建生产库，
   少的那张表要到功能报错时才被发现。
"""

import argparse
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from app import prodcheck  # noqa: E402


def read_env_file(path):
    """读 `KEY=VALUE` 形式的环境变量文件。

    只认最朴素的写法：`#` 开头是注释，第一个 `=` 之前是键。
    不做变量展开——展开规则各家 shell 不一样，**这里猜错就等于检查了个寂寞**。
    """
    env = {}
    with io.open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def check_database(env):
    url = env.get("DATABASE_URL") or ""
    if not url:
        return ["（数据库连通性没法查：DATABASE_URL 都还没设）"]
    try:
        import sqlalchemy
        engine = sqlalchemy.create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        return []
    except Exception as exc:                                  # noqa: BLE001
        return ["连不上数据库：%s" % str(exc).splitlines()[0][:160]]


def check_admin_password(env):
    """管理员口令还是种子默认值的话，等于门没锁。"""
    url = env.get("DATABASE_URL") or ""
    if not url:
        return []
    try:
        import sqlalchemy
        from app.security import verify_password
        engine = sqlalchemy.create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(sqlalchemy.text(
                "SELECT password_hash FROM users WHERE username='admin'")).first()
        if row and verify_password("admin123", row[0]):
            return ["管理员口令还是种子里的 admin123（README 里写着），"
                    "上线前必须改掉。"]
        return []
    except Exception:                                         # noqa: BLE001
        # 库里还没有 users 表属于正常（还没建库），不当成问题报
        return []


def check_frontend_build():
    index = os.path.join(ROOT, "web", "dist", "index.html")
    if not os.path.exists(index):
        return ["前端还没构建：web/dist/index.html 不存在。"
                "执行 cd web && npm ci && npm run build"]
    return []


def check_sql_fresh():
    import subprocess
    done = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "gen_db_sql.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if done.returncode != 0:
        return ["建库脚本与模型对不上了，先重跑 python scripts/gen_db_sql.py"]
    return []


def main():
    parser = argparse.ArgumentParser(description="上线前预演")
    parser.add_argument("--env-file", help="要检查的环境变量文件")
    args = parser.parse_args()

    if args.env_file:
        env = read_env_file(args.env_file)
        print("检查配置文件：%s（%d 个变量）\n" % (args.env_file, len(env)))
    else:
        env = dict(os.environ)
        print("检查当前 shell 里的环境变量\n")

    # `prodcheck.problems` 不看 APP_ENV，直接按上线标准衡量；
    # 但 APP_ENV 本身没设的话，服务起来时那套检查根本不会执行，要单独提醒。
    blocking = list(prodcheck.problems(env))
    if not prodcheck.is_production(env):
        blocking.insert(0, "没有设 APP_ENV=production——**启动时的自检不会执行**，"
                           "配置错了也照样起得来。")

    blocking += check_database(env)
    blocking += check_admin_password(env)
    blocking += check_frontend_build()
    blocking += check_sql_fresh()

    if blocking:
        print("还不能发，%d 处要改：\n" % len(blocking))
        for i, problem in enumerate(blocking, 1):
            print("  %d) %s\n" % (i, problem))
        print("详细步骤见 docs/上线部署手册.md")
        return 1

    print("预演通过，可以发。")
    print("发完记得跑一遍冒烟：docs/上线部署手册.md 第七节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
