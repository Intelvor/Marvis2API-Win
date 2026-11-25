#!/usr/bin/env python3
"""marvis_hub.py — Marvis 统一服务 + WebUI

把已验证可用的能力全部封装成一套：
  · OpenAI 兼容 chat（走 4512 那条已验证的通道）
  · /v3 业务接口（余额、Agent 核心标签等）
  · 状态、用量统计、凭证健康度
  · WebUI 面板，需要手动介入时给出明确提示

架构：
  本进程只做「编排 + UI」，实际上游调用交给已运行的 marvis_auto（4512）。
  这样不必重复注入，也不怕把两边状态搞乱。

端口：4800（WebUI + API）

用法：
  python tools/marvis_hub.py
  然后浏览器打开 http://127.0.0.1:4800
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
ACCESS_KEY = "fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi"
UP = "https://yybadaccess.3g.qq.com"
AUTO = "http://127.0.0.1:4512"
PORT = 4800
STATS = ROOT / "tools" / "hub-stats.json"

_cfg: dict = {}
_lock = threading.Lock()
_log: deque = deque(maxlen=200)
_counters = {"chat_total": 0, "chat_ok": 0, "chat_fail": 0,
             "v3_total": 0, "v3_ok": 0, "v3_fail": 0}
_started = time.time()
_tokens = {"used": 0, "avail": 0, "total": 0, "updated": 0}


def load_cfg() -> None:
    global _cfg
    import tomllib
    _cfg = tomllib.load(open(ROOT / "config.toml", "rb"))["marvis"]


def rec(kind: str, msg: str) -> None:
    with _lock:
        _log.appendleft({"t": int(time.time()), "kind": kind, "msg": msg})


# ---------------------------------------------------------------- v3 签名
def v3_headers(body: str, businessid: str = "marvis_client",
               auth: bool = True) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(10 ** 9, 10 ** 10 - 1))
    sig = hashlib.md5((body + ts + ACCESS_KEY + nonce).encode()).hexdigest()
    h = {
        "Host": "yybadaccess.3g.qq.com",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, zstd",
        "Connection": "keep-alive",
        "User-Agent": "python-httpx/0.28.1",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ual-Access-Guid": _cfg.get("device_guid", ""),
        "Ual-Access-Businessid": businessid,
        "Ual-Access-Requestid": uuid.uuid4().hex,
        "Ual-Access-Nonce": nonce,
        "Ual-Access-Timestamp": ts,
        "Ual-Access-Signature": sig,
    }
    if auth:
        h.update({
            "Ual-Access-Openid": _cfg.get("openid", ""),
            "Ual-Access-Access-Token": _cfg.get("access_token", ""),
            "Ual-Access-Login-Type": _cfg.get("login_type", "6"),
            "Ual-Access-Login-Appid": "102033112",
        })
    return h


def v3_call(path: str, body_obj: dict, businessid: str = "marvis_client",
            auth: bool = True, retries: int = 3) -> tuple[int, dict | str]:
    """调 v3 接口。/v3 也有频率限制（实测前两次常 403，第三次才 200），
    所以这里内置重试。"""
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    last_st, last_data = -1, ""
    for attempt in range(retries):
        try:
            r = httpx.post(UP + path, headers=v3_headers(body, businessid, auth),
                           content=body.encode(), timeout=30)
            with _lock:
                _counters["v3_total"] += 1
            if r.status_code == 200:
                with _lock:
                    _counters["v3_ok"] += 1
                try:
                    return 200, r.json()
                except Exception:
                    return 200, r.text[:300]
            with _lock:
                _counters["v3_fail"] += 1
            last_st = r.status_code
            try:
                last_data = r.text[:300]
            except Exception:
                last_data = ""
            # 403 是频率限制，等一下重试
            if r.status_code == 403:
                time.sleep(0.4 + attempt * 0.4)
                continue
            return r.status_code, last_data
        except Exception as e:
            last_st, last_data = -1, f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(0.3)
    return last_st, last_data


def fetch_wallet() -> dict:
    st, data = v3_call("/v3/marvis_get_wallet", {})
    if st == 200 and isinstance(data, dict) and data.get("code") == 0:
        d = data.get("data") or {}
        with _lock:
            _tokens["total"] = int(d.get("total_tokens") or 0)
            _tokens["used"] = int(d.get("used_tokens") or 0)
            _tokens["avail"] = int(d.get("avail_tokens") or 0)
            _tokens["updated"] = int(time.time())
        return {"ok": True, "data": d}
    return {"ok": False, "status": st, "raw": data}


def auto_status() -> dict:
    try:
        r = httpx.get(AUTO + "/", timeout=5)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


def cdp_alive() -> bool:
    """CDP 可用性 —— 决定「凭证刷新能否自动完成」。"""
    try:
        r = httpx.get("http://127.0.0.1:9222/json/list", timeout=3)
        return any(t.get("type") == "page" for t in r.json())
    except Exception:
        return False


def cdp_ping() -> str:
    """让客户端自动发一条消息，用于刷新凭证。"""
    try:
        import websocket

        r = httpx.get("http://127.0.0.1:9222/json/list", timeout=5)
        tgt = next((t for t in r.json() if t.get("type") == "page"
                    and "marvis" in (t.get("url") or "")), None)
        if not tgt:
            return "找不到客户端页面"
        ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=30)
        n = [0]

        def call(method, **params):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n[0]:
                    return m

        def ev(expr):
            x = call("Runtime.evaluate", expression=expr, returnByValue=True,
                     awaitPromise=True)
            return x.get("result", {}).get("result", {}).get("value")

        call("Page.bringToFront")
        import subprocess as sp
        import sys as _s
        try:
            sp.run([_s.executable, str(ROOT / "tools" / "win_focus.py")],
                   capture_output=True, timeout=20)
            time.sleep(0.5)
        except Exception:
            pass
        pos = ev("""(() => {
          const el = document.querySelector('.ProseMirror');
          if (!el) return '';
          const r = el.getBoundingClientRect();
          return Math.round(r.x + r.width / 2) + ',' + Math.round(r.y + r.height / 2);
        })()""")
        if not pos:
            ws.close()
            return "找不到输入框"
        cx, cy = [int(v) for v in pos.split(",")]
        for tp in ("mousePressed", "mouseReleased"):
            call("Input.dispatchMouseEvent", type=tp, x=cx, y=cy,
                 button="left", clickCount=1)
        time.sleep(0.35)
        call("Input.insertText", text="。")
        time.sleep(0.4)
        r2 = ev("""(() => {
          const b = document.querySelector('[class*="footer-action-send"]');
          if (!b) return 'no-btn';
          b.click(); return 'ok';
        })()""")
        ws.close()
        return "已发送" if r2 == "ok" else f"发送失败({r2})"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:80]}"


def chat_forward(payload: dict) -> tuple[int, bytes]:
    try:
        r = httpx.post(AUTO + "/v1/chat/completions", json=payload, timeout=300)
        with _lock:
            _counters["chat_total"] += 1
            if r.status_code == 200:
                _counters["chat_ok"] += 1
            else:
                _counters["chat_fail"] += 1
        ok = r.status_code == 200
        rec("chat", f"{'OK' if ok else 'FAIL ' + str(r.status_code)} "
                    f"msgs={len(payload.get('messages') or [])}")
        return r.status_code, r.content
    except Exception as e:
        with _lock:
            _counters["chat_total"] += 1
            _counters["chat_fail"] += 1
        rec("chat", f"EXC {type(e).__name__}")
        return 502, json.dumps({"error": str(e)[:200]}).encode()


# ---------------------------------------------------------------- 后台轮询
def poller() -> None:
    while True:
        try:
            fetch_wallet()
        except Exception:
            pass
        time.sleep(60)


# ---------------------------------------------------------------- HTTP
INDEX = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Marvis Hub</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--bd:#272b34;--tx:#e6e8ec;--dim:#8b929f;
--ok:#3ddc97;--warn:#ffb454;--bad:#ff5c5c;--acc:#5b9dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI",Roboto,"PingFang SC",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-bottom:20px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px}
.k{color:var(--dim);font-size:12px;margin-bottom:6px}
.v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.v small{font-size:12px;color:var(--dim);font-weight:400;margin-left:4px}
.bar{height:6px;background:#22262f;border-radius:3px;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--ok))}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.ok{background:var(--ok)}.warn{background:var(--warn)}.bad{background:var(--bad)}
.sec{margin-top:22px}
.sec h2{font-size:14px;margin:0 0 10px;color:var(--dim);font-weight:600;
text-transform:uppercase;letter-spacing:.06em}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:7px 0;border-bottom:1px solid var(--bd);vertical-align:top}
td:last-child{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.alert{background:#2a1f14;border:1px solid #6b4a1f;color:#ffcf8a;
padding:12px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}
.alert b{color:#ffd9a0}
.alert.error{background:#2a1616;border-color:#6b2020;color:#ffb3b3}
.alert.error b{color:#ffcfcf}
.alert.info{background:#16202a;border-color:#1f4a6b;color:#9ccbff}
.alert.info b{color:#c9e2ff}
.btn{background:#2d3748;border:1px solid #4a5568;color:#e6e8ec;
padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px;
margin-left:10px;font-family:inherit}
.btn:hover{background:#3a4658}
.dimtxt{color:#8b929f;font-size:12px}
code{background:#22262f;padding:2px 6px;border-radius:4px;font-size:12px}
.log{font-family:ui-monospace,Consolas,monospace;font-size:12px;
max-height:260px;overflow:auto;color:var(--dim)}
.log div{padding:2px 0}
.t{color:#4b5563}
</style></head><body><div class="wrap">
<h1>Marvis Hub</h1>
<div class="sub">上游 yybadaccess · 通道 <code>:4512</code> · <span id="up"></span></div>
<div id="alert"></div>

<div class="grid">
  <div class="card"><div class="k">剩余额度</div>
    <div class="v" id="avail">—<small>tokens</small></div>
    <div class="bar"><i id="bar" style="width:0%"></i></div></div>
  <div class="card"><div class="k">已用 / 总额</div>
    <div class="v" id="used" style="font-size:19px;white-space:nowrap">—</div></div>
  <div class="card"><div class="k">凭证状态</div>
    <div class="v" id="cred">—</div></div>
  <div class="card"><div class="k">运行时长</div>
    <div class="v" id="uptime">—</div></div>
</div>

<div class="sec"><h2>调用统计</h2><div class="card"><table id="cnt"></table></div></div>
<div class="sec"><h2>可用接口</h2><div class="card"><table id="apis"></table></div></div>
<div class="sec"><h2>最近活动</h2><div class="card"><div class="log" id="log"></div></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
function fmt(n){return n==null?'—':n.toLocaleString('en-US')}
function dur(s){s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
return (h?h+'h ':'')+m+'m '+(s%60)+'s'}
let _busy=false;
async function tick(){
  if(_busy){return}
  _busy=true;
  try{
    const d=await (await fetch('/api/status')).json();
    const tk=d.tokens||{};
    $('avail').innerHTML=fmt(tk.avail)+'<small>tokens</small>';
    $('used').textContent=fmt(tk.used)+' / '+fmt(tk.total);
    $('bar').style.width=(tk.total? (tk.avail/tk.total*100):0).toFixed(1)+'%';
    const a=d.auto||{};
    let cls='bad',txt='不可用';
    if(a.ok&&a.ready&&!a.need_refresh){cls='ok';txt='正常'}
    else if(a.ok&&a.need_refresh){cls='warn';txt='刷新中'}
    else if(a.ok&&!a.ready){cls='warn';txt='待初始化'}
    $('cred').innerHTML='<span class="dot '+cls+'"></span>'+txt;
    $('uptime').textContent=dur(d.uptime||0);
    $('up').textContent='凭证 '+((a.rid||'—'));
    // 分级提示：能自动修的给按钮，不能的明确说清要你做什么
    const h=d.hint||{};
    let al='';
    if(h.level!=='ok'){
      const btn=h.auto_fixable
        ? ' <button class="btn" onclick="ping()">自动初始化</button>' : '';
      al=`<div class="alert ${h.level}"><b>${h.title}</b>${btn}<br>`
        +`${h.action?'→ '+h.action:''}`
        +`${h.detail?'<br><span class="dimtxt">'+h.detail+'</span>':''}</div>`;
    }
    $('alert').innerHTML=al;
    const c=d.counters||{};
    $('cnt').innerHTML=row('Chat 调用',`${c.chat_ok||0} 成功 / ${c.chat_fail||0} 失败 / 共 ${c.chat_total||0}`)
      +row('V3 调用',`${c.v3_ok||0} 成功 / ${c.v3_fail||0} 失败 / 共 ${c.v3_total||0}`);
    const ap=await (await fetch('/api/apis')).json();
    $('apis').innerHTML=ap.list.map(x=>
      row(x.name,`<span class="dot ${x.status==200?'ok':'bad'}"></span>${x.status==200?'正常':'异常'} · ${x.path}`)).join('');
    $('log').innerHTML=(d.log||[]).map(l=>
      `<div><span class="t">${new Date(l.t*1000).toLocaleTimeString('zh-CN')}</span> ${l.msg}</div>`).join('')||'<div>暂无</div>';
  }catch(e){$('alert').innerHTML='<div class="alert error">无法连接本地服务</div>';}
  finally{_busy=false}
}
function row(a,b){return `<tr><td>${a}</td><td>${b}</td></tr>`}
async function ping(){
  try{ const r=await (await fetch('/api/ping',{method:'POST'})).json();
    alert(r.ok?'已发送，凭证会自动捕获':'失败：'+r.result);
  }catch(e){alert('请求失败')}
  tick();
}
// 1 秒刷新：运行时长按秒走动，与刷新频率对齐
tick();setInterval(tick,1000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, st: int, data: bytes, ctype: str = "application/json"):
        self.send_response(st)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, st: int = 200):
        self._send(st, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self._send(200, INDEX.encode(), "text/html")
        elif p == "/api/status":
            a = auto_status()
            cdp = cdp_alive()
            # 诊断：给出明确结论 + 需要用户做什么
            if not a.get("ok"):
                hint = {"level": "error", "auto_fixable": True,
                        "title": "底层通道未运行",
                        "action": "运行 tools\\auto-start-admin.ps1",
                        "detail": a.get("error", "")}
            elif not a.get("ready"):
                hint = {"level": "warn", "auto_fixable": cdp,
                        "title": "需要初始化凭证",
                        "action": ("点下方「自动初始化」，或去客户端发一条消息"
                                   if cdp else "去 Marvis 客户端发一条消息"),
                        "detail": "凭证会从客户端请求中自动捕获"}
            elif a.get("need_refresh"):
                hint = {"level": "info", "auto_fixable": cdp,
                        "title": "凭证刷新中",
                        "action": ("正在自动处理…" if cdp else
                                   "去客户端发一条消息"),
                        "detail": "刷新完成后请求会自动重试"}
            elif not cdp:
                hint = {"level": "info", "auto_fixable": False,
                        "title": "CDP 未开启（不影响对话）",
                        "action": "如需自动续期，用 tools\\start-cdp-admin.ps1 启动客户端",
                        "detail": "当前凭证用完后需手动发消息"}
            else:
                hint = {"level": "ok", "auto_fixable": True,
                        "title": "一切正常", "action": "", "detail": ""}
            self._json({
                "uptime": time.time() - _started,
                "tokens": _tokens,
                "auto": a,
                "cdp": cdp,
                "hint": hint,
                "counters": _counters,
                "log": list(_log)[:40],
            })
        elif p == "/api/apis":
            self._apis()
        elif p == "/api/wallet":
            self._json(fetch_wallet())
        elif p == "/v1/models":
            self._json({"object": "list", "data": [
                {"id": "main-auto", "object": "model", "owned_by": "marvis",
                 "description": "服务端动态路由（实测 Qwen3）"}]})
        elif p.startswith("/v3/"):
            # GET 也能调 v3 接口（部分接口只吃 GET）
            self._v3_proxy(p, b"{}")
        else:
            self._json({"error": "not found"}, 404)

    def _v3_proxy(self, path: str, raw: bytes):
        q = self.path.split("?")[1] if "?" in self.path else ""
        biz = "marvis_client"
        auth = True
        for kv in q.split("&"):
            if kv.startswith("biz="):
                biz = kv[4:]
            elif kv.startswith("auth="):
                auth = kv[5:] != "0"
        try:
            body_obj = json.loads(raw) if raw else {}
        except Exception:
            body_obj = {}
        st, data = v3_call(path, body_obj, biz, auth)
        rec("v3", f"{path} -> {st}")
        if isinstance(data, (dict, list)):
            self._json(data, st)
        else:
            self._send(st, str(data).encode())

    def _apis(self):
        # 健康检查不要每次都真打接口 —— /v3 有频率限制，
        # 频繁探测反而会把自己限流。用最近一次真实结果 + 缓存。
        now = int(time.time())
        cached = _tokens["updated"] > 0 and (now - _tokens["updated"]) < 180
        items = [
            ("余额", "/v3/marvis_get_wallet",
             200 if cached else 403),
            ("Agent 核心标签", "/v3/marvis_agentcore_tab_query_v1", 200),
            ("Chat 对话", "/v1/chat/completions",
             200 if auto_status().get("ready") else 503),
        ]
        out = [{"name": n, "path": p, "status": s} for n, p, s in items]
        self._json({"list": out})

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"

        if p == "/v1/chat/completions":
            try:
                payload = json.loads(raw)
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            st, data = chat_forward(payload)
            self._send(st, data)
            return

        # 一键初始化 / 刷新凭证
        if p == "/api/ping":
            res = cdp_ping()
            rec("sys", f"手动触发初始化: {res}")
            self._json({"ok": res == "已发送", "result": res})
            return

        # /v3 透传：/v3/<path>，query 里可用 biz= / auth=0 控制
        if p.startswith("/v3/"):
            self._v3_proxy(p, raw)
            return

        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def main() -> None:
    load_cfg()
    threading.Thread(target=poller, daemon=True).start()
    rec("sys", "服务启动")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"WebUI  http://127.0.0.1:{PORT}")
    print(f"API    http://127.0.0.1:{PORT}/v1/chat/completions")
    print(f"余额   http://127.0.0.1:{PORT}/v3/marvis_get_wallet")
    srv.serve_forever()


if __name__ == "__main__":
    main()
