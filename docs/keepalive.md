# Veglia 双服务保活与故障恢复（台式机运维记录）

适用范围：`desktop-pc`（台式机）中心节点，Veglia Core `:8513` 与 Veglia HTTP MCP `:8514`。
手机 Sensor、其他 Windows Sensor 设备不在本文的保活范围内。

本文是这台机器上实际生效的保活机制说明。脚本源在仓库 `ops/`，部署树为 `D:\veglia`（同一仓库的
clone，`core.autocrlf=true`，因此工作树是 CRLF 而仓库内容是 LF——已逐文件比对，内容一致）。

---

## 1. 事故时间线

### 已证实

| 时间 | 证据 |
|---|---|
| 2026-10-12 18:41:09 | `D:\veglia\server\logs\device-sensor.log` 开始出现持续 `WinError 10061`（连接被拒绝） |
| 2026-10-12 → 10-14 | Core 与 MCP 双双不在监听，无任何自动恢复动作 |
| 2026-10-14 10:06:48 | 同一日志记录 `Hub connection restored`，人工拉起服务 |

故障窗口约 **39 小时 26 分钟**（≈39.5h）。`device_sensor.py` 每 2 秒向 `http://127.0.0.1:8513/devices/report`
上报，且只在状态变化时写日志，所以它事实上是这台机器上最可靠的历史存活探针——上面的两个时间点都来自它。

另一条重要基线：日志里的 `timed out` 每天出现 14~25 次，是常态噪声，**不是**故障前兆。因此保活机制
必须以 `/health` 的连续结果为门限，不能以延迟为门限。

### 未证实

- **10-12 18:41 两个进程首次退出的直接原因**。没有留下能说明退出原因的日志（原因见下）。
- 是否曾发生过多次退出—再启动的循环。TaskScheduler 的 Operational 日志当时是关闭的，事后无法回溯。

### 取证损失（必须记录的操作失误）

验证 `ops/veglia_logrotate.ps1` 的 copytruncate 行为时，误将 `D:\veglia\server\logs\veglia-mcp.log`
截断并删除，**10-12 当天的 MCP 日志因此丢失**。现有缓解：

- 从历史会话记录恢复出 MCP 日志最后 30 行，存为
  `D:\veglia\logs\forensics\keep-2026-10-12_mcp-last-log-lines.txt`，文件内已标注这是
  historical conversation evidence，不是完整日志。
- `veglia-server.log`（155,362,071 字节，sha256 前缀 `10EE9F2DD4D29792…`）在处置前完整归档到同一目录。
- 教训已写进脚本注释：**今后只在 `-DryRun` 下演练轮转**，不动生产日志。

---

## 2. 为什么 39.5 小时没有人（和程序）恢复它

四个独立缺口叠加，缺一个都不会拖到 39 小时：

1. `VegliaServer` 与 `VegliaMCP` 两个计划任务**只有登录触发器**。进程退出后没有任何调度器会再拉起，
   只有下一次交互登录才会重启。
2. 当时唯一在跑的小时巡检 `VegliaTunnelHourlyCheck` 按它自己的设计**拒绝重启 Core**
   （只记录 `CORE_8513_DOWN`），职责边界本身没错，但结果没人接手。
3. 告警事实上到不了人：巡检只写 `tunnel-watch.log`，Core 挂掉不在它的告警职责内。
4. `Microsoft-Windows-TaskScheduler/Operational` 渠道处于关闭状态，没有任务触发/失败的审计记录。

对应四项修复：BootTrigger、`VegliaWatchdog` 接管 8513/8514、飞书升级式告警、`wevtutil` 启用审计渠道。

---

## 3. 现在的架构：两层保活 + 单一所有权

```text
第一层  Windows 原生调度           开机 / 登录触发 VegliaServer、VegliaMCP
                                   IgnoreNew，StopOnIdleEnd=false，RestartOnFailure 3×PT1M
第二层  VegliaWatchdog（5 分钟）    探测 /health → 连续 2 次失败 → 恢复 → 复测 → 记录 → 必要时告警
```

所有权划分（避免两个调度器在同一分钟抢同一个任务）：

| 组件 | 负责 | 脚本 |
|---|---|---|
| `VegliaWatchdog` | Core 8513、MCP 8514 | `ops/veglia_watchdog.ps1` |
| `VegliaTunnelHourlyCheck` | cloudflared 服务、公网 `/health` | `ops/check_veglia_tunnel.ps1` |

