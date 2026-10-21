# -*- coding: utf-8 -*-
"""本地控制面板：把模型、后端、智能体、前端、模型对话页收进一个网页，替代一排黑窗口。

    python launcher/panel.py [--autostart] [--port 8099]

所有服务都作为隐藏子进程运行，输出写进 launcher/logs/<服务>.log，面板上能看状态、
启停、切档位、看日志。只绑 127.0.0.1。

几条从 start-all.ps1 / start-ninfer.ps1 继承下来的规矩（原因都写在那两个文件里）：
  * 模型仍由 start-ninfer.ps1 起——引擎参数只维护一处。面板不传 -Switch：那个开关会
    自己拉一个面板管不到的隐藏智能体，这里改由面板写 agent\\.env 再重启自己的智能体。
  * 智能体改完 .env 必须整进程重启（config.py 的 load_dotenv 不覆盖已有环境变量）。
  * 停服务一律连进程树一起杀（uvicorn --reload 是三层，只杀顶层会留下攥着端口的孤儿）。
  * 端口已被别处占着时不重复启动，免得同一端口上新旧两份同时应答。
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
ENV_FILE = ROOT / "agent" / ".env"
MODEL_PORT = 18241
CHAT_PAGE = Path(r"D:\pkg\chat\serve_chat.py")
CREATE_NO_WINDOW = 0x08000000

SERVER_ENV = ROOT / "server" / ".env"
WEB_ENV = ROOT / "web" / ".env.local"
# 面板自己记的东西（切到本地模型/纯规则之前的远程地址与模型名）。不含密钥，不进 git。
PANEL_STATE = HERE / "panel-settings.json"
LOCAL_BASE = f"http://127.0.0.1:{MODEL_PORT}/v1"
# 本地引擎不验 Key，但智能体的 llm_enabled 要求 Key 非空；没有 Key 时填这个占位
LOCAL_KEY = "local"

settings = {"profile": "fast", "vision": True, "thinking": True}
procs = {}          # name -> Popen started by this panel
started = {}        # name -> time.time() of the last start by this panel
build = {"state": "idle", "at": "", "exit": None}   # `npm run build` 的进度，见 run_build()
lock = threading.Lock()


def find_python():
    """子进程用哪个解释器。**永远是 python.exe，不能是 pythonw.exe。**

    start-everything.bat 用 pythonw 起面板（为了没有黑窗口），面板原先直接拿
    sys.executable 去起后端和智能体，于是它们也跑在 pythonw 下。
    `uvicorn --reload` 的真正服务是 multiprocessing 拉起的孙进程，pythonw 下它
    没有 stdout/stderr，uvicorn 配日志时一碰 sys.stdout 就抛异常——**而异常本身也
    无处可写**：日志停在 "Started reloader process"，端口永远不开，面板一直「启动中」。
    2026-10-21 双击启动时撞上的；用 python.exe 起面板（launch.json）时一切正常，
    所以之前的验证没发现。黑窗口由 CREATE_NO_WINDOW 负责藏，用不着 pythonw。
    """
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def find_npm():
    for cand in ("npm.cmd",):
        try:
            if subprocess.run([cand, "--version"], capture_output=True, creationflags=CREATE_NO_WINDOW).returncode == 0:
                return cand
        except OSError:
            pass
    for guess in (Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "npm.cmd",
                  Path(os.environ.get("ProgramFiles(x86)", "")) / "nodejs" / "npm.cmd",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "npm.cmd",
                  Path(os.environ.get("APPDATA", "")) / "npm" / "npm.cmd"):
        if guess.exists():
            return str(guess)
    return None


PY = find_python()


def model_cmd():
    switches = []
    if settings["vision"]:
        switches.append("-Vision")
    if settings["profile"] == "long":
        switches.append("-Long")
    if not settings["thinking"]:
        switches.append("-NoThinking")
    # Redirected PowerShell output uses the console code page (GBK here) unless told otherwise,
    # and the panel reads logs as UTF-8 -- the Chinese status lines came out garbled.
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              f"& '{ROOT / 'start-ninfer.ps1'}' {' '.join(switches)}")
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


SERVICES = {
    "model": {"title": "本地模型", "port": MODEL_PORT, "cwd": ROOT, "cmd": model_cmd,
              "health": f"http://127.0.0.1:{MODEL_PORT}/v1/models", "boot": 180},
    # `--no-proxy-headers`：uvicorn 默认开着它、默认信任 127.0.0.1，会在请求到达应用前
    # 拿 X-Forwarded-For 改写客户端地址——登录限流就能被一个请求头绕过。
    # 原因与实测见 server/tests/test_launch_flags.py，那条测试也盯着这里。
    "backend": {"title": "业务后端", "port": 8080, "cwd": ROOT / "server",
                "cmd": lambda: [PY, "-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8080",
                                "--no-proxy-headers"],
                "health": "http://127.0.0.1:8080/docs", "boot": 60},
    "agent": {"title": "智能体", "port": 8000, "cwd": ROOT / "agent",
              "cmd": lambda: [PY, "-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000",
                              "--no-proxy-headers"],
              "health": "http://127.0.0.1:8000/health", "boot": 90},
    "web": {"title": "前端网站", "port": 5173, "cwd": ROOT / "web",
            "cmd": lambda: [find_npm() or "npm.cmd", "run", "dev"],
            "health": "http://localhost:5173/", "boot": 150},
    "chat": {"title": "模型对话页", "port": 8090, "cwd": CHAT_PAGE.parent,
             "cmd": lambda: [PY, str(CHAT_PAGE), "--engine", f"http://127.0.0.1:{MODEL_PORT}"],
             "health": "http://127.0.0.1:8090/", "boot": 20, "optional": True},
}
ORDER = ["model", "backend", "agent", "web", "chat"]


# ---------- probes ----------------------------------------------------------------------------
def port_open(port):
    # Vite listens on ::1 only; uvicorn on 0.0.0.0; check both families.
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            pass
    return False


def http_ok(url, timeout=2):
    # Vite only serves index.html to requests that ask for HTML.
    req = urllib.request.Request(url, headers={"Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400, r.read(20000)
    except Exception:
        return False, b""


def port_owner_pids(port):
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         creationflags=CREATE_NO_WINDOW).stdout
    out += subprocess.run(["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
            pids.add(int(parts[4]))
    return pids


def kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=CREATE_NO_WINDOW)


def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,display_active",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                             creationflags=CREATE_NO_WINDOW).stdout.strip().split(", ")
        return {"used": int(out[0]), "total": int(out[1]), "display": out[2]}
    except Exception:
        return None


# ---------- agent\.env ------------------------------------------------------------------------
def point_agent_at_model():
    """Same edit as start-ninfer.ps1 Set-AgentBaseUrl. Returns True if the file changed."""
    if not ENV_FILE.exists():
        return False
    raw = ENV_FILE.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    new = re.sub(r"LLM_BASE_URL\s*=\s*http://127\.0\.0\.1:\d+/v1", f"LLM_BASE_URL=http://127.0.0.1:{MODEL_PORT}/v1", text)
    if new == text:
        return False
    ENV_FILE.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
    return True


def read_env(path):
    """把一个 .env 读成 dict。规则跟 python-dotenv 一致：注释与空行跳过，
    同名的后一行算数，没加引号的值在「空白 + #」处截断。"""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_bytes().decode("utf-8-sig", "replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value[:1] in ("'", '"') and value.endswith(value[:1]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[m.group(1)] = value
    return out


