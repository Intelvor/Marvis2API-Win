#!/usr/bin/env python3
"""hook_chain.py — 追踪 MarvisAgent 的请求完整去向

目的：确认 MarvisAgent 发出的明文 HTTP 最终写进了哪个 socket、对端是谁。

hook 点：
  - EVP_CipherUpdate  拿 TLS 明文（已知唯一有效入口）
  - ws2_32!send       拿实际写出的字节 + 关联 socket 句柄
  - ws2_32!connect    拿连接目标地址
  - WS2_32!WSASend    同上（httpx 可能走这个）

输出：tools/chain.log，每条记录带 socket 句柄，可交叉对照。

用法（需管理员）：python tools/hook_chain.py --start / --run / --stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "chain.log"
PIDFILE = ROOT / "tools" / "chain.pid"

JS = r"""
(function () {
  const hooked = [], errs = [];
  const ws = Process.findModuleByName('ws2_32.dll');

  function sockaddrStr(sa) {
    try {
      if (sa.isNull()) return '?';
      const fam = sa.readU16();
      if (fam === 2) {  // AF_INET
        const port = (sa.add(2).readU8() << 8) | sa.add(3).readU8();
        const ip = [4,5,6,7].map(function (i) {
          return sa.add(i).readU8();
        }).join('.');
        return ip + ':' + port;
      }
      if (fam === 23) { // AF_INET6
        const port = (sa.add(2).readU8() << 8) | sa.add(3).readU8();
        const b = new Uint8Array(sa.add(8).readByteArray(16));
        let parts = [];
        for (let i = 0; i < 16; i += 2) parts.push(((b[i]<<8)|b[i+1]).toString(16));
        return '[' + parts.join(':') + ']:' + port;
      }
      return 'family=' + fam;
    } catch (e) { return 'err'; }
  }

  // connect：谁连了哪
  try {
    const c = ws.findExportByName('connect');
    if (c) {
      Interceptor.attach(c, {
        onEnter(args) {
          send({kind:'connect', sock: args[0].toInt32(), addr: sockaddrStr(args[1])});
        }
      });
      hooked.push('ws2_32!connect');
    }
  } catch (e) { errs.push('connect ' + e); }

  // send：实际写出的字节
  try {
    const s = ws.findExportByName('send');
    if (s) {
      Interceptor.attach(s, {
        onEnter(args) {
          try {
            const len = args[2].toInt32();
            if (len > 0 && len < 200000) {
              const u8 = new Uint8Array(args[1].readByteArray(Math.min(len, 8000)));
              let pr = 0;
              for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
              const t = String.fromCharCode.apply(null, u8.slice(0, 4000));
              send({kind:'send', sock: args[0].toInt32(), len: len,
                    ratio: pr/u8.length, text: t});
            }
          } catch (e) {}
        }
      });
      hooked.push('ws2_32!send');
    }
  } catch (e) { errs.push('send ' + e); }

  // WSASend
  try {
    const a = ws.findExportByName('WSASend');
    if (a) {
      Interceptor.attach(a, {
        onEnter(args) {
          try {
            const n = args[2].toInt32();
            let total = 0, txt = '';
            for (let i = 0; i < n && i < 8; i++) {
              const buf = args[1].add(i * 16);
              const p = buf.readPointer();
              const l = buf.add(8).readUInt32();
              total += l;
              if (total < 200000 && !p.isNull() && l > 0) {
                const u8 = new Uint8Array(p.readByteArray(Math.min(l, 4000)));
                for (let k = 0; k < u8.length; k++) txt += String.fromCharCode(u8[k]);
              }
            }
            if (total > 0) {
              send({kind:'wsasend', sock: args[0].toInt32(), len: total,
                    text: txt.slice(0, 4000)});
            }
          } catch (e) {}
        }
      });
      hooked.push('ws2_32!WSASend');
    }
  } catch (e) { errs.push('wsasend ' + e); }

  // EVP_CipherUpdate：TLS 明文 —— 注意明文在输入参数 args[3]，
  // 加密方向 (ctx=enc) 是 in=明文 / out=密文，必须在 onEnter 读。
  ['libcrypto-3.dll', 'libcrypto-1_1-x64.dll'].forEach(function (mn) {
    const m = Process.findModuleByName(mn);
    if (!m) return;
    ['EVP_CipherUpdate', 'EVP_EncryptUpdate'].forEach(function (fname) {
      const a = m.findExportByName(fname);
      if (!a) return;
      Interceptor.attach(a, {
        onEnter(args) {
          try {
            const inl = args[4].toInt32();
            if (inl <= 0 || inl > 1000000) return;
            const n = Math.min(inl, 8000);
            const u8 = new Uint8Array(args[3].readByteArray(n));
            let pr = 0;
            for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
            const ratio = pr / u8.length;
            if (ratio < 0.30) return;
            let t = '';
            for (let i = 0; i < Math.min(u8.length, 4000); i++) t += String.fromCharCode(u8[i]);
            send({kind:'evp', fn: fname, len: inl, ratio: ratio, text: t});
          } catch (e) {}
        }
      });
      hooked.push(mn + '!' + fname);
    });
  });

  send({kind:'ready', hooked: hooked, errs: errs});
})();
"""


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_fg():
    import frida
    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == "marvisagent.exe"]
    if not procs:
        log("FATAL: no MarvisAgent.exe")
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
        k = p.get("kind")
        if k == "ready":
            log("HOOKED: " + str(p.get("hooked")))
            if p.get("errs"):
                log("ERRS: " + str(p.get("errs")))
            return
        if k == "connect":
            log(f"connect sock={p.get('sock')} -> {p.get('addr')}")
            return
        n["c"] += 1
        if n["c"] > 300:
            return
        txt = p.get("text", "")
        mark = ""
        if any(x in txt for x in ("POST ", "GET ", "HTTP/", "ual-access", "yybadaccess")):
            mark = " <<<<<"
        ratio = p.get("ratio")
        rs = f" ratio={ratio:.0%}" if isinstance(ratio, (int, float)) else ""
        log(f"{k} #{n['c']} sock={p.get('sock','-')} len={p.get('len')}{rs}{mark}")
        if mark:
            for ln in txt.splitlines()[:30]:
                log("  | " + ln[:350])

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
        out = open(ROOT / "tools" / "chain.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