小时巡检遇到 8513/8514 故障时**不再动作**，日志写 `action=NONE_WATCHDOG_OWNED`、结果写
`CORE_8513_DOWN` / `MCP_8514_DOWN`，把恢复交给 watchdog。

刻意的设计取舍：**不给两个服务任务加周期触发器**。周期重启会绕过维护模式，把"故意停止"当成故障拉起。

---

## 4. 计划任务实际状态（2026-10-14 从任务定义 dump，非推断）

| 任务 | 触发器 | 账号 / 登录类型 | RunLevel | 执行 | 关键设置 |
|---|---|---|---|---|---|
| `VegliaServer` | Logon + **Boot** | Administrator / S4U | Limited | `python.exe D:\veglia\server\veglia_server.py`，cwd `D:\veglia\server` | `PT0S` 不限时、`IgnoreNew`、`StopOnIdleEnd=false`、`RestartOnFailure 3×PT1M` |
| `VegliaMCP` | Logon + **Boot** | Administrator / S4U | Limited | `python.exe D:\veglia\server\veglia_mcp_http.py`，同上 | 同上 |
| `VegliaWatchdog`（新增） | Time `PT5M` + **Boot** | Administrator / S4U | **HighestAvailable** | `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "D:\veglia\ops\veglia_watchdog.ps1"`，cwd `D:\veglia` | `ExecutionTimeLimit=PT10M`、`IgnoreNew`、`RestartOnFailure 1×PT5M` |
| `VegliaDesktopSensor` | Logon（**未改动**） | Administrator / **Interactive** | Highest | `pythonw.exe D:\veglia\server\device_sensor.py` | `StopOnIdleEnd=true`（保持原样，不在本次范围） |
| `VegliaTunnelHourlyCheck` | Time `PT1H` | SYSTEM / ServiceAccount | Highest | `powershell.exe -File "D:\veglia\ops\check_veglia_tunnel.ps1"` | `PT5M`、`IgnoreNew` |

`VegliaWatchdog` 用 HighestAvailable 是因为它需要全局命名互斥（`Global\VEGLIA_WATCHDOG`）和
`wevtutil` 读取任务计划程序日志。

Sensor 的 `Interactive` 登录类型这条已知边界依旧存在：开机不登录时 Sensor 不会自启，而 Server/MCP 会。
本次没有改它（改动会影响正在稳定运行的传感器），只是记录。

---

## 5. 健康判定与恢复策略

健康**只**定义为：`GET /health` 返回 HTTP 200 **且**响应体匹配 `"ok"\s*:\s*true`。
进程存在、端口在听、任务显示 `Running` 都不算健康。

探针使用 `curl.exe --noproxy '*'`，绕开 Clash TUN / fake-ip，防止本地代理污染诊断结果
（这正是历史上 `WinError 10061` 类误判的来源）。

`ops/veglia_watchdog.ps1` 默认参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `-FailStreakThreshold` | 2 | 连续 2 轮（≈10 分钟）失败才动作 |
| `-MaxActionsPerHour` | 3 | 每服务每小时最多 3 次恢复动作 |
| `-WarmupGraceSec` | 120 | 刚启动的任务允许 120 秒预热，判为 `WARMING_UP` 而非卡死 |
| `-RecoveryWaitSec` | 60 | 发起恢复后等待健康的上限 |
| `-AlertCooldownHours` | 6 | 同一告警键的冷却 |
| `-ProbeTimeoutSec` | 8 | 单次探测超时 |
| `-RotateIntervalMin` | 60 | 触发日志轮转的间隔 |

恢复顺序：**Core 先于 MCP**。MCP 的 `DependsOn = core`，Core 当前不健康时 MCP 记 `SKIP_DEPENDENCY`；
Core 恢复成功后立刻复测 MCP，使两者可以在同一轮内连续恢复。任务显示 `Running` 但 HTTP 不通时，
先用任务启动时间判断是预热还是卡死；卡死走 `STUCK` 告警而不是无限重启。

每次恢复后必须复测健康才记 `RECOVERED`；限频计数、连续失败数、告警键都持久化在
`D:\veglia\logs\watchdog-state.json`（临时文件 + `Move-Item` 原子替换），所以每次都是新进程的
调度器不会把限频清零。全局互斥保证并发轮次不会重叠恢复同一服务。

逃生口：`-Force` 忽略维护标记、连续失败门限和限频；`-ProbeOnly` 只探测不动作；`-NoAlert` 动作但不发消息。

---

## 6. 维护模式

