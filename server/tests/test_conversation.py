"""租客与房东会话测试（待完善清单 C-1）。

系统里 AI 对话有两套，但**人与人之间原先不能聊**——现有的三个人际触点
（预约备注、转人工、站内通知）都只能来回一次。租客看中一套房想问
「能不能养猫」，没有地方问。

这批用例守四件事：

* **越权**：改个 id 就能读走别人的私聊，这类漏洞本项目已经修过两次
* **幂等**：重复点「咨询房东」不能长出两个会话
* **未读数**：同一条消息对发送方是已读、对接收方是未读
* **内容审核**：私聊才是发微信号绕过平台的高发地
"""

from tests.base import ApiTestCase, auth_header, make_admin


class ConversationCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")
        self.landlord = auth_header(self.client, "landlord1", "test123")

    def start(self, house_no="DL0001", headers=None):
        return self.client.post("/api/conversations",
                                headers=headers or self.tenant,
                                json={"house_no": house_no})

    def send(self, cid, text, headers=None):
        return self.client.post("/api/conversations/%d/lines" % cid,
                                headers=headers or self.tenant,
                                json={"content": text})

    def lines(self, cid, headers=None, **params):
        return self.client.get("/api/conversations/%d/lines" % cid,
                               headers=headers or self.tenant, params=params)

    def new_conversation(self):
        return self.start().json()["conversation"]["id"]


class TestStart(ConversationCase):

    def test_creates_conversation(self):
        body = self.start().json()["conversation"]
        self.assertEqual(body["house_no"], "DL0001")
        self.assertEqual(body["peer_role"], "房东")

    def test_is_idempotent(self):
        """重复点「咨询房东」不能长出两个会话——
        数据库上有 UNIQUE(house_id, tenant_id) 兜底，这里先给出可读的行为。"""
        first = self.start().json()["conversation"]["id"]
        second = self.start().json()["conversation"]["id"]
        self.assertEqual(first, second)
        self.assertEqual(
            self.client.get("/api/conversations",
                            headers=self.tenant).json()["total"], 1)

    def test_separate_house_separate_conversation(self):
        """会话锚定在「房源 + 租客」上：同一个房东的两套房是两个会话，
        否则租客问的都是「能养猫吗」，房东分不清在问哪套。"""
        self.start("DL0001")
        self.start("DL0002")
        body = self.client.get("/api/conversations",
                               headers=self.tenant).json()
        self.assertEqual(body["total"], 2)
        self.assertEqual({c["house_no"] for c in body["list"]},
                         {"DL0001", "DL0002"})

    def test_landlord_cannot_chat_with_self(self):
        r = self.start(headers=self.landlord)
        self.assertEqual(r.status_code, 400)

    def test_unknown_house(self):
        self.assertEqual(self.start("DL9999").status_code, 404)

    def test_anonymous_denied(self):
        r = self.client.post("/api/conversations", json={"house_no": "DL0001"})
        self.assertEqual(r.status_code, 401)

    def test_peer_name_is_masked(self):
        """会话列表不是通讯录，对方姓名只露姓氏。"""
        self.start()
        body = self.client.get("/api/conversations",
                               headers=self.tenant).json()
        self.assertIn("*", body["list"][0]["peer_name"])


class TestMessaging(ConversationCase):

    def test_send_and_read_back(self):
        cid = self.new_conversation()
        self.assertEqual(self.send(cid, "这套房能养猫吗").status_code, 200)
        body = self.lines(cid).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["list"][0]["content"], "这套房能养猫吗")
        self.assertTrue(body["list"][0]["mine"])

    def test_mine_flag_is_per_viewer(self):
        """同一条消息，发的人看是「我说的」，收的人看不是。"""
        cid = self.new_conversation()
        self.send(cid, "在吗")
        landlord_view = self.lines(cid, self.landlord).json()
        self.assertFalse(landlord_view["list"][0]["mine"])

    def test_both_sides_see_the_whole_thread(self):
        cid = self.new_conversation()
        self.send(cid, "能养猫吗")
        self.send(cid, "可以的", headers=self.landlord)
        for headers in (self.tenant, self.landlord):
            self.assertEqual(self.lines(cid, headers).json()["total"], 2)

    def test_many_messages_in_a_row(self):
        """**验收意见说「私信只能发一条」，这条用来钉死它。**

        本地复现不出来：连发五条、两侧交替发，全部成功。但这个功能
        坏掉的代价太大（私信发不出去等于这个功能不存在），
        而现在没有任何一条用例覆盖「连着发」——
        已有的用例每个会话最多只发一两条。
        """
        cid = self.new_conversation()
        for i in range(5):
            r = self.send(cid, "租客第%d条" % (i + 1))
            self.assertEqual(r.status_code, 200, r.text)
        for i in range(3):
            r = self.send(cid, "房东第%d条" % (i + 1), self.landlord)
            self.assertEqual(r.status_code, 200, r.text)
        rows = self.lines(cid).json()["list"]
        self.assertEqual(len(rows), 8)

    def test_sending_does_not_close_the_conversation(self):
        """发消息不能把会话状态改掉。

        「只能发一条」最可能的成因就是这个：第一条发完之后状态
        不再是 active，后面每一条都被 409 挡住。
        """
        cid = self.new_conversation()
        self.send(cid, "第一条")
        self.assertEqual(self.send(cid, "第二条").status_code, 200)
        self.assertEqual(
            self.client.get("/api/conversations",
                            headers=self.tenant).json()["list"][0]["status"],
            "active")

    def test_sender_label_is_empty_in_a_two_party_chat(self):
        """两人会话里不挂名字。

        `sender_label` 是给委托期间「同一侧坐着两个人」用的。
        两人会话里每条都挂一个名字，等于每句话前面都写一遍
        「房东说：」——而这件事左右位置已经说清楚了。
        """
        cid = self.new_conversation()
        self.send(cid, "租客问")
        self.send(cid, "房东答", self.landlord)
        for headers in (self.tenant, self.landlord):
            for row in self.lines(cid, headers).json()["list"]:
                self.assertEqual(row["sender_label"], "", row["content"])

    def test_after_id_returns_only_new(self):
        """轮询靠它取增量，而不是每次把整段对话重拉一遍。"""
        cid = self.new_conversation()
        self.send(cid, "第一句")
        first_id = self.lines(cid).json()["list"][0]["id"]
        self.send(cid, "第二句")
        body = self.lines(cid, after_id=first_id).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["list"][0]["content"], "第二句")

    def test_empty_message_rejected(self):
        cid = self.new_conversation()
        self.assertEqual(self.send(cid, "").status_code, 422)

    def test_last_message_is_cached_on_conversation(self):
        """列表页要显示最后一句，冗余在会话上才不用回查明细表。"""
        cid = self.new_conversation()
        self.send(cid, "几月能入住")
        body = self.client.get("/api/conversations",
                               headers=self.tenant).json()
        self.assertEqual(body["list"][0]["last_message"], "几月能入住")


