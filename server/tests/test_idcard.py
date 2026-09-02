"""身份证号处理测试（需求 R-02 / N-04）。

这一组守的是一条底线：**原文任何时候都不能落库、不能被接口吐出去**。

系统对身份证号的全部需求只有「展示」与「查重」，两者都不需要还原原文。
既然不需要还原，就不该保留还原的可能——所以存的是不可逆哈希 + 掩码，
而不是可逆加密。密钥泄露也还原不出来。
"""

from tests.base import ApiTestCase, auth_header
from app import idcard

# 校验位正确的测试号码（非真实证件）
VALID = "110105199003077817"
VALID2 = "310101199001011234"
# 校验位是 X 的号码，用来验大小写处理
WITH_X = "21020219950812456X"


class TestValidate(ApiTestCase):

    def test_accepts_valid(self):
        self.assertTrue(idcard.is_valid(VALID))

    def test_accepts_x_check_digit_either_case(self):
        """校验位的 X 有人写大写有人写小写，都得认。"""
        self.assertTrue(idcard.is_valid(WITH_X))
        self.assertTrue(idcard.is_valid(WITH_X.lower()))

    def test_rejects_wrong_length(self):
        self.assertFalse(idcard.is_valid("11010519900307"))
        self.assertFalse(idcard.is_valid("1101051990030778170"))

    def test_rejects_letters_in_body(self):
        self.assertFalse(idcard.is_valid("1101051990030A7817"))

    def test_rejects_bad_checksum(self):
        """只判长度和字符不够——随手编一串十八位数字也能过，
        校验位能挡掉绝大多数手滑与瞎填。"""
        self.assertFalse(idcard.is_valid("110105199003077811"))
        self.assertFalse(idcard.is_valid("123456789012345678"))

    def test_rejects_empty(self):
        self.assertFalse(idcard.is_valid(""))
        self.assertFalse(idcard.is_valid(None))


class TestMask(ApiTestCase):

    def test_shape(self):
        self.assertEqual(idcard.mask(VALID), "110***********7817")

    def test_hides_birthday(self):
        """中间的生日与顺序码是识别个人的关键，必须遮掉。"""
        masked = idcard.mask(VALID)
        self.assertNotIn("19900307", masked)

    def test_keeps_last_four_for_self_check(self):
        """留后四位是让本人核对「是不是我这张」。"""
        self.assertTrue(idcard.mask(VALID).endswith("7817"))

    def test_bad_input(self):
        self.assertEqual(idcard.mask(""), "")
        self.assertEqual(idcard.mask("123"), "")


class TestFingerprint(ApiTestCase):

    def test_stable(self):
        self.assertEqual(idcard.fingerprint(VALID), idcard.fingerprint(VALID))

    def test_case_insensitive(self):
        """校验位的 X 有人写大写有人写小写，不能算成两个人。"""
        self.assertEqual(idcard.fingerprint(WITH_X),
                         idcard.fingerprint(WITH_X.lower()))

    def test_different_numbers_differ(self):
        self.assertNotEqual(idcard.fingerprint(VALID),
                            idcard.fingerprint(VALID2))

    def test_not_reversible(self):
        """哈希里不该残留原文的任何片段。"""
        digest = idcard.fingerprint(VALID)
        self.assertNotIn("19900307", digest)
        self.assertNotIn(VALID, digest)

    def test_salted(self):
        """裸 SHA-256 挡不住彩虹表——身份证号真正随机的只有后四位，
        穷举成本极低。加盐后拿不到密钥就无从穷举。"""
        import hashlib
        naked = hashlib.sha256(VALID.encode()).hexdigest()
        self.assertNotEqual(idcard.fingerprint(VALID), naked)


class TestPrepare(ApiTestCase):

    def test_ok(self):
        ok, result = idcard.prepare(VALID)
        self.assertTrue(ok)
        self.assertEqual(result["masked"], "110***********7817")
        self.assertEqual(len(result["hash"]), 64)

    def test_rejects_invalid(self):
        ok, message = idcard.prepare("123456789012345678")
        self.assertFalse(ok)
        self.assertIn("格式", message)

    def test_rejects_empty(self):
        ok, message = idcard.prepare("")
        self.assertFalse(ok)


class TestProfileApi(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tenant = auth_header(self.client, "tenant1", "test123")

    def save(self, number, headers=None):
        return self.client.put("/api/auth/profile", headers=headers or self.tenant,
                                 json={"id_card": number})

    def test_saved_and_returned_masked(self):
        r = self.save(VALID)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["id_card"], "110***********7817")

    def test_plaintext_never_leaves_the_api(self):
        """接口返回里不能出现完整号码。"""
        body = str(self.save(VALID).json())
        self.assertNotIn(VALID, body)
        self.assertNotIn("19900307", body)

    def test_plaintext_never_reaches_the_database(self):
        """**这条是底线**：全表扫一遍，任何字段都不能存着原文。"""
        self.save(VALID)
        import app.db as db
        from app.models import User
        session = db.SessionLocal()
        try:
            row = session.query(User).filter_by(username="tenant1").one()
            dump = " ".join(str(v) for v in row.__dict__.values())
            self.assertNotIn(VALID, dump)
            
            self.assertNotIn("19900307", dump, "生日片段也不该留下")
            self.assertTrue(row.id_card_hash)
            self.assertEqual(row.id_card_masked, "110***********7817")
        finally:
            session.close()

    def test_invalid_rejected(self):
        self.assertEqual(self.save("123456789012345678").status_code, 400)

    def test_duplicate_rejected(self):
        """同一个身份证号只能绑一个账号，否则实名就形同虚设。"""
        self.save(VALID)
        self.client.post("/api/auth/register", json={
            "username": "other", "password": "test123", "role": "tenant"})
        other = auth_header(self.client, "other", "test123")
        r = self.save(VALID, headers=other)
        self.assertEqual(r.status_code, 409)

    def test_rebinding_same_number_to_self_is_fine(self):
        """本人重复提交同一个号码不该被自己挡住。"""
        self.save(VALID)
        self.assertEqual(self.save(VALID).status_code, 200)

    def test_other_fields_still_work(self):
        r = self.client.put("/api/auth/profile", headers=self.tenant,
                              json={"real_name": "林小雨", "id_card": VALID})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["real_name"], "林小雨")

    def test_not_required(self):
        """没填身份证号时不能报错——不是所有操作都需要实名。"""
        r = self.client.put("/api/auth/profile", headers=self.tenant,
                              json={"real_name": "林小雨"})
        self.assertEqual(r.status_code, 200)

    def test_anonymous_denied(self):
        r = self.client.put("/api/auth/profile", json={"id_card": VALID})
        self.assertEqual(r.status_code, 401)
