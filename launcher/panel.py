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

settings = {"profile": "fast", "vision": True, "thinking": True}
procs = {}          # name -> Popen started by this panel
lock = threading.Lock()


def find_python():
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


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


def agent_llm_target():
    """agent\\.env 里 LLM_BASE_URL 的值；没有文件或没写就是空串。"""
    if not ENV_FILE.exists():
        return ""
    text = ENV_FILE.read_bytes().decode("utf-8-sig", "replace")
    m = re.search(r"^\s*LLM_BASE_URL\s*=\s*(\S*)", text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def agent_uses_local_model():
    """智能体是不是在用本机的模型。**不是的话，本地模型就不必起。**

    没有显卡的机器上，本地模型要么起不来、要么用 CPU 慢到没法用；
    而「一键启动」原先总是先起模型、再**最多等 180 秒**才轮到后端和智能体——
    走远程 API 的人每次都要白等这三分钟，换来一个用不上的进程。
    所以以 agent\\.env 为准：LLM_BASE_URL 指向 127.0.0.1 / localhost 才算用本地模型。
    想换回本地模型，把那一行改回 http://127.0.0.1:18241/v1 即可，面板会照旧先起模型。
    """
    target = agent_llm_target()
    return bool(re.match(r"https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", target))


# 只服务于本地模型的两项：远程 API 模式下一键启动跳过它们
NEEDS_LOCAL_MODEL = ("model", "chat")


# ---------- lifecycle -------------------------------------------------------------------------
def status(name):
    svc = SERVICES[name]
    p = procs.get(name)
    ours = p is not None and p.poll() is None
    listening = port_open(svc["port"])
    ok = listening and http_ok(svc["health"])[0]
    if ok:
        state = "running" if ours else "external"
    elif ours:
        state = "starting"
    elif p is not None and p.poll() is not None:
        state = "exited"
    else:
        state = "stopped"
    note = ""
    if state == "stopped" and name in NEEDS_LOCAL_MODEL and not agent_uses_local_model():
        state = "not_needed"
        target = agent_llm_target() or "（未配置，纯规则模式）"
        note = "智能体走的是远程 API：%s，用不到本地模型；需要时仍可手动启动" % target
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

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "panel.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, {"services": [status(n) for n in ORDER], "settings": settings,
                                    "gpu": gpu(), "model": model_info(), "agent_llm": agent_llm()})
        if self.path.startswith("/api/log?"):
            name = self.path.split("name=", 1)[-1].split("&")[0]
            if name not in SERVICES:
                return self._send(404, {"error": "unknown service"})
            return self._send(200, tail(name).encode(), "text/plain; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        action, name = body.get("action"), body.get("name")
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
