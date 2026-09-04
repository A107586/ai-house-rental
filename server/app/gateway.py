"""支付网关适配器。

**这一层存在的唯一理由是：将来换成真网关时，只改这个文件。**

课程项目拿不到支付牌照与商户号，所以没有真实的支付宝/微信通道。
但「拿不到通道」不等于「支付流程可以简化成把账单状态改成已付」——
那样写出来的代码，等真接上网关时整条链路都要重写。

所以这里按真实网关的形状定义接口：

    发起支付 → 拿到跳转地址与网关单号 → 用户去网关付款
             → 网关异步回调平台 → 平台验签、幂等、记账 → 前端轮询确认

`PaymentGateway` 是接口，`MockGateway` 是本地实现（用一个能自己回调
自己的假网关）。将来接支付宝，写一个 `AlipayGateway` 实现同样三个方法，
在 `get_gateway()` 里换掉即可，业务代码一行不动。

真网关与假网关的差别只有两处，都被这一层挡住了：
  1. `create()` 返回的 pay_url 指向谁；
  2. 回调是谁发来的、怎么验签。
"""

import hashlib
import hmac
import json
import os
import time

# 与登录令牌用的是同一套思路：标准库 hmac，不引第三方 SDK。
# 真实网关这里换成商户私钥 RSA2 签名，验签逻辑同样收在本文件里。
SIGN_SECRET = os.environ.get("PAYMENT_SIGN_SECRET", "rental-pay-dev-secret")

# 支付单有效期（秒）。超时未回调的单子要关掉，否则它会永远占着
# bill_id 的唯一约束，用户重新发起支付时被挡住。
EXPIRE_SECONDS = int(os.environ.get("PAYMENT_EXPIRE_SECONDS", "900"))


def sign(params):
    """按字典序拼接后签名——支付宝/微信都是这个做法。

    之所以要排序：网关推送过来的参数顺序不保证，按收到的顺序拼接
    验签会时对时错，而且这种错只在参数顺序变化时出现，极难复现。
    """
    items = sorted((k, v) for k, v in params.items() if k != "sign" and v != "")
    raw = "&".join("%s=%s" % (k, v) for k, v in items)
    return hmac.new(SIGN_SECRET.encode("utf-8"),
                    raw.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(params):
    """验签。**回调接口没有登录态，签名是唯一的身份凭证。**

    用 compare_digest 而不是 ==：字符串比较会在第一个不同的字符处返回，
    比较耗时因此泄露了「前几位对不对」，理论上可以逐位爆破出签名。
    """
    got = params.get("sign", "")
    if not got:
        return False
    return hmac.compare_digest(got, sign(params))


class PaymentGateway(object):
    """网关接口。三个方法对应三段式的三步。"""

    name = "base"

    def create(self, trade_no, amount, subject):
        """发起支付，返回 {gateway_trade_no, pay_url, expire_at}。"""
        raise NotImplementedError

    def build_notify(self, trade_no, gateway_trade_no, amount, result="success"):
        """构造一份带签名的回调报文——真网关里这一步是网关做的，
        这里由假网关代劳，好让本地能完整跑通回调链路。"""
        raise NotImplementedError

    def query(self, gateway_trade_no):
        """主动查询。回调可能丢，真实系统必须有一条主动查询的兜底路径。"""
        raise NotImplementedError


class MockGateway(PaymentGateway):
    """本地假网关。

    它不联网、不收钱，但**回调报文的形状、签名方式、幂等语义都是真的**——
    这样写出来的回调处理代码，接上真网关不用改。
    """

    name = "mock"

    def __init__(self):
        # 假网关自己记账，模拟网关侧的单据状态
        self._orders = {}

    def create(self, trade_no, amount, subject):
        gateway_trade_no = "MOCK%s" % hashlib.md5(
            ("%s|%s" % (trade_no, time.time())).encode("utf-8")).hexdigest()[:16].upper()
        expire_at = time.time() + EXPIRE_SECONDS
        self._orders[gateway_trade_no] = {
            "trade_no": trade_no, "amount": float(amount),
            "subject": subject, "status": "pending", "expire_at": expire_at,
        }
        return {
            "gateway_trade_no": gateway_trade_no,
            # 真网关这里是收银台地址，前端跳过去付款。
            # 假网关给一个本地页面，点「模拟支付成功」触发回调。
            "pay_url": "/#/pay/%s" % trade_no,
            "expire_at": expire_at,
        }

    def build_notify(self, trade_no, gateway_trade_no, amount, result="success"):
        params = {
            "trade_no": trade_no,
            "gateway_trade_no": gateway_trade_no,
            "amount": "%.2f" % float(amount),
            "result": result,
            "timestamp": str(int(time.time())),
        }
        params["sign"] = sign(params)
        order = self._orders.get(gateway_trade_no)
        if order:
            order["status"] = "succeeded" if result == "success" else "failed"
        return params

    def query(self, gateway_trade_no):
        order = self._orders.get(gateway_trade_no)
        if not order:
            return {"status": "not_found"}
        if order["status"] == "pending" and time.time() > order["expire_at"]:
            return {"status": "closed"}
        return {"status": order["status"], "amount": order["amount"]}


_gateway = MockGateway()


def get_gateway():
    """将来换真网关只改这里：读配置返回 AlipayGateway / WechatGateway。"""
    return _gateway


def dumps(params):
    """回调报文原样存档——出了对账问题要能拿出网关到底推了什么。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True)
