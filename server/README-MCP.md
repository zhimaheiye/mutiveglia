# Veglia MCP Server (stdio)

Veglia 官方 Model Context Protocol (MCP) 本地服务层，基于 MCP Python SDK v2 (`mcp.server.mcpserver.MCPServer`) 构建。

通过标准输入/输出 (stdio) 为各类 AI Agent 提供原生工具支持，使 Agent 能够无缝感知宿主手机运行状态，并在需要时直接接收屏幕截图（原生 ImageContent 类型）。

## 架构

```text
Android Companion (Veglia App)
       ↓ (局域网 HTTP / Header X-Auth-Token)
veglia_server.py (Windows 本地服务 :8513)
       ↓ (本地 HTTP 请求)
veglia_tools.py (底层业务逻辑与数据模型)
       ↓ (函数引用)
veglia_mcp.py (MCP SDK v2 stdio 服务端)
       ↓ (stdio JSON-RPC)
AI Agent (Antigravity / Codex / Claude Desktop / Cursor 等)
```

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

## 注册工具说明

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
5. `get_desktop_activity`:
   - 读取当前 Windows 桌面状态：前台应用进程名、窗口标题、PID、键鼠空闲时间（`idle_seconds`）以及最近的窗口切换历史；
   - 本地轻量只读工具，低延迟感知用户在台式机上的当前工作上下文。
