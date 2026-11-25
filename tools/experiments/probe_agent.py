#!/usr/bin/env python3
"""probe_agent.py — 枚举 MarvisAgent.exe 的模块与 TLS 栈

抓包显示 chat 请求的 UA 是 "AsyncOpenAI/Python 2.32.0"，说明真正发
LLM 请求的是 MarvisAgent.exe（Python 进程），不是 MarvisHost。
本脚本确认它加载了哪个 TLS 库，为 hook 选点。

用法（需管理员）：
  python tools/probe_agent.py
"""
from __future__ import annotations

import time
from pathlib import Path

import frida

OUT = Path(__file__).resolve().parent / "agent-modules.txt"


def main():
    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == "marvisagent.exe"]
    if not procs:
        print("[!] 未找到 MarvisAgent.exe")
        return
    pid = procs[0].pid
    print(f"[probe] MarvisAgent pid={pid}")

    session = dev.attach(pid)
    js = r"""
    (function () {
      const mods = Process.enumerateModules().map(m => ({n: m.name, p: m.path}));
      const want = /ssl|crypto|tls|schannel|winhttp|wininet|python|_ssl|socket/i;
      const hit = mods.filter(m => want.test(m.n));
      // python 版本信息
      let py = null;
      try {
        const p = Process.findModuleByName('python311.dll') || Process.findModuleByName('python3.dll');
        if (p) py = p.path;
      } catch (e) {}
      send({total: mods.length, hit: hit, py: py});
    })();
    """
    script = session.create_script(js)
    state = {"done": False}

    def on_msg(m, data):
        if m.get("type") != "send":
            print("MSG", str(m)[:150])
            return
        p = m["payload"]
        lines = [f"总模块数: {p['total']}", f"python: {p['py']}", "", "=== 相关模块 ==="]
        for x in p["hit"]:
            lines.append(f"  {x['n']:30} {x['p']}")
        txt = "\n".join(lines)
        print(txt)
        OUT.write_text(txt, encoding="utf-8")
        state["done"] = True

    script.on("message", on_msg)
    script.load()
    for _ in range(20):
        if state["done"]:
            break
        time.sleep(0.5)
    try:
        script.unload()
    except Exception:
        pass
    session.detach()


if __name__ == "__main__":
    main()
