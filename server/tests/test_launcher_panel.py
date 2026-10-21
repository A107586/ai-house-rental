# -*- coding: utf-8 -*-
"""一键启动面板：子进程用对解释器、改 .env 不伤别的行、别的网页改不了配置。

## 为什么值得测

**双击 start-everything.bat 起不来业务后端和智能体**（2026-10-21）。
面板跑在 pythonw.exe 下，又拿同一个 pythonw 去起 `uvicorn --reload`；
真正的服务进程是 multiprocessing 拉起的孙进程，没有 stdout，
uvicorn 配日志时一碰 `sys.stdout` 就死——异常也无处可写。
日志停在 "Started reloader process"，端口永远不开，面板显示「启动中」。
用 python.exe 起面板时一切正常，所以之前的验证全绿。

面板随后加了「配置」：能改 agent/.env 的地址、模型、Key，和高德的三项凭据。
这让两件事变得要紧：

- 改 .env 时**不许碰别的行**——那个文件里有队友手写的注释和别的配置；
  值里也不许带换行，否则能凭空多出一行配置；
- **只有面板自己的网页能改**。任何网页都能往 127.0.0.1:8099 发一个
  text/plain 的请求（不触发 CORS 预检），把 LLM_BASE_URL 指到别处，
  智能体下一次调用就把 Key 发过去了。
"""

