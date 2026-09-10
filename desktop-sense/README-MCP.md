# Desktop Sense MCP Server (Windows 11)

基于官方 MCP Python SDK v2 (`MCPServer`) 开发的 Windows 11 桌面状态感知与按需视觉捕获 stdio MCP 服务。

## 设计特性与隐私原则

1. **协议层级**：完全基于标准输入输出 (`stdio`)，无额外开放的网络端口，无需环境变量秘钥；
2. **隐私脱敏机制**：
   - `get_active_window` 与 `get_desktop_status` 默认严格脱敏（`mode: safe`），仅暴露进程名、友好标签（如 `Obsidian`、`VS Code`）与 PID；
   - 绝不默认暴露完整窗口标题（防泄露私密聊天、网页或笔记内容）与可执行程序全路径（防泄露系统用户名）；
3. **视觉感知按需调用**：
   - `get_desktop_screen` 默认只截取当前前台活动窗口（`target="active_window"`），避免将多屏全屏内容一股脑传给模型；
   - 原生返回 MCP `ImageContent` 对象，AI 客户端无需进行二次文件读取即可直接多模态理解屏幕。

## 启动方式

```powershell
& "D:\veglia\.venv-mcp\Scripts\python.exe" D:\desktop-sense\desktop_mcp.py
```

## 通用 MCP 客户端配置模板

```json
{
  "mcpServers": {
    "desktop-sense": {
      "command": "D:\\veglia\\.venv-mcp\\Scripts\\python.exe",
      "args": [
        "D:\\desktop-sense\\desktop_mcp.py"
      ],
      "cwd": "D:\\desktop-sense"
    }
  }
}
```

## 注册工具一览

- `get_active_window`：获取当前前台聚焦应用程序的安全脱敏摘要；
- `get_idle_status`：获取键鼠无操作空闲秒数及 UX 推断状态（active / thinking / away）；
- `get_desktop_status`：首选聚合工具，一次性获取当前前台应用与空闲状态；
- `get_desktop_screen`：按需截屏，支持 `active_window`（默认）、`primary`（主显示器）、`virtual`（全局虚拟屏幕），原生返回 JPEG 多模态图像。
