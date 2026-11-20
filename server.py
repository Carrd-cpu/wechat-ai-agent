"""零依赖 Web 服务：静态页面 + Agent REST API。

启动：python server.py    （默认 http://127.0.0.1:8000）
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent import CustomerServiceAgent

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
AGENT = CustomerServiceAgent()
LOCK = threading.RLock()  # 会话与文件读写串行化，避免并发写坏 sessions.json


class Handler(SimpleHTTPRequestHandler):
    server_version = "AICustomerService/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # ---------------- 基础 ---------------- #
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # 控制台只保留错误，避免刷屏
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---------------- 路由 ---------------- #
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._api_get(parsed)
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        return self._api_post(parsed)

    def _api_get(self, parsed):
        if parsed.path == "/api/state":
            customer_id = (parse_qs(parsed.query).get("customer_id") or ["c1"])[0]
            with LOCK:
                return self._send_json(AGENT.state(customer_id))
        if parsed.path == "/api/skills":
            return self._send_json({"skills": [s.to_dict(with_body=True) for s in AGENT.skills.skills]})
        self.send_error(404)

    def _api_post(self, parsed):
        try:
            with LOCK:
                return self._handle_post(parsed)
        except Exception as exc:  # 任何异常都回 JSON，别让前端拿 HTML 报错页
            print(f"[error] {type(exc).__name__}: {exc}")
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _handle_post(self, parsed):
        body = self._read_json()
        customer_id = body.get("customer_id") or "c1"

        if parsed.path == "/api/chat":
            AGENT.handle(customer_id, body.get("text", ""))
            return self._send_json(AGENT.state(customer_id))

        if parsed.path == "/api/human":
            if body.get("on"):
                AGENT.tools.transfer_human(customer_id, body.get("reason") or "客服手动接管")
            else:
                session = AGENT.session(customer_id)
                session["status"] = "ai"
                AGENT.push_message(customer_id, "system", "人工已恢复 AI 自动接待")
            return self._send_json(AGENT.state(customer_id))

        if parsed.path == "/api/human-say":
            text = (body.get("text") or "").strip()
            if text:
                AGENT.push_message(customer_id, "human", text)
            return self._send_json(AGENT.state(customer_id))

        if parsed.path == "/api/reset":
            return self._send_json(AGENT.reset(customer_id))

        self.send_error(404)


def pick_port(start: int = 8000, end: int = 8010) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("8000-8010 端口都被占用了，请先关闭其他服务。")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    runtime = AGENT.runtime()

    print("=" * 60)
    print(" 企业微信 AI 智能客服助手（模拟版）")
    print(f" 地址      ：{url}")
    print(f" 生成引擎  ：{runtime['mode']}  ({runtime['model']})")
    print(f" 已加载技能：{', '.join(s['name'] for s in runtime['skills'])}")
    if not runtime["llm_enabled"]:
        print(" 提示      ：未检测到 LLM_API_KEY，当前为离线规则模式（功能完整可演示）")
    print(" 停止服务  ：Ctrl + C")
    print("=" * 60)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