# 值里不许有空白、引号、#：**换行能在 .env 里凭空多出一行配置**（比如顺手塞一个
# LLM_BASE_URL 把 Key 引到别处），引号和 # 会被 dotenv 当成语法吃掉一截。
# Key、网址、模型名都用不到这些字符。
_SAFE_VALUE = re.compile(r"^[^\s\"'#]*$")


def update_env(path, changes, template=None):
    """改 .env 里的几项，**其余行（含注释）原样保留**。

    已有的行就地替换（同名多行都换掉，免得后一行把新值盖回去），没有的追加到末尾。
    文件不存在时从 template（.env.example）起步。保留 BOM 与原来的换行风格；
    先写临时文件再 os.replace，写到一半断电也不会留下半个 .env。
    """
    for key, value in changes.items():
        if not _SAFE_VALUE.match(value):
            raise ValueError("%s 的值不能包含空格、换行、引号或 #" % key)
    if path.exists():
        raw = path.read_bytes()
    elif template is not None and template.exists():
        raw = template.read_bytes()
    else:
        raw = b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in changes.items():
        # [^\r\n]* 而不是 .*：CRLF 文件里 .* 会把 \r 一起吃掉，换完就成了混合换行
        pattern = re.compile(r"^[ \t]*(?:export[ \t]+)?%s[ \t]*=[^\r\n]*" % re.escape(key), re.M)
        line = "%s=%s" % (key, value)
        if pattern.search(text):
            text = pattern.sub(lambda _m: line, text)
        else:
            if text and not text.endswith("\n"):
                text += newline
            text += line + newline
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    os.replace(tmp, path)


