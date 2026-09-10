# Veglia AI Tools

Veglia 面向 AI / LLM / Agent 调用的轻量级结构化工具接口层。

底层直接对接本地运行的 Veglia 服务端，所有输出均为纯 JSON 结构，无任何多余的终端排版杂音，适合各类 Agent 进程直接解析。

## 配置依赖

脚本自动读取同目录下的 `.env`：
- `VEGLIA_TOKEN`：鉴权令牌
- `VEGLIA_URL`：本地服务端接口地址（默认 `http://127.0.0.1:8513`）
- `VEGLIA_DATA_DIR`：数据存储目录（默认 `./data`）

## CLI 工具列表

### 1. get_veglia_status (服务与健康检查)

命令：
```bash
python veglia_tools.py status
```

返回示例：
```json
{
  "ok": true,
  "tool": "get_veglia_status",
  "server_reachable": true,
  "service": "veglia",
  "version": "0.6.0",
  "base_url": "http://127.0.0.1:8513",
  "screenshots_dir": "D:\\veglia\\server\\data\\screenshots",
  "latest_screenshot": "D:\\veglia\\server\\data\\screenshots\\peek_1788281177.jpg",
  "latest_screenshot_mtime": 1788281177
}
```

### 2. get_phone_activity (前台应用状态感知)

命令：
```bash
python veglia_tools.py activity
```

返回示例：
```json
{
  "ok": true,
  "tool": "get_phone_activity",
  "most_recent": {
    "app": "dev.veglia.companion",
    "label": "Veglia",
    "ts": 1788280998259,
    "ago": "2m ago"
  },
  "events": [
    {
      "app": "dev.veglia.companion",
      "label": "Veglia",
      "ts": 1788280998259,
      "ago": "2m ago"
    },
    {
      "app": "com.android.settings",
      "label": "系统设置",
      "ts": 1788280996355,
      "ago": "2m ago"
    }
  ]
}
```

### 3. get_phone_screen (远程截屏并返回绝对路径)

触发手机端无障碍服务在后台静默截图，并等待服务端接收到完整图片后交付。

命令：
```bash
python veglia_tools.py screen [--timeout 10.0]
```

返回示例：
```json
{
  "ok": true,
  "tool": "get_phone_screen",
  "image_path": "D:\\veglia\\server\\data\\screenshots\\peek_1788281177.jpg",
  "created_at": 1788281177,
  "waited_seconds": 3.51
}
```

### 4. summon_phone_ai (拉起前台 AI 应用)

命令：
```bash
python veglia_tools.py summon
```

返回示例：
```json
{
  "ok": true,
  "tool": "summon_phone_ai",
  "message": "summon sent"
}
```

## 后续接入 MCP 说明

若需接入 Model Context Protocol (MCP)，只需使用 FastMCP 或标准 JSON-RPC 包装器将上述四个函数暴露为 Tool，底层直接调用 `tool_status`、`tool_activity`、`tool_screen`、`tool_summon` 即可，逻辑与数据模型完全复用。
