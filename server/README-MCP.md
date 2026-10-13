# Veglia MCP Server (stdio)

Veglia 官方 Model Context Protocol (MCP) 服务层，基于 MCP Python SDK v2 (`mcp.server.mcpserver.MCPServer`) 构建。

为各类 AI Agent 提供原生工具支持：感知 Android 手机状态、跨 **任意数量** Windows 电脑的设备活动（Device Hub）、按需接收屏幕截图（原生 ImageContent 类型），以及融合后的多设备上下文快照。

## 架构

```text
Windows Sensors (每台电脑运行 device_sensor.py, .env 配置 VEGLIA_DEVICE_ID)
       ↓  POST /devices/report (Header X-Auth-Token)
Central Device Hub :8513  (veglia_server.py, 同时桥接 Android 手机)
       ↓  GET /devices · /devices/{device_id} · /context
veglia_tools.py (底层业务逻辑与数据模型)
       ↓  函数引用
veglia_mcp.py (stdio) / veglia_mcp_http.py (HTTP)
       ↓  JSON-RPC MCP
AI Agent (Antigravity / Codex / Claude Desktop / Cursor 等)
```

Device Hub 是 **N-device 泛化架构**：Registry 按任意唯一 `device_id` 动态注册（`self.devices[device_id] = record`），不限制设备数量，不依赖固定设备清单。新增一台 Windows 电脑只需在新机器上运行 `device_sensor.py` 并配置独立的 `VEGLIA_DEVICE_ID` / `VEGLIA_DEVICE_NAME`，无需改动服务端代码。

Context Fusion（`context_fusion.py`，规格版本 `context-v1`）遍历全部 Windows device records 与手机状态统一 normalize，输出 `active_devices` / `primary_device` / `simultaneous_usage` / `ambiguous` 等确定性结论。

## Agent 配置接入示例

在 Agent 的 MCP 客户端配置中添加如下条目（将 `<repo_root>` 替换为实际本地代码目录路径，例如 `D:\\veglia` 或 `D:\\mutiveglia`）：

```json
{
  "mcpServers": {
    "veglia": {
      "command": "<repo_root>\\.venv-mcp\\Scripts\\python.exe",
      "args": [
        "<repo_root>\\server\\veglia_mcp.py"
      ],
      "cwd": "<repo_root>\\server"
    }
  }
}
```

注意：
- 鉴权令牌 `VEGLIA_TOKEN` 由 `veglia_tools.py` 自动从本地 `server/.env` 文件加载，无需在 MCP 配置中明文暴露。
- 服务工作目录必须指定为 `server/` 所在目录，以保证正确加载 `.env` 及数据目录。
- HTTP 传输变体 `veglia_mcp_http.py` 复用同一套工具注册，供远程 Agent 接入。

## 注册工具说明（共 8 个）

### 手机侧

1. `get_veglia_status`:
   - 检查手机桥接服务状态与截图目录状态；
   - 只读诊断工具。
2. `get_phone_activity`:
   - 读取手机最近前台应用切换历史及当前前台 App；
   - 包含中文友好标签（如系统设置、微信、哔哩哔哩等）和相对时间；
   - 低侵入只读工具，建议优先调用。
3. `get_phone_screen`:
   - 触发无障碍截图并等待回传；
   - 真正返回 MCP `ImageContent`（JPEG 字节流），Agent 可直接进行多模态视觉理解。
4. `summon_phone_ai`:
   - 拉起手机端伴侣 AI 应用；
   - 具有明显前台打断性，仅在明确指令下调用。

### Windows 设备侧（Device Hub）

5. `get_devices_activity`:
   - 读取 Device Hub 中 **所有已注册 Windows 设备** 的活动概要；
   - 支持 **任意数量** 已注册设备，无需预先知道 device_id；
   - 返回各设备的 online 状态、前台进程、窗口标题、idle 秒数。
6. `get_device_activity(device_id)`:
   - 按 device_id 读取任意一台已注册 Windows 电脑的详细活动；
   - 不清楚有哪些设备时，先用 `get_devices_activity` 获取可用 ID；
   - 返回前台进程、窗口标题、idle 秒数与最近窗口切换历史。
7. `get_desktop_activity`:
   - **向后兼容 alias**：查询当前 MCP 宿主 `.env` 中 `VEGLIA_DEVICE_ID` 指向的本机设备；
   - 每台电脑上的本地 MCP 各自返回自己的状态（如台式机返回 `desktop-pc`，主力机返回 `main-pc`）；
   - 不固定指向某台特定机器。

### 融合上下文

8. `get_context`:
   - 返回确定性融合的多设备上下文快照（`context-v1`）；
   - 包含 `active_devices`、`primary_device`（含置信度）、`simultaneous_usage`、`ambiguous` 与逐设备 normalized 状态；
   - 关心"当前整体上下文"时，优先调用本工具而非逐台对比各设备。

## 远程接入：cloudflared 临时隧道（本机现场记录）

> 证据边界：以下为 2026-09-16 单机现场验证记录，不是本仓库的正式部署推荐；正式 / 长期部署仍应使用命名隧道 + 固定域名（或 nginx/caddy TLS）。

把本机 HTTP MCP（`veglia_mcp_http.py`，监听 `127.0.0.1:8514`，路径 `/mcp`）临时暴露给远程 Agent，最快路径是 cloudflared Quick Tunnel：

```bash
cloudflared tunnel --url http://127.0.0.1:8514
```

会打印一个 `https://<随机>.trycloudflare.com` 地址，远程 Agent 以 `https://<随机>.trycloudflare.com/mcp` 接入。本机现场约定：**隧道只挂 8514，不挂 8513**——8513 是 Device Hub / 手机桥接，不要一起暴露。

Quick Tunnel 的已知限制：

- 地址每次重启都会变，不能写成固定配置；
- Cloudflare 官方明确 Quick Tunnel 不支持 SSE，对依赖 SSE 的 MCP 客户端可能不可用；
- 仅适合临时打通验证，长期使用请改命名隧道。

### 本机坑：Clash Verge TUN + fake-ip 导致隧道连不上

本机现场验证：即使 Clash 里已有 `cloudflared` 的 DIRECT 规则，TUN + fake-ip 模式仍会把 `argotunnel.com`（cloudflared 边缘连接用的 7844 端口域）解析成 `198.18.x.x` 假 IP，导致隧道注册失败、连不上。

解决：在 Clash Verge 配置中

1. `tun.exclude-process` 加入 `cloudflared.exe`（进程级绕过 TUN）；
2. `dns.fake-ip-filter` 加入 `+.argotunnel.com`、`+.trycloudflare.com`（域名不走 fake-ip）；

然后重启 mihomo 内核生效。

注意这是这台机器的现场验证结论，不要直接外推为所有 Clash / mihomo 版本的通用行为；其他机器遇到隧道连不上时，先用同样的思路核对 DNS 解析是否被 fake-ip 接管。