import http.client
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _load_panel():
    spec = importlib.util.spec_from_file_location("launcher_panel", ROOT / "launcher" / "panel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


panel = _load_panel()


class TempDirCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestChildInterpreter(TempDirCase):
    """子进程一律用 python.exe：黑窗口靠 CREATE_NO_WINDOW 藏，不靠 pythonw。"""

    def _install(self, *names):
        for name in names:
            (self.tmp / name).write_bytes(b"")
        return str(self.tmp / names[0])

    def test_pythonw_is_swapped_for_its_console_sibling(self):
        pythonw = self._install("pythonw.exe", "python.exe")
        with mock.patch.object(panel, "ROOT", self.tmp), mock.patch.object(sys, "executable", pythonw):
            self.assertEqual(Path(panel.find_python()).name, "python.exe")

    def test_case_of_the_file_name_does_not_matter(self):
        pythonw = self._install("PythonW.EXE", "python.exe")
        with mock.patch.object(panel, "ROOT", self.tmp), mock.patch.object(sys, "executable", pythonw):
            self.assertEqual(Path(panel.find_python()).name, "python.exe")

    def test_without_a_sibling_it_keeps_what_it_has(self):
        pythonw = self._install("pythonw.exe")
        with mock.patch.object(panel, "ROOT", self.tmp), mock.patch.object(sys, "executable", pythonw):
            self.assertEqual(panel.find_python(), pythonw)

    def test_a_venv_wins(self):
        venv = self.tmp / ".venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        with mock.patch.object(panel, "ROOT", self.tmp):
            self.assertEqual(panel.find_python(), str(venv / "python.exe"))


class TestEnvFile(TempDirCase):

    def test_replaces_in_place_and_keeps_every_other_line(self):
        path = self.tmp / ".env"
        path.write_bytes("# 队友写的注释\nLLM_MODEL=old\nOTHER=1\n".encode("utf-8"))
        panel.update_env(path, {"LLM_MODEL": "gpt-6-sol"})
        self.assertEqual(path.read_text(encoding="utf-8"), "# 队友写的注释\nLLM_MODEL=gpt-6-sol\nOTHER=1\n")

    def test_missing_keys_are_appended(self):
        path = self.tmp / ".env"
        path.write_bytes(b"A=1")
        panel.update_env(path, {"B": "2"})
        self.assertEqual(path.read_text(encoding="utf-8"), "A=1\nB=2\n")

    def test_crlf_and_bom_survive(self):
        path = self.tmp / ".env"
        path.write_bytes(b"\xef\xbb\xbfA=1\r\nLLM_MODEL=x\r\nC=3\r\n")
        panel.update_env(path, {"LLM_MODEL": "y", "NEW": "z"})
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfA=1\r\nLLM_MODEL=y\r\nC=3\r\nNEW=z\r\n")

    def test_every_duplicate_is_replaced(self):
        """dotenv 里同名的后一行算数：只换第一行的话，后一行会把新值盖回去。"""
        path = self.tmp / ".env"
        path.write_bytes(b"K=a\nK=b\n")
        panel.update_env(path, {"K": "c"})
        self.assertEqual(panel.read_env(path)["K"], "c")
        self.assertNotIn(b"=a", path.read_bytes())

    def test_values_that_could_smuggle_a_line_are_refused(self):
        path = self.tmp / ".env"
        path.write_bytes(b"K=a\n")
        for bad in ("x\nLLM_BASE_URL=https://evil.example/v1", "a b", 'a"b', "a#b", "x\r"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                panel.update_env(path, {"K": bad})
        self.assertEqual(path.read_bytes(), b"K=a\n")

    def test_a_missing_file_starts_from_the_template(self):
        template = self.tmp / ".env.example"
        template.write_bytes(b"# from template\nVITE_AMAP_KEY=\n")
        target = self.tmp / ".env.local"
        panel.update_env(target, {"VITE_AMAP_KEY": "abc"}, template=template)
        self.assertEqual(target.read_text(encoding="utf-8"), "# from template\nVITE_AMAP_KEY=abc\n")
        self.assertTrue(template.read_bytes().endswith(b"VITE_AMAP_KEY=\n"), "模板本身不能被改")
        self.assertFalse((self.tmp / ".env.local.tmp").exists())

    def test_reading_follows_dotenv(self):
        path = self.tmp / ".env"
        path.write_bytes(b'A="quoted # not a comment"\nB=plain # comment\nC=1\nC=2\n# D=3\n')
        env = panel.read_env(path)
        self.assertEqual(env, {"A": "quoted # not a comment", "B": "plain", "C": "2"})


class TestLlmConfig(TempDirCase):
    """模式的判法要和 agent/app/config.py 的 llm_enabled 一致，否则面板说的和智能体做的不是一回事。"""

    def setUp(self):
        super().setUp()
        self.env = self.tmp / ".env"
        for name, value in (("ENV_FILE", self.env), ("PANEL_STATE", self.tmp / "state.json"),
                            ("ROOT", self.tmp)):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, text):
        self.env.write_text(text, encoding="utf-8")

    def test_modes(self):
        cases = [
            ("LLM_PROVIDER=openai_compatible\nLLM_API_KEY=k\nLLM_BASE_URL=https://4router.net/v1\n", "remote"),
            ("LLM_PROVIDER=openai_compatible\nLLM_API_KEY=k\nLLM_BASE_URL=http://127.0.0.1:18241/v1\n", "local"),
            ("LLM_PROVIDER=\nLLM_API_KEY=k\nLLM_BASE_URL=https://4router.net/v1\n", "rules"),
            # 有 provider 没 Key：config.py 里 llm_enabled 为假，智能体其实是纯规则
            ("LLM_PROVIDER=deepseek\nLLM_API_KEY=\nLLM_BASE_URL=https://api.deepseek.com/v1\n", "rules"),
        ]
        for text, mode in cases:
            self.write(text)
            self.assertEqual(panel.llm_config()["mode"], mode, text)
        self.assertFalse(panel.agent_uses_local_model())

    def test_the_key_never_leaves_the_panel(self):
        self.write("LLM_PROVIDER=x\nLLM_API_KEY=sk-secret-123\nLLM_BASE_URL=https://a.example/v1\n")
        self.assertNotIn("sk-secret-123", json.dumps(panel.llm_config()))
        self.assertTrue(panel.llm_config()["has_key"])

    def test_a_stored_key_is_not_sent_to_another_host(self):
        self.write("LLM_PROVIDER=x\nLLM_API_KEY=sk-a\nLLM_BASE_URL=https://a.example/v1\n")
        with self.assertRaises(ValueError):
            panel._resolve_llm({"mode": "remote", "base_url": "https://b.example/v1"})
        _, base, key = panel._resolve_llm({"mode": "remote", "base_url": "https://a.example/v1/"})
        self.assertEqual((base, key), ("https://a.example/v1", "sk-a"))
        _, _, key = panel._resolve_llm({"mode": "remote", "base_url": "https://b.example/v1", "api_key": "sk-b"})
        self.assertEqual(key, "sk-b")

    def test_switching_to_rules_only_clears_the_provider(self):
        """地址、模型、Key 都留着：切回远程时不用重填。"""
        self.write("# keep me\nLLM_PROVIDER=openai_compatible\nLLM_API_KEY=sk-a\n"
                   "LLM_BASE_URL=https://a.example/v1\nLLM_MODEL=m1\nLLM_TIMEOUT=7\n")
        with mock.patch.object(panel, "status", return_value={"state": "stopped"}):
            result = panel.llm_save({"mode": "rules", "timeout": "12"})
        self.assertTrue(result["ok"], result)
        env = panel.read_env(self.env)
        self.assertEqual(env["LLM_PROVIDER"], "")
        self.assertEqual((env["LLM_API_KEY"], env["LLM_MODEL"], env["LLM_TIMEOUT"]), ("sk-a", "m1", "12"))
        self.assertIn("# keep me", self.env.read_text(encoding="utf-8"))
        config = panel.llm_config()
        self.assertEqual(config["mode"], "rules")
        self.assertEqual(config["remote"], {"base_url": "https://a.example/v1", "model": "m1"})

    def test_local_then_back_to_remote(self):
        self.write("LLM_PROVIDER=openai_compatible\nLLM_API_KEY=sk-a\n"
                   "LLM_BASE_URL=https://a.example/v1\nLLM_MODEL=m1\n")
        with mock.patch.object(panel, "status", return_value={"state": "stopped"}):
            self.assertTrue(panel.llm_save({"mode": "local", "model": "qwen-local", "timeout": "30"})["ok"])
            self.assertEqual(panel.read_env(self.env)["LLM_BASE_URL"], panel.LOCAL_BASE)
            self.assertTrue(panel.agent_uses_local_model())
            remembered = panel.llm_config()["remote"]
            self.assertEqual(remembered, {"base_url": "https://a.example/v1", "model": "m1"})
            result = panel.llm_save({"mode": "remote", "base_url": remembered["base_url"],
                                     "model": remembered["model"], "timeout": "7"})
        self.assertTrue(result["ok"], result)
        env = panel.read_env(self.env)
        self.assertEqual((env["LLM_BASE_URL"], env["LLM_MODEL"], env["LLM_API_KEY"]),
                         ("https://a.example/v1", "m1", "sk-a"))

    def test_errors_do_not_echo_the_key(self):
        error = panel.urllib.error.URLError("refused sk-echo-back")
        self.assertNotIn("sk-echo-back", panel._llm_error(error, "sk-echo-back"))


class TestAmapSave(TempDirCase):

    def setUp(self):
        super().setUp()
        (self.tmp / "web").mkdir()
        (self.tmp / "web" / ".env.example").write_bytes(b"VITE_AMAP_KEY=\nVITE_AMAP_SECURITY_JS_CODE=\n")
        self.web, self.server_env = self.tmp / "web" / ".env.local", self.tmp / "server.env"
        self.server_env.write_bytes(b"AMAP_WEB_SERVICE_KEY=old\nAUTH_SECRET=keep\n")
        for name, value in (("ROOT", self.tmp), ("WEB_ENV", self.web), ("SERVER_ENV", self.server_env)):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(panel, "apply_amap")
        self.apply = patcher.start()
        self.addCleanup(patcher.stop)

    def test_blank_fields_are_left_alone(self):
        result = panel.amap_save({"web_key": "wk", "web_security": "", "service_key": ""})
        self.assertTrue(result["ok"], result)
        self.assertEqual(panel.read_env(self.web), {"VITE_AMAP_KEY": "wk", "VITE_AMAP_SECURITY_JS_CODE": ""})
        self.assertEqual(panel.read_env(self.server_env)["AMAP_WEB_SERVICE_KEY"], "old")
        self.assertEqual(panel.amap_config(), {"web_key": True, "web_security": False, "service_key": True})

    def test_only_the_side_that_changed_is_restarted(self):
        panel.amap_save({"service_key": "sk"})
        self.apply.assert_called_once_with(False, True)
        self.assertEqual(panel.read_env(self.server_env), {"AMAP_WEB_SERVICE_KEY": "sk", "AUTH_SECRET": "keep"})
        self.assertFalse(self.web.exists())

    def test_nothing_filled_is_an_error_not_a_restart(self):
        self.assertFalse(panel.amap_save({})["ok"])
        self.apply.assert_not_called()


class TestOnlyThePanelPageCanTalkToIt(TempDirCase):

    def setUp(self):
        super().setUp()
        env = self.tmp / ".env"
        env.write_text("LLM_PROVIDER=x\nLLM_API_KEY=sk-secret-456\nLLM_BASE_URL=https://a.example/v1\n",
                       encoding="utf-8")
        for name, value in (("ENV_FILE", env), ("PANEL_STATE", self.tmp / "state.json"),
                            ("WEB_ENV", self.tmp / "web.env"), ("SERVER_ENV", self.tmp / "server.env")):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_the_panel_page_itself_works(self):
        status, data = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn(b"sk-secret-456", data)
        status, _ = self.request("POST", "/api", body=json.dumps({"action": "nope"}),
                                 headers={"Content-Type": "application/json",
                                          "Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 400)

    def test_a_simple_cross_site_request_is_refused(self):
        """text/plain 的 POST 不触发预检，浏览器会直接发出去——必须在这里拦住。"""
        body = json.dumps({"action": "llm_save", "mode": "remote", "base_url": "https://evil.example/v1",
                           "api_key": "x", "model": "m"})
        status, _ = self.request("POST", "/api", body=body, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertNotIn("evil", panel.ENV_FILE.read_text(encoding="utf-8"))

    def test_another_origin_is_refused(self):
        status, _ = self.request("POST", "/api", body=json.dumps({"action": "amap_save", "web_key": "x"}),
                                 headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(panel.WEB_ENV.exists())

    def test_dns_rebinding_is_refused(self):
        status, _ = self.request("GET", "/api/config", headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