`ops/veglia_maintenance.ps1`

```text
-On -Reason "upgrade sensor" -Hours 8   进入维护（写 D:\veglia\logs\maintenance.flag）
-Off                                    清除标记，并按 core→mcp 顺序拉起、轮询健康
-Off -NoStart                           只清标记，不启动
-Status                                 打印标记 + 任务状态 + HTTP 码
```

`server/stop_server.bat` 在停止服务前自动 `-On -Reason "stop_server.bat" -Hours 8`；
`server/start_server.bat` 在启动前自动 `-Off -NoStart`。维护期内 watchdog 记录 `SKIP_MAINTENANCE`，
不动作、不告警。标记带 `expires_unix`（**毫秒**），过期即失效，所以被遗弃的标记不会永久屏蔽恢复。

小时巡检脚本读同一个标记，避免巡检在维护期重启 cloudflared。

---

## 7. 告警策略

通道：复用台式机已有的 `lark-cli`（`...@larksuite\cli\bin\lark-cli.exe im +messages-send --as bot`），
没有新建通知系统，保活链路里没有任何 AI 调用——全部是确定性 PowerShell。

**不发**：正常巡检轮；一次偶发故障且自动恢复成功（只写日志）。

**发**（按告警键去重，冷却 6 小时）：

| 键 | 触发条件 |
|---|---|
| `FLAP:<svc>` | 1 小时内恢复次数达到上限（反复退出） |
| `LIMIT:<svc>` | 超出每小时恢复预算仍失败，`SKIP_RATE_LIMIT` |
| `STUCK:<svc>` | 任务 Running 但超过预热宽限仍不健康 |
| `PERSISTENT:<svc>` | 尝试后仍不可用 |
| `DISABLED:<svc>` | 任务被禁用——watchdog **拒绝静默启用**，只告警 |
| `TASKMISSING:` | 任务不存在 |
| `RECOVERED:<svc>` | 恢复成功后的单条简报，随后清空该服务的告警键 |
| `SELF:*` | watchdog 自身异常（无法读写状态、无法探测等） |

消息正文含：服务标签、检测时间、尝试次数与预算、最终状态与健康码、监听地址、日志路径。
最近一次发出的告警留在 `D:\veglia\logs\watchdog.alert` 便于核对，不含任何凭据。

---

## 8. 日志、轮转与取证

