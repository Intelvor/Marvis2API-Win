# 快速开始

三分钟上手。**只看这一页就够。**

---

## 1. 装依赖

```powershell
cd D:\Documents\Marvis2API-Win
python -m pip install -r requirements.txt
```

需要 Python 3.11+。

---

## 2. 启动

```powershell
.\start.ps1
```

会弹 UAC，点「是」。然后它会自动跑 6 步：

```
[1] 检查环境          Python / Marvis.exe / config.toml
[2] 准备客户端        自动带 CDP 端口启动（已在运行则复用）
[3] 启动注入通道      Frida 注入 MarvisAgent → 127.0.0.1:4512
[4] 启动面板          → http://127.0.0.1:4800
[5] 检查凭证          未就绪时用 CDP 自动初始化
[6] 就绪              打印汇总表并打开浏览器
```

看到三个端口都是 `OK` 就好了。

> 如果第 5 步提示「请手动发一条消息」，去 Marvis 客户端随便发一句即可。

---

## 3. 用起来

**监控面板**：http://127.0.0.1:4800

里面能看到余额、已用量、凭证状态、调用统计、最近活动。有问题会直接在顶部
用红/黄色横条提示该做什么。

**接客户端**：

```
Base URL : http://127.0.0.1:4800/v1
API Key  : 随便填
Model    : main-auto
```

Cherry Studio / ChatBox / NextChat / OpenAI SDK 都一样填。

**命令行试一下**：

```powershell
curl.exe -X POST http://127.0.0.1:4800/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"main-auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'
```

**查余额**：

```powershell
curl.exe http://127.0.0.1:4800/v3/marvis_get_wallet
```

---

## 4. 停止

```powershell
.\stop.ps1          # 停服务
.\stop.ps1 -Deep    # 停服务 + 清掉注入
```

---

## 遇到问题

| 现象 | 处理 |
|---|---|
| 提示「需要管理员权限」 | 正常，会自动提权重开一个窗口 |
| 第 3 步失败 | 看 `tools\auto.serve.err.log` |
| 第 5 步凭证一直捕获不到 | 客户端窗口是否最小化？去客户端手动发一句话 |
| 改了代码不生效 | 端口被旧进程占着。`.\start.ps1 -Restart` |
| 面板提示「底层通道未运行」 | `.\start.ps1` 没跑成功，重跑一次 |

更多细节：

- Bridge 方案的安装与排错 → [`WINDOWS-SETUP.md`](./WINDOWS-SETUP.md)
- 风控逆向全过程 → [`docs/RISKCONTROL-1.60.3400.md`](./docs/RISKCONTROL-1.60.3400.md)
- 各工具脚本用途 → [`docs/TOOLS.md`](./docs/TOOLS.md)
