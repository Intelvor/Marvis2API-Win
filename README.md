# Marvis2API-Win

把腾讯 **Marvis** 桌面助手的私有接口，封装成**标准 OpenAI 兼容网关**。

本机装好并登录了 Marvis 客户端，就能用任意 OpenAI 客户端（Cherry Studio、ChatBox、
NextChat、LangChain、OpenAI SDK……）调用它背后的模型。

> 模型实测为 **Qwen3**，服务端动态路由（标识 `main-auto`，不可指定具体模型）。

---

## 快速开始

**双击 `start.ps1`**（或在终端 `.\start.ps1`），等它跑完 6 步即可。全程只需点一次 UAC「是」。

```powershell
.\start.ps1              # 正常启动
.\start.ps1 -Restart     # 强制重启客户端与服务
.\start.ps1 -NoUI        # 不自动打开浏览器
.\stop.ps1               # 停止服务
.\stop.ps1 -Deep         # 停止 + 清掉 Frida 注入
```

启动完成后：

| 用途 | 地址 |
|---|---|
| **监控面板** | http://127.0.0.1:4800 |
| **OpenAI 接口** | http://127.0.0.1:4800/v1/chat/completions |
| 余额查询 | http://127.0.0.1:4800/v3/marvis_get_wallet |

客户端配置：

```
Base URL : http://127.0.0.1:4800/v1
API Key  : 随便填
Model    : main-auto
```

---

## 三套方案，按需选择

仓库里其实有三套并存的实现，解决不同阶段的问题。**日常用 Hub 就够了。**

### 方案 A：Hub（推荐）—— `start.ps1` → 4800

```
你的客户端 → Hub(4800) → 注入通道(4512) → Marvis 客户端进程 → 上游
```

- 标准 OpenAI 接口，可重复调用、可并发
- 凭证自动续期（后台 watchdog + CDP 自动发消息）
- 带监控面板：余额、用量、凭证状态、调用统计
- 需要客户端带 CDP 启动（`start.ps1` 会自动处理）

### 方案 B：Bridge（旧）—— `deploy.ps1` → 4605

```
你的客户端 → Bridge(4605) → 上游
```

最早期的实现，直接把请求转发到上游。**从 Marvis 1.60.3400 起上游加了风控，
新装环境用这套会被拒绝**（`4100404`）。保留它是为了旧版本客户端仍可继续使用。

细节见 [`WINDOWS-SETUP.md`](./WINDOWS-SETUP.md)。

### 方案 C：new-api 分发层（可选）

配合 new-api 做多用户分发、额度管理、渠道编排。属于上层增强，与本仓库主体独立。

---

## 工作原理（一句话版）

Marvis 的网关要求请求头里的 `Marvis-Response-ID` 必须是一个**当前有效的会话凭证**，
自己生成的一律 403。所以不能凭空构造请求，只能：

1. 从客户端真实请求里捕获这份凭证（Frida 注入 `MarvisAgent` 的 `httpx`）
2. 用它发我们自己的请求
3. 凭证约 3 次调用失效 → 自动用 CDP 让客户端再发一条消息刷新 → 重试原请求

完整的逆向过程、15 项失败实验、可复用的 API 清单，见
[`docs/RISKCONTROL-1.60.3400.md`](./docs/RISKCONTROL-1.60.3400.md)。

---

## 可用的上游接口

| 接口 | 用途 | 状态 |
|---|---|---|
| `/v1/chat/completions` | 对话 | 可用 |
| `/v3/marvis_get_wallet` | 余额（总额/已用/剩余） | 可用 |
| `/v3/marvis_agentcore_tab_query_v1` | Agent 核心标签 | 可用 |
| 其余 `/v3/*` | 公告、会话统计、模型网关、嵌入 | 不可用（400/403） |

Hub 支持任意 `/v3/*` 透传，直接换成对应路径即可（`/v3/xxx?biz=pc_yyb&auth=0` 可调身份）。

---

## 环境要求

- Windows 10/11
- Python 3.11+
- 本机安装并登录 **Marvis 客户端**
- 管理员权限（Frida 注入需要）

```powershell
python -m pip install -r requirements.txt
```

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`QUICKSTART.md`](./QUICKSTART.md) | 三分钟上手，只看这个就够 |
| [`WINDOWS-SETUP.md`](./WINDOWS-SETUP.md) | Bridge 方案的分步安装与排错对照表 |
| [`docs/RISKCONTROL-1.60.3400.md`](./docs/RISKCONTROL-1.60.3400.md) | **核心文档**：风控逆向全过程、真实请求结构、可用接口、踩坑记录 |
| [`docs/TOOLS.md`](./docs/TOOLS.md) | `tools/` 下每个脚本的用途与用法 |

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 模型不可选 | 只认 `main-auto`，显式指定 `hy3` / `deepseek-*` 一律 403 |
| 凭证会过期 | 约 3 次调用失效，自动续期（需 CDP 可用） |
| 刷新留痕 | 每次自动续期会在客户端留一条 `.` 消息 |
| 依赖客户端 | 客户端必须运行；窗口被最小化时自动续期可能失败 |
| 单线程 | Hub 内部串行转发，高并发会排队 |

---

## 目录结构

```
.
├── start.ps1                  一键启动（注入 + 服务 + 面板 + 凭证初始化）
├── stop.ps1                   一键停止
├── start-service.bat          start.ps1 的双击包装
├── stop-service.bat           stop.ps1 的双击包装
├── bridge.py                  方案 B：旧版直转发网关（4605）
├── deploy.ps1 / undeploy.ps1  方案 B 的部署脚本
├── uskey_provider.py          设备指纹提供器
├── config.toml                配置（含凭证，不入库）
├── docs/
│   ├── RISKCONTROL-1.60.3400.md   风控逆向核心文档
│   └── TOOLS.md                   工具脚本索引
└── tools/                     全部逆向工具与服务实现
```

---

## 免责

仅供个人学习与研究。请遵守腾讯的服务条款，不要用于商业分发或大规模滥用。