class TestUnread(ConversationCase):

    def test_unread_goes_to_the_receiver_only(self):
        """同一条消息，对发送方是已读、对接收方是未读——
        所以未读数必须分两个字段存。"""
        cid = self.new_conversation()
        self.send(cid, "在吗")

        mine = self.client.get("/api/conversations",
                               headers=self.tenant).json()
        theirs = self.client.get("/api/conversations",
                                 headers=self.landlord).json()
        self.assertEqual(mine["list"][0]["unread"], 0)
        self.assertEqual(theirs["list"][0]["unread"], 1)

    def test_read_clears_only_my_side(self):
        cid = self.new_conversation()
        self.send(cid, "在吗")
        self.client.post("/api/conversations/%d/read" % cid,
                         headers=self.landlord)

        theirs = self.client.get("/api/conversations",
                                 headers=self.landlord).json()
        self.assertEqual(theirs["list"][0]["unread"], 0)

        # 房东回一句，未读该记到租客头上
        self.send(cid, "在的", headers=self.landlord)
        mine = self.client.get("/api/conversations",
                               headers=self.tenant).json()
        self.assertEqual(mine["list"][0]["unread"], 1)

    def test_total_unread_across_conversations(self):
        first = self.start("DL0001").json()["conversation"]["id"]
        second = self.start("DL0002").json()["conversation"]["id"]
        self.send(first, "问题一")
        self.send(second, "问题二")
        body = self.client.get("/api/conversations",
                               headers=self.landlord).json()
        self.assertEqual(body["unread"], 2)

    def test_receiver_gets_an_inbox_notice(self):
        """对方没开着会话面板时，靠消息中心的红点提醒。"""
        cid = self.new_conversation()
        self.send(cid, "能养猫吗")
        messages = self.client.get("/api/messages",
                                   headers=self.landlord).json()
        titles = [m["title"] for m in messages["list"]]
        self.assertIn("收到一条新消息", titles)


class TestAccess(ConversationCase):
    """越权是这块最该守的——改个 id 就能读走别人的私聊。"""

    def setUp(self):
        super().setUp()
        self.cid = self.new_conversation()
        self.send(self.cid, "只有我们两个能看到")
        self.client.post("/api/auth/register", json={
            "username": "nosy", "password": "test123", "role": "tenant"})
        self.outsider = auth_header(self.client, "nosy", "test123")

    def test_outsider_cannot_read(self):
        self.assertEqual(self.lines(self.cid, self.outsider).status_code, 403)

    def test_outsider_cannot_send(self):
        self.assertEqual(
            self.send(self.cid, "我插一句", self.outsider).status_code, 403)

    def test_outsider_cannot_mark_read(self):
        r = self.client.post("/api/conversations/%d/read" % self.cid,
                             headers=self.outsider)
        self.assertEqual(r.status_code, 403)

    def test_outsider_list_is_empty(self):
        body = self.client.get("/api/conversations",
                               headers=self.outsider).json()
        self.assertEqual(body["total"], 0)

    def test_anonymous_denied(self):
        self.assertEqual(
            self.client.get("/api/conversations/%d/lines" % self.cid).status_code,
            401)

    def test_admin_can_read(self):
        admin = make_admin(self.client)
        self.assertEqual(self.lines(self.cid, admin).status_code, 200)

    def test_unknown_conversation_404(self):
        self.assertEqual(self.lines(999999).status_code, 404)


class TestModeration(ConversationCase):
    """私聊才是发微信号绕过平台的高发地——只审房源和评价，
    等于把门锁在了没人走的那道。"""

    def test_wechat_is_blocked(self):
        cid = self.new_conversation()
        r = self.send(cid, "加我微信 abc12345 详谈")
        self.assertEqual(r.status_code, 400)
        self.assertIn("平台内沟通", r.json()["detail"])
        self.assertEqual(self.lines(cid).json()["total"], 0)

    def test_phone_is_blocked(self):
        cid = self.new_conversation()
        self.assertEqual(self.send(cid, "打我电话 13900001234").status_code, 400)

    def test_normal_message_passes(self):
        """「误伤为零是硬要求」——正常提问不能被拦。"""
        cid = self.new_conversation()
        for text in ("这套房能养猫吗", "几月份能入住", "押一付一行不行",
                     "微信支付可以吗"):
            self.assertEqual(self.send(cid, text).status_code, 200, text)
