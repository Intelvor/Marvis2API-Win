#!/usr/bin/env python3
"""cdp_input.py — 经 CDP 找到客户端的输入框与发送按钮

用途：为 inject_body 的搭载提供自动化前端。
先只做侦察，不改任何东西。

用法：
  python tools/cdp_input.py --list          列出可编辑元素
  python tools/cdp_input.py --send "文本"   填入并发送
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import websocket

CDP = "http://127.0.0.1:9222"


def page_ws() -> str:
    import urllib.request

    raw = urllib.request.urlopen(f"{CDP}/json/list", timeout=8).read()
    for t in json.loads(raw):
        if t.get("type") == "page" and "marvis" in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("找不到 page target")


class Cdp:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(page_ws(), timeout=30)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method,
                                 "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                return msg

    def eval(self, expr: str, await_promise: bool = True):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"error": res["exceptionDetails"].get("text")}
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


PROBE = r"""
(() => {
  const out = [];
  const sel = 'textarea, input[type=text], [contenteditable=""], [contenteditable=true], [role=textbox]';
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    out.push({
      i: i,
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 90),
      id: el.id || '',
      ph: el.getAttribute('placeholder') || '',
      role: el.getAttribute('role') || '',
      editable: el.isContentEditable,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      visible: r.width > 0 && r.height > 0,
    });
  });
  // 顺便找按钮
  const btns = [];
  document.querySelectorAll('button, [role=button]').forEach((b, i) => {
    const t = (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 20);
    const r = b.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      btns.push({i: i, text: t, cls: (b.className || '').toString().slice(0, 60)});
    }
  });
  return JSON.stringify({
    url: location.href,
    title: document.title,
    editables: out,
    buttons: btns.slice(0, 25),
  }, null, 1);
})()
"""


def do_list() -> None:
    c = Cdp()
    try:
        raw = c.eval(PROBE)
        if isinstance(raw, dict) and raw.get("error"):
            print("JS 错误:", raw["error"])
            return
        print(raw)
    finally:
        c.close()


def do_send(text: str) -> None:
    """填入 ProseMirror 编辑器并点击发送按钮。

    输入框是 tiptap/ProseMirror（div[role=textbox][contenteditable]），
    不是 textarea。发送按钮的类名含 semi-aiChatInput-footer-action-send。
    """
    c = Cdp()
    try:
        fill = r"""
        (() => {
          const t = %s;
          const el = document.querySelector('.ProseMirror')
                   || document.querySelector('[role=textbox]')
                   || document.querySelector('[contenteditable="true"]');
          if (!el) return 'no-input';
          el.focus();

          // ProseMirror 不吃 textContent 赋值，要模拟真实输入事件链。
          // 优先用 execCommand（浏览器会把输入正确地转成 PM 事务）
          const sel = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(el);
          sel.removeAllRanges();
          sel.addRange(range);
          let ok = false;
          try {
            ok = document.execCommand('insertText', false, t);
          } catch (e) { ok = false; }

          if (!ok) {
            // 退路：手动构造 beforeinput / input
            el.dispatchEvent(new InputEvent('beforeinput', {
              bubbles: true, cancelable: true, inputType: 'insertText', data: t,
            }));
            el.textContent = t;
            el.dispatchEvent(new InputEvent('input', {
              bubbles: true, inputType: 'insertText', data: t,
            }));
          }
          return (ok ? 'execCommand-ok' : 'fallback') + ' textLen=' + (el.innerText || '').length;
        })()
        """ % json.dumps(text)
        print("填入:", c.eval(fill))
        time.sleep(0.5)

        click = r"""
        (() => {
          const b = document.querySelector('[class*="footer-action-send"]');
          if (!b) return 'no-send-button';
          const r = b.getBoundingClientRect();
          const dis = b.getAttribute('disabled') !== null
                      || (b.className || '').toString().includes('disabled');
          b.click();
          return 'clicked disabled=' + dis + ' rect=' + Math.round(r.x) + ',' + Math.round(r.y);
        })()
        """
        print("发送:", c.eval(click))
    finally:
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--send")
    a = ap.parse_args()
    if a.list:
        do_list()
    elif a.send:
        do_send(a.send)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
