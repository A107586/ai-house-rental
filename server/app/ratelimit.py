"""登录限流（待完善清单 B-1）。

登录接口原先没有任何限制——没有限流、没有验证码、没有失败计数，
撞库脚本可以不受限制地跑。

三个决定值得写下来：

**一、按 IP 计数，不按用户名。**
按用户名限流看着更精准，实际上是**给攻击者提供了锁死他人账号的手段**：
拿一个已知用户名连错八次，本人就登不上了。按 IP 则只影响攻击者自己。
代价是同一出口 IP 后面的一群人会互相影响（宿舍、公司网关），
所以阈值要留得宽一些。

**二、只统计失败，成功立刻清零。**
统计全部尝试的话，一个正常用户在几台设备上轮流登录几次就会被锁。
失败才是信号。

**三、放在内存里，不引 Redis。**
本项目是单进程部署，进程内的字典就够用。多 worker 之后这份计数会各存
各的（等于阈值被放大 N 倍），那时再换 Redis——**但那是部署形态变化时
要一起处理的事，不是现在该背的复杂度**。

不做验证码：验证码需要前端配合与图形库，而限流已经能把自动化撞库的
成本抬到不可行。
"""

import threading
import time

# 同一 IP 在 FAIL_WINDOW 秒内最多失败 FAIL_LIMIT 次。
#
# 8 次是给真人留的余量：记混了密码、大小写没切、旧密码试一遍，
# 三五次很正常。而自动化撞库一秒就能打完 8 次，对它毫无用处。
FAIL_WINDOW = 300
FAIL_LIMIT = 8

# {ip: [失败时刻, ...]}
_failures = {}
# {(命名空间, ip): [尝试时刻, ...]}，见文件末尾的通用尝试限流
_attempts = {}
_lock = threading.Lock()

# 超过这个规模就清一次过期条目。不每次都清是因为绝大多数请求
# 只需要看自己那一条，全表扫描不划算。
_SWEEP_AT = 512


def _prune(now):
    """丢掉窗口外的记录。调用方必须已经持有锁。"""
    dead = []
    for ip, stamps in _failures.items():
        alive = [t for t in stamps if now - t < FAIL_WINDOW]
        if alive:
            _failures[ip] = alive
        else:
            dead.append(ip)
    for ip in dead:
        _failures.pop(ip, None)


def check(ip):
    """还能不能再试。返回 ``(允许, 还需等待的秒数)``。"""
    if not ip:
        return True, 0
    now = time.time()
    with _lock:
        stamps = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
        if stamps:
            _failures[ip] = stamps
        if len(stamps) < FAIL_LIMIT:
            return True, 0
        # 最早那次失败滑出窗口时才解封，而不是从最后一次算起——
        # 否则攻击者每次被拒都会把封禁时间刷新一遍，等于永久封死
        wait = int(FAIL_WINDOW - (now - stamps[0])) + 1
        return False, max(wait, 1)


def record_failure(ip):
    if not ip:
        return
    now = time.time()
    with _lock:
        _failures.setdefault(ip, []).append(now)
        if len(_failures) > _SWEEP_AT:
            _prune(now)


def record_success(ip):
    """登录成功即清零。本人已经证明身份，之前的手误不该继续记账。"""
    if not ip:
        return
    with _lock:
        _failures.pop(ip, None)


def reset():
    """清空全部计数。仅供测试使用——用例之间不能互相污染。"""
    with _lock:
        _failures.clear()
        _attempts.clear()


def client_ip(request):
    """取客户端 IP。经过反向代理时用 X-Forwarded-For 的第一段。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else ""


# ---------------------------------------------------------------- 通用尝试限流
#
# 登录用的是**失败**计数：成功即清零，因为「本人已经证明身份」。
# 但有些接口没有「成功」这个概念可以用来清零——比如找回密码（K-11），
# 它对存在与不存在的账号都返回同一句话，正是为了不泄露账号是否存在。
#
# **不能直接复用上面那套。** 两个原因：
#   1. 语义不同：那边记的是「猜错了几次」，这边要记的是「来了几次」；
#   2. 更要命的是 `record_success` 会清零——攻击者只要登录一下自己的
#      账号就能把找回密码的计数抹掉，限流形同虚设。
#
# 所以另开一份带命名空间的计数，两者互不影响（`_attempts` 定义在文件开头）。


def hit(namespace, ip, limit, window):
    """记一次尝试，并返回 ``(允许, 还需等待的秒数)``。

    先判断再记账：正好卡在阈值上的那一次应当被放行，
    而不是「记完发现超了」把它也拒掉。
    """
    if not ip:
        return True, 0
    now = time.time()
    key = (namespace, ip)
    with _lock:
        stamps = [t for t in _attempts.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            _attempts[key] = stamps
            # 和登录一致：从最早那次算解封时间，
            # 否则每次被拒都会把封禁时间刷新一遍，等于永久封死
            wait = int(window - (now - stamps[0])) + 1
            return False, max(wait, 1)
        stamps.append(now)
        _attempts[key] = stamps
        if len(_attempts) > _SWEEP_AT:
            dead = [k for k, v in _attempts.items()
                    if not any(now - t < window for t in v)]
            for k in dead:
                _attempts.pop(k, None)
        return True, 0