| 路径 | 内容 |
|---|---|
| `D:\veglia\logs\watchdog.log` | 结构化巡检日志：`时间 \| TAG \| 明细`，TAG 为 `ROUND`/`PROBE`/`ACTION`/`ALERT`/`ROTATE`。每行含健康码、耗时、任务状态、监听地址、连续失败数、本轮恢复次数、限频状态、是否告警 |
| `D:\veglia\logs\watchdog-state.json` | 跨轮状态（failStreak / actions / recoveries / alerts / selfErrors） |
| `D:\veglia\logs\maintenance.flag` | 维护标记 |
| `D:\veglia\logs\tunnel-watch.log` | 小时巡检（cloudflared + 公网） |
| `D:\veglia\server\logs\veglia-server.log`、`veglia-mcp.log`、`device-sensor.log` | 业务运行日志 |
| `D:\veglia\logs\archive\` | 轮转归档 |
| `D:\veglia\logs\forensics\` | 事故取证，`keep-*` 前缀，永不删除，不进 Git |

`ops/veglia_logrotate.ps1` 用 **copytruncate** 而不是 rename：`veglia_server.py` 全程以追加模式持有
`veglia-server.log`（重定向的 stdout/stderr），uvicorn 持有 `veglia-mcp.log`；在 Windows 上重命名被持有的
文件要么失败，要么让旧句柄继续写进归档文件，活动日志会静默停止接收关键行。先复制、再原地截断，
所有既有句柄保持有效，追加写者直接从新的文件尾继续。

守卫是"副本长度不得短于截断前的快照长度"，并显式报告 `tail_lost_bytes`（复制期间新写入的尾部）。
默认阈值 16MB、每日志保留 10 份、归档总量预算 400MB，`-DryRun` 只报告不改动。
首轮生产演练：归档 148.2MB，活动日志继续正常写入。

日志与告警正文都不写入 token；`.env`、凭据、原始大体积日志均在 `.gitignore` 内，
`ops/backup/`（任务定义与脚本原始副本）也已 gitignore。

---

## 9. 本次真实改动

仓库（`ops/` 与 `server/`，提交 `8ca50df`→`de5eb0a`）：

- 新增 `ops/veglia_watchdog.ps1`、`ops/veglia_logrotate.ps1`、`ops/veglia_maintenance.ps1`、
  `ops/install_veglia_watchdog.ps1`
- 重写 `ops/check_veglia_tunnel.ps1`：所有权移交（8513/8514 只记录不动作）、维护标记识别（毫秒单位）、
  cloudflared 与公网职责保留
- `server/stop_server.bat` / `server/start_server.bat` 接入维护标记
- `.gitignore` 增加 `ops/backup/`

台式机任务层（由 `ops/install_veglia_watchdog.ps1` 执行，可重复运行、可回滚）：

- `VegliaServer`、`VegliaMCP` 增加 BootTrigger，保留 LogonTrigger，`StopOnIdleEnd=false`
- 注册 `VegliaWatchdog`（Time PT5M + Boot）
- 启用 `Microsoft-Windows-TaskScheduler/Operational`（`/e:true /ms:20971520`，实测 `enabled: true`、
  `maxSize: 20971520`）

安装过程中修掉的 PowerShell 5.1 陷阱（都已写进代码注释，别再踩）：`XmlDocument.LoadXml()` 会向输出流
泄漏根元素；`XmlNamespaceManager` 实现了 `IEnumerable`，裸 `return $m` 会被管道展开成字符串数组，必须
`return ,$m`；返回布尔的 helper 里不能用 `Write-Output`；脚本必须纯 ASCII；`curl` 是别名要用 `curl.exe`。

---

## 10. 验收测试结果

先备份：`D:\veglia\ops\backup\` 保存 4 个任务定义 XML 与 `check_veglia_tunnel.ps1`、start/stop bat 原始副本。
以下每一条都有 `watchdog.log` 的行级证据。

| # | 测试 | 结果 |
|---|---|---|
| T1 | 健康轮不重复启动 | 通过，`ROUND END` 无任何 `ACTION` |
| T2 | 杀掉 MCP，由计划任务自动恢复 | 通过，`10:56:03 START_TASK attempt_in_1h=1/3` → `RECOVERED code=200`，未发通知 |
| T3 | 只杀 Core | 通过，MCP 进程未被牵连（pid 不变） |
| T4 | Core+MCP 全杀 | 通过，同一轮按 core→mcp 顺序恢复 |
| T5 | 维护模式不被误判 | 通过，streak1 `WAIT_STREAK`、streak2 `SKIP_MAINTENANCE`，零动作零告警；`-Off` 后恢复 200 且标记清除 |
| T6 | 连续失败 / 限频 / 冷却 / 告警去重 | 通过，3/3 `RECOVERED` → `FLAP:mcp SENT` → 第 4 次 `SKIP_RATE_LIMIT 3/3` + `LIMIT:mcp SENT` → 重复键 `DEDUPED since_last_min=0` → 恢复后 `HEALTHY_AGAIN` + `RECOVERED:mcp SENT`，告警键清空 |
| T7 | 并发不重叠恢复 | 通过，三轮并发 1×`START` + 2×`SKIP_ALREADY_RUNNING` |
| T8 | 本机 / 局域网 / 公网 | 通过（11:06 复核，见下） |
| T8b | MCP 会话链路（可选） | 通过（11:2x 实测）：`initialize=200`（server `veglia-phone`，protocol `2025-03-26`，返回 session id）→ `notifications/initialized=202` → `tools/list=200` 且 8 个工具齐全 → `DELETE=200` |
| T9 | 任务被禁用时的升级告警 | 通过，`ACTION mcp task_disabled - refusing to enable silently` + `DISABLED:mcp SENT`；重新启用后自动恢复 |

> T8b 的第一次尝试返回 400 `Parse error: key must be a string at line 1 column 2`，
> 那是 PowerShell 把 `-d '{"a":1}'` 里的双引号吃掉导致的**客户端假故障**，不是 MCP 的问题；
> 改用 `--data-binary '@file'` 后全链路 200。以后写探针脚本要记住这条。

> T2、T3、T4、T6、T9 里被杀掉的进程与被禁用的任务**都是人为注入的测试**，并通过真实 lark-cli
> 发出过飞书消息（`FLAP:mcp`、`LIMIT:mcp`、`DISABLED:mcp`、`RECOVERED:mcp`）。不是线上事故。

**未验证 / 受限，不得当成已完成：**

- `WARMING_UP` 分支：逻辑存在，但没有真实制造过"任务 Running、已过预热宽限、HTTP 仍不通"的场景。
- `TASK_MISSING` 分支：未真实触发（要删任务，风险不成比例）。
- 重启后 BootTrigger 的实际效果：未做重启实测（会中断在用服务），只验证了任务定义里触发器已存在。
- T6 期间为了重置测试计数删除过 `watchdog-state.json`（测试动作，会清掉当时已累计的限频历史）。

收尾复核（2026-10-14 11:06 实测，11:26 再次复测）：`127.0.0.1:8513`、`127.0.0.1:8514`、`192.168.101.84:8513`、
`192.168.101.84:8514`、`https://mcp.zhimaheiye.dpdns.org/health` 全部 200；无 `maintenance.flag`；
状态文件告警键为空；Core 与 MCP 各只有一组进程且父进程为任务调度器，无测试残留进程；
11:06 / 11:11 / 11:16 / 11:21 / 11:26 连续五轮 `ROUND END core=200/ok mcp=200/ok`，
`LastTaskResult=0`，其间没有任何 `ACTION` 或 `ALERT` 行——健康状态下确实不打扰人。
`VegliaWatchdog` `LastTaskResult=0`，每 5 分钟正常轮转。

