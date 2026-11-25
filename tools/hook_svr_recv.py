#!/usr/bin/env python3
"""hook_svr_recv.py — hook MarvisSvr 的接收与发送，看清隧道协议

已确认：
  MarvisSvr.exe 监听 127.0.0.1:5284，是 "sctun" 隧道服务（HTTPS）
  MarvisAgent 把请求交给它，它再经 TRPC/IPv6 发往腾讯后端
  sctun 需要专用协议，直接 POST 返回空

本脚本 hook MarvisSvr 的 ws2_32 recv/send 以及 SSL_read/SSL_write，
抓取它与 MarvisAgent、与腾讯后端之间的真实报文。

用法（需管理员）：
  python tools/hook_svr_recv.py --start / --run / --stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "svrrecv.log"
PIDFILE = ROOT / "tools" / "svrrecv.pid"

JS = r"""
(function () {
  const hooked = [], info = [];

  // recv：从 socket 收到（客户端请求）
  try {
    const ws = Process.findModuleByName('ws2_32.dll');
    if (ws) {
      const r = ws.findExportByName('recv');
      if (r) {
        Interceptor.attach(r, {
          onEnter(args) { this.buf = args[1]; this.len = args[2].toInt32(); },
          onLeave(ret) {
            try {
              const n = ret.toInt32();
              if (n > 0 && n < 1000000) {
                const u8 = new Uint8Array(this.buf.readByteArray(Math.min(n, 6000)));
                let pr = 0;
                for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
                send({kind:'recv', len:n, ratio: pr/u8.length,
                      text: String.fromCharCode.apply(null, u8.slice(0, 3000))});
              }
            } catch (e) {}
          }
        });
        hooked.push('ws2_32!recv');
      }
    }
  } catch (e) { info.push('recv ' + e); }

  // SSL_write / SSL_read（隧道内 TLS）
  ['libssl-1_1-x64.dll', 'libssl-3.dll'].forEach(function (mn) {
    const m = Process.findModuleByName(mn);
    if (!m) return;
    [['SSL_write', false], ['SSL_read', true]].forEach(function (pair) {
      const fn = pair[0], isRead = pair[1];
      const a = m.findExportByName(fn);
      if (!a) return;
      if (isRead) {
        Interceptor.attach(a, {
          onEnter(args) { this.buf = args[1]; },
          onLeave(ret) {
            try {
              const n = ret.toInt32();
              if (n > 0 && n < 1000000) {
                const u8 = new Uint8Array(this.buf.readByteArray(Math.min(n, 6000)));
                let pr = 0;
                for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
                send({kind:'ssl_read', len:n, ratio: pr/u8.length,
                      text: String.fromCharCode.apply(null, u8.slice(0, 3000))});
              }
            } catch (e) {}
          }
        });
      } else {
        Interceptor.attach(a, {
          onEnter(args) {
            try {
              const len = args[2].toInt32();
              if (len > 0 && len < 1000000) {
                const u8 = new Uint8Array(args[1].readByteArray(Math.min(len, 6000)));
                let pr = 0;
                for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
                send({kind:'ssl_write', len:len, ratio: pr/u8.length,
                      text: String.fromCharCode.apply(null, u8.slice(0, 3000))});
              }
            } catch (e) {}
          }
        });
      }
      hooked.push(mn + '!' + fn);
    });
  });

  send({kind:'ready', hooked:hooked, info:info});
})();
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_fg() -> None:
    import frida

    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == "marvissvr.exe"]
    if not procs:
        log("FATAL: no MarvisSvr.exe")
        return
    proc = procs[0]
    log(f"attach {proc.name} pid={proc.pid}")
    session = dev.attach(proc.pid)
    script = session.create_script(JS)
    n = {"c": 0}

    def on_msg(m, data):
        if m.get("type") != "send":
            return
        p = m["payload"]
        if p.get("kind") == "ready":
            log("HOOKED: " + str(p.get("hooked")))
            if p.get("info"):
                log("INFO: " + str(p.get("info")))
            return
        n["c"] += 1
        if n["c"] > 120:
            return
        txt = p.get("text", "")
        mark = " <<<" if any(x in txt for x in
                             ("HTTP/", "ual-access", "yybadaccess", "POST ", "GET ",
                              "chat/completions")) else ""
        log(f"{p.get('kind')} #{n['c']} len={p.get('len')} "
            f"ratio={p.get('ratio'):.0%}{mark}")
        if mark:
            for ln in txt.splitlines()[:40]:
                log("  | " + ln[:400])
        else:
            log("  " + txt[:120].replace("\n", " "))

    script.on("message", on_msg)
    script.load()
    log("监听中")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()

    if a.stop:
        if PIDFILE.exists():
            pid = PIDFILE.read_text().strip()
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            PIDFILE.unlink(missing_ok=True)
            print(f"stopped {pid}")
        return
    if a.start:
        LOG.write_text("", encoding="utf-8")
        out = open(ROOT / "tools" / "svrrecv.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
