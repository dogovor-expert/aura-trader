# -*- coding: utf-8 -*-
"""Webhook bridge: Polymarket Oracle -> Hummingbot.
Receives POST /signal {"signal": {...}} and forwards to Hummingbot API
or writes to a strategy file watched by Hummingbot.

Run: venv/Scripts/python src/bridge.py  (port 8181)
Hummingbot side: configure webhook or read D:/Hummingbot/signals.json
"""
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIG_FILE = "D:/Hummingbot/signals.json"
# тот же файл внутри маунта контейнера Hummingbot (D:/Hummingbot/data -> /home/hummingbot/data)
SIG_FILE_MOUNTED = "D:/Hummingbot/data/signals.json"
LOG_FILE = os.path.join(BASE, "logs", "bridge.log")
# каталог панели: src/web/index.html


def _load_token():
    """Токен доступа к мосту. Иначе любой сторонний сайт мог бы послать
    POST на localhost и поменять настройки/стратегию (CSRF/DNS-rebind)."""
    tok = os.getenv("BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.join(BASE, "config", "bridge_token.txt")
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok)
        os.chmod(path, 0o600)
        print(f"[bridge] generated access token -> {path}")
    except OSError:
        pass
    return tok


TOKEN = _load_token()



def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return {"__error__": str(e)[:150]}

    def _check_auth(self):
        """Токен в заголовке X-Bridge-Token или в query (?token=...)."""
        tok = self.headers.get("X-Bridge-Token", "") or ""
        if not tok:
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in qs.split("&"):
                if part.startswith("token="):
                    tok = part[6:]
                    break
        return bool(tok) and secrets.compare_digest(tok, TOKEN)

    def _send_dashboard(self):
        """Отдаёт панель, подставляя токен вместо плейсхолдера."""
        path = os.path.join(WEB_DIR, "index.html")
        try:
            with open(path, encoding="utf-8") as f:
                html = f.read().replace("__BRIDGE_TOKEN__", TOKEN)
        except OSError:
            return self._send(404, {"error": "dashboard not found"})
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._check_auth():
            log(f"unauthorized POST {path} from {self.client_address[0]}")
            return self._send(401, {"ok": False, "error": "unauthorized: missing/invalid bridge token"})
        if path in ("/api/settings", "/api/strategy", "/api/trading"):
            import strategy_control
            data = self._read_json_body()
            if "__error__" in data:
                return self._send(400, {"ok": False, "error": data["__error__"]})
            if path == "/api/settings":
                values = data.get("settings") or {}
                log(f"settings apply: {json.dumps(values, ensure_ascii=False)}")
                res = strategy_control.apply_settings(values)
            elif path == "/api/trading":
                if "mode" in data:
                    log(f"trade mode -> {data.get('mode')}")
                    res = strategy_control.set_trade_mode(data.get("mode"))
                elif "close_all" in data:
                    enabled_now = strategy_control.trading_state()
                    res = {"ok": True, "state": strategy_control.request_close_all(
                        str(data.get("close_all") or "закрыть всё по кнопке")),
                        "trading_enabled": bool(enabled_now.get("trading_enabled", False))}
                    log(f"close_all requested: {res['state'].get('close_all_reason')}")
                else:
                    enabled = bool(data.get("enabled"))
                    note = str(data.get("note") or "")[:120]
                    log(f"trading switch -> enabled={enabled}")
                    res = {"ok": True, "state": strategy_control.set_trading(enabled, note)}
            else:
                sid = data.get("id") or ""
                log(f"strategy switch -> {sid}")
                res = strategy_control.switch_strategy(sid)
            log(f"{path} result ok={res.get('ok')} {res.get('error', '')}")
            return self._send(200 if res.get("ok") else 400, res)
        if path not in ("/signal", "/webhook"):
            return self._send(404, {"error": "use /signal"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": str(e)[:200]})
        sig = data.get("signal", data)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "signal": sig}
        try:
            for target in (SIG_FILE, SIG_FILE_MOUNTED):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(entry, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return self._send(500, {"error": str(e)[:200]})
        log(f"signal -> {SIG_FILE} + {SIG_FILE_MOUNTED}: {json.dumps(sig, ensure_ascii=False)[:300]}")
        return self._send(200, {"ok": True, "file": SIG_FILE})


    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/dashboard"):
            return self._send_dashboard()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/api/state", "/api/strategies") and not self._check_auth():
            return self._send(401, {"error": "unauthorized: missing/invalid bridge token"})
        if path == "/api/state":
            try:
                import dashboard_data
                return self._send(200, dashboard_data.collect_state())
            except Exception as e:
                log(f"api/state error: {e}")
                return self._send(500, {"error": str(e)[:200]})
        if path == "/api/strategies":
            try:
                import strategy_control
                return self._send(200, strategy_control.catalog())
            except Exception as e:
                log(f"api/strategies error: {e}")
                return self._send(500, {"error": str(e)[:200]})
        if path in ("/health", "/healthz"):
            return self._send(200, {"ok": True, "service": "oracle-bridge",
                                    "dashboard": "/", "api": "/api/state"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.getenv("BRIDGE_PORT", "8181"))
    # Threading: сбор состояния читает логи и docker, и не должен блокировать
    # загрузку страницы и /health.
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    log(f"bridge listening on 127.0.0.1:{port}, file={SIG_FILE}")
    srv.serve_forever()