---

## 11. 以后怎么用

**主动停机（升级、改代码）**

```powershell
powershell -File D:\veglia\ops\veglia_maintenance.ps1 -On -Reason "why" -Hours 2
D:\veglia\server\stop_server.bat
# 做完：
D:\veglia\server\start_server.bat          # 或 veglia_maintenance.ps1 -Off
```

**手动恢复（绕过一切门限）**

```powershell
powershell -File D:\veglia\ops\veglia_watchdog.ps1 -Force
```

**只看不做**

```powershell
powershell -File D:\veglia\ops\veglia_watchdog.ps1 -ProbeOnly
```

**"设备状态获取异常" 的排查顺序**（先查这三处，别先怀疑服务挂了）

1. `curl.exe --noproxy "*" -s http://127.0.0.1:8513/health` 和 `:8514/health` — 先确认是不是真的挂了。
2. 请求带的是 `X-Auth-Token` 头或 `?token=`；**Bearer 不是本系统的传法**，错 token 的鉴权失败容易被误读成服务不可用。
3. `tail D:\veglia\logs\watchdog.log` 看 `PROBE`/`ACTION` 行；`wevtutil` 渠道已开，可用
   `Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'}`
   按 100/102/107/129/140/200/201/325 区分触发来源。
4. 数据陈旧而不是端口不通 → 看 `VegliaDesktopSensor` 是否在跑（`Interactive` 登录类型的限制）。
5. 本地都好、只有公网坏 → 那是 `VegliaTunnelHourlyCheck` 的职责，看 `tunnel-watch.log`，
   并人工确认 Clash profile 里 cloudflared 的 DIRECT 规则是否还在运行时 `rules:` 中。

**安装 / 回滚**

```powershell
powershell -File D:\veglia\ops\install_veglia_watchdog.ps1            # 幂等安装
powershell -File D:\veglia\ops\install_veglia_watchdog.ps1 -Verify    # 只检查
powershell -File D:\veglia\ops\install_veglia_watchdog.ps1 -Rollback  # 从 ops\backup 恢复任务定义
```

回滚只恢复任务定义 XML；脚本文件本身要用 `git` 还原。

---

## 12. 已知限制

- 保活依赖 Windows 任务计划程序本身；`lsass`/调度器故障或任务被删除时不会自救，只能靠 `SELF:*` /
  `TASKMISSING:` 告警被人发现。
- 每小时 3 次的恢复预算是有意设置的止损线：如果服务在预算内反复崩溃，继续重启只会掩盖根因，
  所以第 4 次改为告警。
- watchdog 能恢复"进程退出"，不能恢复"进程活着但逻辑坏掉"（健康接口仍返回 200 的情况一律视为健康）。
- 没有跨设备冗余：台式机本身关机时，8513/8514 不存在。
- 未做启动竞态处理。历史上没有证据表明开机时网络未准备好会造成持续故障，因此**没有**给巡检加 AtStartup。
- `device_sensor.py` 只在状态变化时写日志，取证窗口靠它，但它不记录 Core 的退出原因。
- 10-12 首次退出的原因至今未证实；若再次发生，先看 `watchdog.log` 的 `PROBE` 行与归档里的
  `veglia-server.log`（`D:\veglia\logs\archive\`、`forensics\`）。