def load_panel_state():
    try:
        return json.loads(PANEL_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_panel_state(state):
    PANEL_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_local_url(url):
    return bool(re.match(r"https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url or ""))


def same_host(a, b):
    host = lambda u: re.sub(r"^https?://", "", (u or "").strip().lower()).split("/", 1)[0]
    return bool(a and b) and host(a) == host(b)


def agent_llm_target():
    """agent\\.env 里 LLM_BASE_URL 的值；没有文件或没写就是空串。"""
    return read_env(ENV_FILE).get("LLM_BASE_URL", "")


def llm_config():
    """智能体现在用的是哪种模型，**照 agent/app/config.py 的判法**：

    - LLM_PROVIDER 或 LLM_API_KEY 有一个是空的 → `llm_enabled` 为假 → 纯规则；
    - 否则 LLM_BASE_URL 指向本机 → 本地模型；
    - 否则 → 远程 API。

    **不返回 Key 本身**，只说有没有——这个结果会原样交给网页。
    """
    env = read_env(ENV_FILE)
    provider = env.get("LLM_PROVIDER", "")
    base = env.get("LLM_BASE_URL", "")
    has_key = bool(env.get("LLM_API_KEY"))
    if not provider or not has_key:
        mode = "rules"
    elif is_local_url(base):
        mode = "local"
    else:
        mode = "remote"
    saved = load_panel_state()
    remote = saved.get("remote", {})
    if mode == "remote" or (not remote and base and not is_local_url(base)):
        remote = {"base_url": base, "model": env.get("LLM_MODEL", "")}
    local_model = saved.get("local_model", "")
    if mode == "local":
        local_model = env.get("LLM_MODEL", "")
    return {"mode": mode, "env_exists": ENV_FILE.exists(), "base_url": base,
            "model": env.get("LLM_MODEL", ""), "timeout": env.get("LLM_TIMEOUT", "7"),
            "has_key": has_key, "remote": remote, "local_model": local_model}


def agent_uses_local_model():
    """智能体是不是在用本机的模型。**不是的话，本地模型就不必起。**

    没有显卡的机器上，本地模型要么起不来、要么用 CPU 慢到没法用；
    而「一键启动」原先总是先起模型、再**最多等 180 秒**才轮到后端和智能体——
    走远程 API 的人每次都要白等这三分钟，换来一个用不上的进程。
    所以以 agent\\.env 为准（判法见 llm_config），面板上「配置」一栏切回本地模型即可。
    """
    return llm_config()["mode"] == "local"


def _llm_call(base_url, key, path, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                 method="POST" if data is not None else "GET",
                                 headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _llm_error(exc, key):
    """把调用失败说成人话。**输出里不许出现 Key**——有的服务会把收到的 Key 回显在报错里。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", "replace")
            err = json.loads(detail).get("error", detail)
            detail = err.get("message", detail) if isinstance(err, dict) else err
        except Exception:
            detail = ""
        text = "HTTP %d %s" % (exc.code, str(detail)[:200])
        if exc.code in (401, 403):
            text += "（Key 不对或没有这个模型的权限）"
        elif exc.code == 404:
            text += "（地址不对：一般以 /v1 结尾）"
        elif exc.code == 503:
            text += "（服务方暂时没有可用渠道，常见于模型被下架——换一个模型试试）"
    elif isinstance(exc, urllib.error.URLError):
        text = "连不上：%s" % (exc.reason,)
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        text = "等了太久没有回应"
    else:
        text = "%s: %s" % (type(exc).__name__, exc)
    return text.replace(key, "***") if key else text


def _resolve_llm(body):
    """网页发来的 (mode, 地址, Key)。Key 留空就沿用 agent\\.env 里的——
    **但只在地址还是同一家时沿用**：换了服务商还拿旧 Key 去敲，等于把这家的 Key
    发给了另一家。"""
    mode = body.get("mode")
    if mode not in ("remote", "local", "rules"):
        raise ValueError("不认识的模式")
    env = read_env(ENV_FILE)
    stored = env.get("LLM_API_KEY", "")
    typed = str(body.get("api_key") or "").strip()
    if mode == "local":
        return mode, LOCAL_BASE, typed or stored or LOCAL_KEY
    base = str(body.get("base_url") or "").strip().rstrip("/")
    if mode == "remote":
        if not re.match(r"^https?://[^/\s]+", base):
            raise ValueError("服务地址要以 http:// 或 https:// 开头，一般以 /v1 结尾")
        if not typed:
            if not stored or stored == LOCAL_KEY:
                raise ValueError("还没有 API Key，要先填一个")
            # Key 属于哪一家：当前地址是本机时，看切到本地之前记下的远程地址
            home = env.get("LLM_BASE_URL", "")
            if is_local_url(home):
                home = load_panel_state().get("remote", {}).get("base_url", "")
            if not same_host(base, home):
                raise ValueError("换了服务地址，要重新填这一家的 API Key")
    return mode, base, typed or stored


def llm_models(body):
    try:
        mode, base, key = _resolve_llm(body)
        data = _llm_call(base, key, "/models", timeout=10)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": _llm_error(e, body.get("api_key") or read_env(ENV_FILE).get("LLM_API_KEY"))}
    ids = sorted({str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")})
    return {"ok": True, "models": ids}


def llm_test(body):
    """发一句最短的对话，量一下这家现在通不通、多快。

    量出来的耗时直接跟 LLM_TIMEOUT 比：**超过它的调用在智能体里会退回规则**，
    表面上不报错，只是回答变成了规则模板——那是最难察觉的一种「坏了」。
    """
    try:
        mode, base, key = _resolve_llm(body)
        timeout = float(body.get("timeout") or 7)
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    if mode == "rules":
        return {"ok": True, "message": "纯规则模式不调模型，不用测"}
    model = str(body.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "先选一个模型"}
    t0 = time.time()
    try:
        data = _llm_call(base, key, "/chat/completions", timeout=30, body={
            "model": model, "max_tokens": 64,
            "messages": [{"role": "user", "content": "只回复两个字：收到"}]})
    except Exception as e:
        return {"ok": False, "error": _llm_error(e, key), "seconds": round(time.time() - t0, 1)}
    seconds = round(time.time() - t0, 1)
    try:
        reply = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return {"ok": False, "error": "返回的不是对话格式，地址可能不是 OpenAI 兼容接口", "seconds": seconds}
    result = {"ok": True, "seconds": seconds, "reply": reply[:60]}
    # 这里测的是最短的一句；助手真答题时每次调用都带着完整的工具清单，还要调 2～4 次。
    # 2026-10-21 实测 gpt-6-sol：这里 4.2 秒，答题时每次平均 6.5 秒、慢的超过 30 秒；
    # 超时设 7 秒时 8 句里 7 句答不了——两次超时就熔断 60 秒，后面每句都秒回「连不上」。
    if seconds * 2 > timeout:
        result["warning"] = ("最短的一句就用了 %.1f 秒；助手答题时每次调用要慢一倍以上，超时 %g 秒偏紧——"
                             "超时的调用答不出来，连续两次还会暂停调用模型 60 秒。建议把超时调到 30 秒"
                             % (seconds, timeout))
    return result


def llm_save(body):
    try:
        mode, base, key = _resolve_llm(body)
        timeout = float(body.get("timeout") or 7)
        if not 1 <= timeout <= 300:
            raise ValueError("超时要在 1 到 300 秒之间")
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    env = read_env(ENV_FILE)
    model = str(body.get("model") or "").strip()
    changes = {"LLM_TIMEOUT": "%g" % timeout}
    state = load_panel_state()
    if mode == "rules":
        # 只清 provider：地址、模型、Key 都留着，切回来不用重填
        changes["LLM_PROVIDER"] = ""
    else:
        if not model:
            return {"ok": False, "error": "先选一个模型"}
        changes.update(LLM_PROVIDER=env.get("LLM_PROVIDER") or "openai_compatible",
                       LLM_BASE_URL=base, LLM_MODEL=model, LLM_API_KEY=key)
        if mode == "remote":
            state["remote"] = {"base_url": base, "model": model}
        else:
            state["local_model"] = model
    # 切走之前把远程的那套记下来，切回来时面板能原样填回去
    current = llm_config()
    if current["mode"] == "remote" and mode != "remote":
        state["remote"] = current["remote"]
    try:
        update_env(ENV_FILE, changes, template=ROOT / "agent" / ".env.example")
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    save_panel_state(state)
    running = status("agent")["state"] in ("running", "external", "starting", "stuck")
    if running:
        threading.Thread(target=apply_llm, args=(mode,), daemon=True).start()
    return {"ok": True, "message": "已保存，正在重启智能体（约 10 秒）" if running
            else "已保存。智能体没在运行，下次启动时用新配置"}


def apply_llm(mode):
    # 智能体改完 .env 必须整进程重启（load_dotenv 不覆盖已有环境变量），见文件头
    if mode == "local" and status("model")["state"] not in ("running", "external"):
        start("model")
        wait_healthy("model")
    stop("agent")
    start("agent")


AMAP_FIELDS = {  # 网页上的字段 -> (哪一端, 变量名)：web 写 web\.env.local，server 写 server\.env
    "web_key": ("web", "VITE_AMAP_KEY"),
    "web_security": ("web", "VITE_AMAP_SECURITY_JS_CODE"),
    "service_key": ("server", "AMAP_WEB_SERVICE_KEY"),
}


def amap_config():
    """三项高德凭据填了没有。同样**只说有没有，不回传值**。"""
    files = {"web": read_env(WEB_ENV), "server": read_env(SERVER_ENV)}
    return {field: bool(files[side].get(var)) for field, (side, var) in AMAP_FIELDS.items()}


def amap_save(body):
    """留空的项不动。Web 端两项是**构建时**写进前端的：5173 要重启，
    8080 托管的 dist 要重新 `npm run build`；服务端那项要重启业务后端。"""
    web, server = {}, {}
    for field, (side, var) in AMAP_FIELDS.items():
        value = str(body.get(field) or "").strip()
        if value:
            (web if side == "web" else server)[var] = value
    if not web and not server:
        return {"ok": False, "error": "没有填任何一项"}
    try:
        if web:
            update_env(WEB_ENV, web, template=ROOT / "web" / ".env.example")
        if server:
            update_env(SERVER_ENV, server, template=ROOT / "server" / ".env.example")
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    threading.Thread(target=apply_amap, args=(bool(web), bool(server)), daemon=True).start()
    steps = []
    if server:
        steps.append("重启业务后端")
    if web:
        steps.append("重启前端并重新构建网站（约 1 分钟）")
    return {"ok": True, "message": "已保存，正在" + "、".join(steps)}


def apply_amap(web, server):
    alive = ("running", "external", "starting", "stuck")
    if server and status("backend")["state"] in alive:
        stop("backend")
        start("backend")
    if web:
        if status("web")["state"] in alive:
            stop("web")
            start("web")
        run_build()


def run_build():
    """`npm run build`：8080 托管的是 web/dist，Key 改了不重新构建就还是旧的。"""
    with lock:
        if build["state"] == "running":
            return
        build.update(state="running", at=time.strftime("%H:%M:%S"), exit=None)
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / "build.log", "ab") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} npm run build =====\n".encode())
        log.flush()
        try:
            code = subprocess.run([find_npm() or "npm.cmd", "run", "build"], cwd=ROOT / "web", stdout=log,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  creationflags=CREATE_NO_WINDOW).returncode
        except OSError as e:
            log.write(f"{e}\n".encode())
            code = -1
    build.update(state="ok" if code == 0 else "failed", at=time.strftime("%H:%M:%S"), exit=code)


# 只服务于本地模型的两项：远程 API 模式下一键启动跳过它们
NEEDS_LOCAL_MODEL = ("model", "chat")


# ---------- lifecycle -------------------------------------------------------------------------
def status(name):
    svc = SERVICES[name]
    p = procs.get(name)
    ours = p is not None and p.poll() is None
    listening = port_open(svc["port"])
    ok = listening and http_ok(svc["health"])[0]
    note = ""
    if ok:
        state = "running" if ours else "external"
    elif ours:
        state = "starting"
        # 进程活着、端口却一直不应答：原先这里永远显示「启动中」，
        # pythonw 那次（见 find_python）就是这样看起来「在启动」看了好几分钟
        if time.time() - started.get(name, time.time()) > svc["boot"]:
            state = "stuck"
            note = "进程还在，但 %d 秒了还没应答，看下面的日志；多半要停掉重启" % svc["boot"]
    elif p is not None and p.poll() is not None:
        state = "exited"
    else:
        state = "stopped"
    if state == "stopped" and name in NEEDS_LOCAL_MODEL and not agent_uses_local_model():
        state = "not_needed"
        if llm_config()["mode"] == "rules":
            note = "智能体现在是纯规则模式，用不到本地模型"
        else:
            note = "智能体走的是远程 API：%s，用不到本地模型；需要时仍可手动启动" % agent_llm_target()
    return {"name": name, "title": svc["title"], "port": svc["port"], "state": state, "note": note,
            "optional": svc.get("optional", False), "exit": p.poll() if p is not None else None}


def start(name):
    with lock:
        svc = SERVICES[name]
        p = procs.get(name)
        if p is not None and p.poll() is None:
            return "already running"
        if port_open(svc["port"]):
            return f"port {svc['port']} is already in use by another process"
        if name == "web" and not (ROOT / "web" / "node_modules").exists():
            subprocess.run([find_npm() or "npm.cmd", "install", "--no-audit", "--no-fund"], cwd=ROOT / "web",
                           creationflags=CREATE_NO_WINDOW)
        if name == "backend" and not (ROOT / "server" / "rental.db").exists():
            subprocess.run([PY, "seed.py"], cwd=ROOT / "server", creationflags=CREATE_NO_WINDOW)
        if name == "agent":
            point_agent_at_model()
        LOGS.mkdir(exist_ok=True)
        log = open(LOGS / f"{name}.log", "ab")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start =====\n".encode())
        log.flush()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        started[name] = time.time()
        procs[name] = subprocess.Popen(svc["cmd"](), cwd=svc["cwd"], stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=env, creationflags=CREATE_NO_WINDOW)
        return "started"


def stop(name):
    with lock:
        svc = SERVICES[name]
        p = procs.pop(name, None)
        if p is not None and p.poll() is None:
            kill_tree(p.pid)
        # Also whatever else holds the port (e.g. windows left over from start-all.ps1).
        for pid in port_owner_pids(svc["port"]):
            kill_tree(pid)
        if name == "model":
            # start-ninfer.ps1 runs the engine as a child; make sure no engine survives.
            subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve.exe"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        for _ in range(20):
            if not port_open(svc["port"]):
                break
            time.sleep(0.5)
        return "stopped"


def wait_healthy(name):
    svc = SERVICES[name]
    deadline = time.time() + svc["boot"]
    while time.time() < deadline:
        if status(name)["state"] in ("running", "external"):
            return True
        p = procs.get(name)
        if p is not None and p.poll() is not None:
            return False
        time.sleep(1)
    return False


def start_all(include_optional=True):
    # Model first so the agent comes up already talking to it.
    # 智能体走远程 API 时跳过本地模型与模型对话页——见 agent_uses_local_model()
    local = agent_uses_local_model()
    for name in ORDER:
        if SERVICES[name].get("optional") and not include_optional:
            continue
        if name in NEEDS_LOCAL_MODEL and not local:
            continue
        start(name)
        if name == "model":
            wait_healthy("model")


def restart_model():
    stop("model")
    start("model")
    wait_healthy("model")
    # The agent caches the model's reachability per process; restart it only if we own it.
    if procs.get("agent") is not None and procs["agent"].poll() is None:
        stop("agent")
        start("agent")


def tail(name, n=200):
    path = LOGS / f"{name}.log"
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 64000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-n:])


def model_info():
    ok, _ = http_ok(f"http://127.0.0.1:{MODEL_PORT}/v1/models")
    info = {"up": ok}
    # The engine prints a "[kvmem-cont] trim" line per request, so the startup lines scroll out of
    # a short tail quickly; look further back and only after the latest start marker.
    path = LOGS / "model.log"
    if not path.exists():
        return info
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4_000_000))
        log = f.read().decode("utf-8", "replace")
    log = log[log.rfind("===== "):] if "===== " in log else log
    m = re.findall(r"capacity \| KV ([\d,]+) tokens", log)
    if m:
        info["context"] = m[-1]
    m = re.findall(r"(?:极速档|长文档档)[^\n]*", log)
    if m:
        info["profile_line"] = m[-1]
    return info


def agent_llm():
    ok, body = http_ok("http://127.0.0.1:8000/health")
    if not ok:
        return None
    try:
        return json.loads(body).get("llm")
    except ValueError:
        return None


# ---------- HTTP --------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _trusted(self, post):
        """只认面板自己的网页。

        面板能改 API Key 和服务地址之后，这一条就不是可有可无了：随便哪个网页都能往
        127.0.0.1:8099 发一个 text/plain 的「简单请求」（不触发 CORS 预检），
        把 LLM_BASE_URL 改到别人的服务器上——智能体下一次调用就把 Key 送过去了。

        - Host 必须是 127.0.0.1 / localhost 加本端口：挡 DNS 重绑定；
        - POST 必须是 application/json：这种请求跨站要先预检，而面板不应答 OPTIONS；
        - 带了 Origin 就必须是面板自己。
        """
        port = self.server.server_port
        hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        if self.headers.get("Host", "") not in hosts:
            return False
        if post:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + h for h in hosts}:
                return False
        return True

    def do_GET(self):
        if not self._trusted(post=False):
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "panel.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, {"services": [status(n) for n in ORDER], "settings": settings,
                                    "gpu": gpu(), "model": model_info(), "agent_llm": agent_llm(),
                                    "llm_mode": llm_config()["mode"], "build": build})
        if self.path == "/api/config":
            return self._send(200, {"llm": llm_config(), "amap": amap_config(), "build": build})
        if self.path.startswith("/api/log?"):
            name = self.path.split("name=", 1)[-1].split("&")[0]
            if name not in SERVICES and name != "build":
                return self._send(404, {"error": "unknown service"})
            return self._send(200, tail(name).encode(), "text/plain; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # 先把请求体读完再决定要不要拒：没读的数据还留在 socket 里时就关连接，
        # Windows 会发 RST，对方收到的是「连接被中止」而不是 403（测试里时有时无）
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if not 0 <= n <= 1_000_000:
            self.close_connection = True
            return self._send(413, {"error": "body too large"})
        raw = self.rfile.read(n)
        if not self._trusted(post=True):
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "bad request"})
        action, name = body.get("action"), body.get("name")
        handlers = {"llm_models": llm_models, "llm_test": llm_test, "llm_save": llm_save, "amap_save": amap_save}
        if action in handlers:
            return self._send(200, handlers[action](body))
        if action in ("start", "stop", "restart") and name in SERVICES:
            if action in ("stop", "restart"):
                stop(name)
            if action in ("start", "restart"):
                threading.Thread(target=(restart_model if name == "model" and action == "restart" else start),
                                 args=() if name == "model" and action == "restart" else (name,), daemon=True).start()
            return self._send(200, {"ok": True})
        if action == "start_all":
            threading.Thread(target=start_all, daemon=True).start()
            return self._send(200, {"ok": True})
        if action == "stop_all":
            for name in reversed(ORDER):
                stop(name)
            return self._send(200, {"ok": True})
        if action == "settings":
            changed = False
            for k in ("profile", "vision", "thinking"):
                if k in body and body[k] != settings[k]:
                    settings[k] = body[k]
                    changed = True
            if changed and status("model")["state"] != "stopped":
                threading.Thread(target=restart_model, daemon=True).start()
            return self._send(200, {"ok": True, "restarting_model": changed})
        if action == "quit":
            for name in reversed(ORDER):
                stop(name)
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
            return
        self._send(400, {"error": "bad request"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--autostart", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if port_open(a.port):
        # A panel is already running: just open it.
        if not a.no_browser:
            webbrowser.open(f"http://127.0.0.1:{a.port}/")
        sys.exit(0)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if a.autostart:
        threading.Thread(target=start_all, daemon=True).start()
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}/")).start()
    server.serve_forever()
