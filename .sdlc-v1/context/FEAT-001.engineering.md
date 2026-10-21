# 工程上下文：FEAT-001 终端会话 MCP Server MVP

## 当前有效方案

### 架构设计

**关联产品上下文**：`.sdlc-v1/context/REQ-001.product.md`  
**工程上下文**：`.sdlc-v1/context/FEAT-001.engineering.md`  
**已确认需求**：`NEED-001` 至 `NEED-008`、`CON-001` 至 `CON-003`、`DEC-001` 至 `DEC-011`、`REQ-F01` 至 `REQ-F08` 与用户故事 1 至 8；仅采用其中状态为 `confirmed` 的内容。  
**创建日期**：2026-10-21  
**状态**：当前有效方案（主 Agent 2026-10-21 验收；已并入多实例注册表、默认根目录、命令执行方式、对外语言与 CI 修正）。

#### 背景

本仓库当前只有 SDLC 元数据，没有既有源代码或可复用依赖。FEAT-001 在 macOS 上交付一个仅提供 stdio 的本地 MCP Server：Agent 用五个以会话为中心的 Tool 创建、读取、写入、查询和终止本地进程。它不是单次 shell 包装器；每个会话都有独立进程组、有限输出缓冲、绝对字节游标和明确的清理边界。Linux 必须兼容；Windows、远程主机、常驻 daemon、HTTP 和全屏 TUI 渲染均不实现。

低风险工程默认值：命令以当前用户身份执行；默认根目录优先取 CLI `--allowed-root`（可重复），其次 TOML `allowed_roots`，都没有时取服务启动目录的 realpath，若该目录为 `/` 则改用 `$HOME` 的 realpath 并在 stderr 记录警告；默认 PTY 尺寸为 `80x24`；会话硬超时为 3,600 秒；会话总数上限 16；输出环形缓冲为 1 MiB。`timeout_s`、路径和策略都可由本地 TOML/CLI 在规定上限内收紧，不能由 Tool 放宽。

主 Agent 已在本机核实（2026-10-21）：`uv` 管理的 CPython 3.12.13 可用；`mcp==2.3.0` 可安装，`mcp.server.Server` 与 `mcp.server.MCPServer` 存在，`mcp.server.fastmcp` 已不存在，`mcp.client.stdio.stdio_client`/`StdioServerParameters` 与 `mcp.ClientSession` 可导入；解析得到 `anyio 4.15.1`、`pytest 9.1.1`。这些包已缓存在 `.work/uv-cache`。

#### 复用评估

| 候选（不超过 3 个） | 证据：仓库、语言、许可证、最后活动 | 已覆盖的能力 | 与本 Feature 的具体缺口 | 结论 |
|---|---|---|---|---|
| DesktopCommanderMCP 风格进程工具 | [wonderwhy-er/DesktopCommanderMCP](https://github.com/wonderwhy-er/DesktopCommanderMCP)，JavaScript，MIT；GitHub API 于 2026-10-21 显示 `pushed_at=2026-10-16`、`updated_at=2026-10-18`。README 有长命令、进程交互、分页输出、list/kill、审计与命令 blocklist。 | `start_process`/交互/读取输出/进程管理这一类能力证明会话 Tool 有实际价值。 | API 不是本期五个契约；混入文件、Office 等大范围能力；不能作为 Python 包复用；其 Node 安装路径违背已确认的 Python 3.12 + uv 决策，且没有本期要求的 pipe 分流、绝对字节游标、重启 PID 防复用清扫和连接 EOF 清理语义。 | 不复用代码；仅借鉴“进程持续存在且输出分页”的产品模式。 |
| 维护状态最合格的 tmux MCP 方案：tmux-mcp-rs | [bnomei/tmux-mcp](https://github.com/bnomei/tmux-mcp)，Rust，MIT；GitHub API 显示 `updated_at=2026-10-16`、代码 `pushed_at=2026-07-11`。对比 `nickgnd/tmux-mcp`（JavaScript/MIT，代码 2026-02-14）与 `MadAppGang/tmux-mcp`（Go、许可证未声明，代码 2026-09-04）后，选择具有明确 MIT 许可、结构化 Tool 与完整安全说明的前者作为最可采用的 tmux 候选。 | tmux session/window/pane、TTY、原始键入、命令跟踪和输出捕获均成熟。 | 强制依赖系统 `tmux>=3.0` 和 Rust/预编译二进制；Tool 表面远超 MVP；tmux pane 不是本期 `Session`，不能给出 pipe stdout/stderr、共享绝对字节游标、当前连接 EOF 的整组清理或本期注册表语义。引入它也改变 DEC-001 的无原生编译依赖边界。 | 不引入 tmux；保留标准 PTY 作为后端。 |
| 官方 MCP Python SDK + 标准库 PTY/自建会话层 | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)，Python，MIT；GitHub API 显示 `pushed_at=2026-09-25`、`updated_at=2026-09-27`。PyPI JSON 端点 [`/pypi/mcp/json`](https://pypi.org/pypi/mcp/json) 与 [PyPI release 页](https://pypi.org/project/mcp/) 于规划时核验最新为 **`mcp 2.3.0`**（2026-10-02 发布，Python `>=3.10`，纯 Python wheel）。 | v2 的高层 `MCPServer`（原 `FastMCP`）从类型标注生成输入 schema、`outputSchema` 和 `structuredContent`；支持 stdio；`Context` 访问一次性的 lifespan；官方 client 同时提供 `mcp.client.stdio`、`ClientSession`；v2 客户端取消等待会发送 `notifications/cancelled` 并中断 handler。低层 `Server` 可精确发送 `CallToolResult(is_error=True, structured_content=...)`。 | SDK 不提供本地 PTY/pipe 进程组、字节环形缓冲、游标、会话 timeout、危险命令逐行检查、注册表重启清扫和审计；这些是本 Feature 的领域逻辑，不能由通用协议 SDK 替代。 | **引入成熟依赖 + 自建最小会话层。** 固定 `mcp==2.3.0`，用其低层 `Server` 保证 `isError` 的结构化业务错误；其余会话语义自建。 |

SDK v2 的命名变化须显式处理：`FastMCP` 已更名为 `MCPServer`，而非继续使用 v1 import。高层 `MCPServer` 可由 Pydantic/`TypedDict` 等类型返回值生成 `outputSchema`；但本需求要求业务失败同时具有 `isError=true` 与结构化 `error`。官方低层文档明确该场景应手建 `CallToolResult`，所以本 MVP 选择 `mcp.server.Server`，为成功和失败都声明 JSON Schema 2020-12 的 `output_schema`。这没有改变 DEC-001，仍是官方 SDK。

PTY 选择标准库 `pty`/`os.openpty`，不加 `ptyprocess`：后者当前 PyPI 为 0.7.0、纯 Python wheel，能 spawn/read/write PTY，但其同步读取抽象不能替代 asyncio 的非阻塞多会话缓冲、进程组和重启逻辑。避免该额外依赖可保持最小供应链。`mcp 2.3.0` 已传递依赖 AnyIO（v2 在 Python <3.14 的最低要求为 `anyio>=4.9`）；测试直接使用其 pytest 插件，不另加 `pytest-asyncio`。规划时 PyPI 可见 `anyio 4.10.0`、`pytest 9.1.1`（Python `>=3.10`、MIT）；项目开发依赖锁定为 `pytest>=9.1,<10`，并由 `uv.lock` 固化完整解析结果。

#### 架构与组件

```text
MCP stdio client
  │ tools/call (JSON-RPC)
  ▼
server.py: 低层 Server、schema、校验、Tool 分派、isError 结果
  │                            │
  │                            ├── policy.py ── audit.py
  ▼                            └── config.py
session.py: SessionManager ─── registry.py ─── procinfo.py
  ├── PTY 后端（pty.fork，控制 TTY）
  ├── pipe 后端（asyncio subprocess，stdout/stderr）
  ├── kill/timeout/reap/GC
  └── buffer.py（有界原始字节环 + 游标/等待条件）
         ▲
       keys.py（命名控制键编码）
```

##### `server.py`：MCP Tool 层

- **职责**：用官方低层 `Server` 声明五个 Tool 的 JSON Schema，手动校验请求、调用 `SessionManager`，把成功结果写入 `structuredContent` 与简短文本摘要；把预期业务失败写成 `CallToolResult(is_error=True)`。
- **接口**：`create_server(config)` 创建低层 `Server`；已核对安装的 mcp 2.3.0 源码，handler 通过构造参数 `on_list_tools` / `on_call_tool` 注册，签名为 `(ctx, params)`，从 `ctx.lifespan_context.manager` 访问会话管理器。`run_stdio(config)` 用 `stdio_server()` 取得 SDK 消息流后调用 `Server.run(read, write, server.create_initialization_options())`，CLI 使用 `asyncio.run`。lifespan 在启动时创建 `SessionManager`，等待 `initialize()` 完成独立注册表与安全 sweep 后提供 `AppContext`，关闭 `finally` 在 AnyIO cancellation shield 中无条件等待 `shutdown_all()`。Tool 层先用 `validate_arguments` 校验，再按固定白名单分派；不吞取消异常。预期错误保留 `ToolError.as_dict()`（含 START_FAILED 的身份字段），非预期异常只回传脱敏的 INTERNAL_ERROR。成功文本包含状态/游标等摘要与有界输出预览，完整输出只保留在 structuredContent，避免复制整页。
- **覆盖**：`REQ-F01`、`REQ-F02`、`REQ-F03`、`REQ-F04`、`REQ-F05`、`REQ-F06`、`REQ-F07`。

##### `session.py`：`Session` 与 `SessionManager`

- **职责**：拥有进程句柄/PTY FD、PID/PGID、状态、并发写锁、timeout task、退出信息和输出通知；创建两个后端，串行化同一会话写入，维护 16 会话限制，负责整组信号、reap、保留和 GC。
- **接口**：`SessionManager(config)`，六个入口均为 async：`start(**tool_arguments)`、`read(session_id, cursor, **options)`、`write(session_id, **options)`、`list(session_id=None, include_exited=True)`、`kill(session_id, **options)`、`shutdown_all()`；成功返回与 `contracts.py` 对应的普通 dict，业务错误抛 `ToolError`。Manager 复用参数校验和 `Policy`，完整行 tracker 与实际 write 共用写锁；取消/失败的部分写入后禁用该会话后续写入，避免 tracker 与已发送内容失配。调用者绝不直接操作 `Popen`/FD；T03 负责 MCP 分派；T04 已接通 `initialize()`、每次状态变化的 `_persist()`、`_accept_lost()`、故障锁存与策略审计回调。内部直接调用 start 时也会幂等初始化。
- **覆盖**：`REQ-F01` 至 `REQ-F05`。

##### `buffer.py`：有界原始字节输出缓冲

- **职责**：保存带 `stream`（`pty`/`stdout`/`stderr`）标签的原始字节区段和单调递增绝对偏移；按 1 MiB 上限逐段淘汰；从游标产生 UTF-8 安全文本、`dropped_bytes`、`next_cursor`、`has_more`。纯缓冲不拥有异步任务；T02 的 `Session` 在 append/close 后通知自己的长轮询条件。
- **接口**：`OutputBuffer(capacity_bytes=1048576, mode="pty")`、`append(stream, bytes)`、`read(cursor, max_bytes=65536, strip_ansi=True) -> OutputPage`、`close_stream(stream)`；`OutputPage` 是不可变 dataclass，含 `output/stdout/stderr/next_cursor/dropped_bytes/has_more`。T02 增加 `raw_text(cursor)`（按到达顺序、各流独立解码、未剥离 ANSI 的有界等待窗口）与 `revision`（append 和首次 EOF 都递增），保证 EOF 刷出的 U+FFFD 同样触发 until 匹配。等待和退出状态由会话层组合。
- **覆盖**：`REQ-F02`、`REQ-F04`、`REQ-F06`。

##### PTY/pipe 后端（`session.py` 内的两个小启动路径）

- **职责**：PTY 路径通过 `pty.fork()` 创建新 session、进程组和 controlling TTY，再以 `execvpe` 执行 `$SHELL -l` 或 `/bin/sh -c <command>`；pipe 路径以 `asyncio.create_subprocess_exec(*argv, start_new_session=True)` 分别 drain stdout/stderr，argv 同样显式为 `/bin/sh -c <command>` 或 `$SHELL -l`，避免默认 shell 路径被二次 shell 解释。两者都以非阻塞回调/任务写入同一缓冲并持续 reap。
- **接口**：向 `Session` 提供异步 `write_all`、`poll`、`close_streams`；PTY 写入处理 partial write/EAGAIN，不能阻塞事件循环。
- **覆盖**：`REQ-F01`、`REQ-F02`、`REQ-F03`、`REQ-F05`。

##### `keys.py`：命名控制键

- **职责**：把白名单名称映射为稳定字节序列；初版支持 `Enter`、`Tab`、`Backspace`、`Esc`、`C-c`、`C-d`、`Up`、`Down`、`Left`、`Right`。
- **接口**：`encode_keys(names) -> bytes`；未知名称作为 `INVALID_ARGUMENT`。
- **覆盖**：`REQ-F03`、`REQ-F06`。

##### `policy.py` 与 `config.py`：本地安全默认

- **职责**：加载并验证 TOML，规范化 `allowed_roots`，建立过滤后的环境，检查启动命令和写入的完整逻辑行，统一资源限制。
- **接口**：`Policy.prepare_start(...)`、`Policy.check_complete_lines(...)`；所有拒绝都带规则 ID。
- **覆盖**：`REQ-F01`、`REQ-F06`、`REQ-F08`。

##### `audit.py` 与 `registry.py`：可审计生命周期

- **职责**：追加 JSONL 审计而不记录输出正文；原子保存最少的会话注册表；启动时安全清扫上一进程遗留组并标记旧记录 `lost`。
- **接口**：`Audit(path).log(event)`、请求级 `AuditCall`/`current_call`（ContextVar 隔离并发请求）、`Registry(state_dir, owner_pid, owner_start_time, instance_id).load/snapshot/write/sweep`、不可变 `RegistryRecord` 与纯比较函数 `owner_is_live` / `record_matches_process`；`procinfo.py` 负责原生 PID/PGID/启动时间和 RSS。`sweep(accept_records, grace_ms)` 在文件锁内确认旧 owner、预检全部 leader、逐组清理，再调用 manager 持久化 lost 记录，最后删除旧文件。探测失败不等同于 owner 不存在，无法确认或格式损坏的文件保守保留。
- **覆盖**：`REQ-F04`、`REQ-F05`、`REQ-F08`。

##### 建议包布局

```text
src/terminal_mcp/
  __init__.py
  __main__.py              # terminal-mcp 控制台入口
  server.py                # 低层 MCP Server、schema、Tool 分派
  contracts.py             # 请求/响应 schema、状态和错误常量
  session.py               # Session、Manager、PTY/pipe 后端
  buffer.py                # 原始字节环形缓冲和解码
  keys.py                  # 命名键映射
  policy.py                # cwd/env/命令规则与限额
  config.py                # TOML、平台默认路径
  audit.py                 # JSONL 追加器
  registry.py              # 按实例分文件的会话注册表、启动清扫
  procinfo.py              # 标准库原生进程身份与 RSS 探测
```

#### 关键设计决定

##### 决定 1：MCP v2 使用低层 `Server`，而非 v1 `FastMCP`

- **背景**：Tool 成功结果要有 `outputSchema`/`structuredContent`，业务拒绝必须让 Agent 收到可机读的稳定码并标记 `isError`。
- **决定**：固定 `mcp==2.3.0`；低层 `Server` 手写五个输入 schema 和「成功或错误」联合输出 schema。每个结果都有文本摘要，成功为 `isError=false`，已预期失败为 `isError=true`。
- **替代项**：v2 高层 `MCPServer`（原 `FastMCP`）的类型返回；它适合纯成功结果，但无法同等直接控制本期结构化 `isError` 契约。
- **取舍**：放弃自动 schema 推导，换取跨客户端一致的错误结构；JSON Schema 与 `contracts.py` 必须由同一测试覆盖，防漂移。
- **覆盖**：`REQ-F06`、`REQ-F07`。

##### 决定 2：标准库 PTY + asyncio pipe，自建有界会话层

- **背景**：需要真实 TTY、独立 stdout/stderr、绝对游标、取消不杀进程和整组回收。
- **决定**：PTY 用 `pty.fork()`（子进程建立 session 并拥有 controlling TTY），pipe 用 asyncio subprocess；所有流入 `OutputBuffer`。不使用 tmux 或 `ptyprocess`。
- **替代项**：tmux pane、`ptyprocess`、DesktopCommanderMCP。
- **取舍**：要自行维护少量 Unix FD 边界代码；得到无原生依赖、无系统 tmux 前置条件和与产品一致的状态/缓冲模型。
- **覆盖**：`REQ-F01` 至 `REQ-F05`。

##### 决定 3：连接级服务生命周期与安全保守清扫

- **背景**：stdio 没有断线重连；DEC-006 要求 EOF/正常关闭清掉全部进程，同时强杀后能安全处置遗留进程。
- **决定**：SDK lifespan 每个 server 运行实例一次；其 `finally` 无条件按进程组终止和 reap 所有本实例会话。注册表仅用于下一实例启动时清扫；不承诺恢复或接管旧会话，旧记录一律变 `lost`。
- **替代项**：让 session 脱离 stdio 客户端长期存活，或由新连接接管。
- **取舍**：客户端 EOF 会结束任务；这是 DEC-006 所有权边界，也是没有 daemon 时最安全和最简单的行为。
- **覆盖**：`REQ-F04`、`REQ-F05`、`REQ-F07`。

##### 决定 4：多实例并存时按实例隔离注册表

- **背景**：Manus、Claude Code、Codex 可能同时各自启动一个 `terminal-mcp` 进程。若所有实例共用一个 `sessions.json`，新实例的启动清扫会把另一个仍在运行的实例的会话当作遗留进程杀掉，并发写同一文件也会互相覆盖。
- **决定**：每个实例只写自己的注册表文件，文件内记录 owner 的 PID 与 `owner_start_time` 原生令牌；启动清扫只处理 owner 已不存在的文件，处理前对该文件取 `fcntl.flock` 排他锁，防止两个新实例同时清扫同一文件。审计 JSONL 由多个实例以 `O_APPEND` 单次 `write` 追加整行。
- **替代项**：全局单文件 + 文件锁（仍无法区分活实例与已退出实例的会话）。
- **取舍**：多一次目录扫描；换来“不会误杀其他活实例的会话”这一必须满足的安全属性。
- **覆盖**：`REQ-F05`、US7。

##### 决定 5：对外文本使用英文

- **背景**：DEC-010 确定发布到公开 GitHub 仓库，目标是“任意 MCP 客户端”。
- **决定**：Tool 描述、参数描述、错误 `message`/`hint`、README 与代码注释使用英文；本文中的中文 Tool 描述是语义草案，实现时译为英文并保持同等约束。SDLC 上下文文档保持中文。
- **取舍**：中文用户阅读 README 略增成本；换来公开项目和多数客户端模型的一致理解。README 可附一段简短中文说明。
- **覆盖**：`REQ-F06`、`REQ-F07`。

##### 决定 6：用原生接口读取进程身份与内存，不调用 `ps`

- **背景**：macOS 的 `/bin/ps` 是 setuid root 程序，在受沙箱约束的执行环境（包括本机 Agent 执行环境，也可能包括 MCP 客户端拉起的服务进程）中执行会得到 `Operation not permitted`；同时 `ps -o lstart=` 输出依赖本地化时间格式且精度只有秒，每次探测还要多起一个子进程。
- **决定**：新增 `src/terminal_mcp/procinfo.py`，只用标准库：macOS 通过 `ctypes` 调用 `libproc` 的 `proc_pidinfo(pid, PROC_PIDTBSDINFO)` 读取 `pbi_pid`、`pbi_pgid`、`pbi_start_tvsec`/`pbi_start_tvusec`，以 `PROC_PIDTASKINFO` 的 `pti_resident_size` 读取 RSS；Linux 读取 `/proc/<pid>/stat` 的 pgrp 与 starttime（clock ticks）并结合 `/proc/sys/kernel/random/boot_id`，RSS 读取 `/proc/<pid>/statm`。接口为 `process_identity(pid) -> ProcessIdentity(pid, pgid, start_time) | None` 与 `process_rss(pid) -> int | None`；`start_time` 是不透明字符串令牌（macOS `"<sec>.<usec>"`，Linux `"<boot_id>:<ticks>"`）。返回值区分三种情况：进程存在（返回身份）、确定不存在（`ESRCH`，返回 `None`）、无法确认（`EPERM` 或其他错误，抛 `ProcessProbeError`）；清扫时“无法确认”一律按“不发信号、不删除文件”处理。
- **字段命名**：注册表与代码中原 `owner_lstart`/`leader_lstart` 统一改名为 `owner_start_time`/`leader_start_time`，语义为上述令牌；文中其余提到 `lstart` 处均按此理解。
- **替代项**：继续调用 `ps`（在受限环境不可用）；引入 `psutil`（含原生编译扩展，违背 DEC-001）。
- **取舍**：维护一小段 `ctypes` 结构体定义（`proc_bsdinfo` 136 字节，主 Agent 已在本机验证读出 PID/PGID/微秒级启动时间与 RSS，对非本用户进程返回 `EPERM`、对已退出进程返回 `ESRCH`）；换来无外部命令、无本地化解析、精度更高的身份校验。
- **覆盖**：`REQ-F05`、US7、SC-005。

#### 数据模型 / 关键实体

| 实体 | 关键字段 | 约束 | 覆盖 |
|---|---|---|---|
| `Session` | `session_id` UUID、`name`、`command`、`cwd`、`mode`、`pid`、`pgid`、`status`、`exit_code`、`signal`、`started_at`、`ended_at`、`last_activity_at`、`timeout_s` | 状态仅为 `running`、`exited`、`signaled`、`timed_out`、`failed_to_start`、`lost`；仅 `running` 可写；PID/PGID 均为当前用户子进程。 | `REQ-F01`、`REQ-F03`、`REQ-F04`、`REQ-F05` |
| `OutputBuffer` | `start_offset`、`end_offset`、带 stream 标签的 byte chunks、`capacity_bytes` | 偏移绝不回退；默认容量 1,048,576；淘汰只从最旧字节开始。 | `REQ-F02`、`REQ-F06` |
| `RegistryRecord` | `session_id`、`leader_pid`、`pgid`、`leader_start_time`、`started_at`、`status` | `leader_start_time` 为 `procinfo.process_identity()` 返回的启动时间令牌（决定 6）；仅在 PID、PGID、启动时刻全匹配时允许重启 sweep 向组发信号。 | `REQ-F04`、`REQ-F05` |
| `RegistryFile` | `owner_pid`、`owner_start_time`、`records: RegistryRecord[]` | 每个服务实例只写自己的文件 `sessions/<owner_pid>-<instance_id>.json`；只有 owner 进程已不存在（PID 不存在或启动时间令牌不匹配；无法确认时视为仍存活）时，其他实例才可清扫该文件。 | `REQ-F04`、`REQ-F05` |
| `Policy` | `allowed_roots`、`env_deny_patterns`、`dangerous_command_rules`、`max_sessions`、`buffer_bytes` | allowed root 与 cwd 均使用存在路径的 realpath；规则拒绝优先于 spawn/write。 | `REQ-F08` |
| `AuditEvent` | `timestamp`、`tool`、`session_id`、`command`、`cwd`、`policy`、`result`、`duration_ms` | 每行一个 JSON；绝不写 `output`、`stdout` 或 `stderr` 正文，环境变量值也不写。 | `REQ-F08` |

#### API / 接口契约

##### 通用传输、schema 与错误约定

五个 Tool 只通过 MCP stdio 发布。`tools/list` 的每个 Tool 都含输入 JSON Schema 2020-12、成功/错误联合 `outputSchema` 和下列中文说明。成功结果使用 `structuredContent` 和内容等价的短文本摘要；业务错误返回：

```json
{
  "isError": true,
  "structuredContent": {
    "error": {
      "code": "POLICY_DENIED",
      "message": "cwd 不在 allowed_roots 内。",
      "hint": "选择 /Users/me/project 下的现有目录，或由本机用户修改 config.toml。"
    }
  }
}
```

实际线上的字段名遵循 MCP wire 的 camelCase（`structuredContent`、`isError`）；其中的对象字段维持本文 snake_case，以便 Agent 在 Tool 输入和结果中一致引用。错误文本内容还会作为唯一 `TextContent` 返回，不能只依赖客户端显示结构化字段。

所有成功结果中的 `status` 枚举固定为 `running`、`exited`、`signaled`、`timed_out`、`failed_to_start`、`lost`。`START_FAILED` 的错误联合分支额外含 `session_id:string` 和 `status:"failed_to_start"`，以便客户端可审计该次已保留的失败记录；其他错误联合分支至少含 `error` 对象。任何错误分支都满足声明的联合 `outputSchema`。

| 错误码 | 使用时机 | 给 Agent 的 hint 文本 |
|---|---|---|
| `INVALID_ARGUMENT` | 缺参、类型/边界错误、未知键、非法 regex、负/越界游标、cwd 不存在或非目录。 | “更正本 Tool 的参数；游标使用上一结果的 `next_cursor`，数值不能超出说明范围。” |
| `SESSION_NOT_FOUND` | `session_id` 不存在、已被 `remove=true` 删除或 GC。 | “先用 `terminal_list(include_exited=true)` 查找有效会话；不要重用旧 ID。” |
| `SESSION_EXITED` | 对 `exited`、`signaled`、`timed_out` 或 `failed_to_start` 会话写入。 | “该会话不可再写；可用 `terminal_read` 读取余下输出，或 `terminal_start` 新建会话。” |
| `SESSION_LOST` | 对服务重启后标记 `lost` 的会话写入或终止。 | “原服务已不再拥有该会话；该记录只可查询，必要时新建会话。” |
| `POLICY_DENIED` | cwd、环境名或危险命令规则拒绝。 | “查看 `message` 的规则 ID；选择允许目录或安全命令。MVP 不会请求放行。” |
| `LIMIT_REACHED` | `running` 会话数达到 `max_sessions`。 | “先 `terminal_kill(remove=true)` 清理不用的会话，或由本机用户调整配置。” |
| `START_FAILED` | fork、exec、pipe 创建等 OS 失败。 | “检查 `message` 中的本机原因；命令不存在通常会作为 shell 输出和退出码 127，而非此错误。” |
| `INTERNAL_ERROR` | 非预期内部异常。 | “重试一次；若仍失败，保留时间和 session_id 供本机用户检查审计日志。” |

`terminal_list` 和 `terminal_kill` 对已退出但仍保留的会话是可
查询幂等：它们返回现状，不把已退出本身当错误；`remove=true` 才使随后读/查得到 `SESSION_NOT_FOUND`。所有 Tool 调用都生成审计事件，包括失败。

##### `terminal_start`

**草案 Tool 描述（发布到 `tools/list`）：**“创建一个独立本地终端会话并启动命令。先调用此 Tool，再保存返回的 `session_id` 和 `next_cursor`；对长任务用 `terminal_read(session_id, cursor=next_cursor)` 持续取得新增输出。默认 `mode="pty"`，适合 shell、REPL 和控制键；需要分开的 stdout/stderr 时选 `mode="pipe"`。`wait_ms` 只等待本次初始输出：未给 `until` 时有输出即返回，给 `until` 时等待该正则匹配、进程退出或到期。命令以当前用户权限执行，cwd 和命令会受本机策略检查。”

| 参数 | 类型、默认值 | 边界与语义 |
|---|---|---|
| `command` | `string | null`，默认 `null` | `null` 启动过滤环境中的用户登录 shell（`$SHELL -l`，否则 `/bin/sh`）；字符串为 1 至 32,768 UTF-8 字节，不能含 NUL，以 `/bin/sh -c` 执行（非登录 shell，避免每次加载 profile 产生额外输出和副作用；PATH 等来自继承环境）。 |
| `cwd` | `string | null`，默认服务启动目录 | 必须为现存目录，realpath 必须在 `allowed_roots` 中。 |
| `env` | `object<string,string> | null`，默认 `null` | 最多 128 项；合法 POSIX 环境名；每值最多 4,096 字节、合计最多 65,536 字节、均不能含 NUL。先继承后过滤敏感名，再合并并再次过滤调用方项。 |
| `mode` | `"pty" | "pipe"`，默认 `"pty"` | `pty` 合并输出且有 controlling TTY；`pipe` 分离 stdout/stderr。 |
| `cols` | `integer`，默认 `80` | 20 至 500；只影响 PTY 窗口尺寸，pipe 中仅记录、不影响进程。 |
| `rows` | `integer`，默认 `24` | 5 至 200；规则同 `cols`。 |
| `timeout_s` | `integer`，默认配置的 `3600` | 1 至 86,400，且不得超过配置上限；到期触发 timeout 终止序列。 |
| `name` | `string | null`，默认 `null` | 最多 128 Unicode 字符；仅供 list/audit 识别，不作为 shell 参数。 |
| `wait_ms` | `integer`，默认 `0` | 0 至 30,000；0 为快照式立即返回。 |
| `until` | `string | null`，默认 `null` | Python `re` 正则，最多 1,024 Unicode 字符；编译失败返回 `INVALID_ARGUMENT`。 |
| `strip_ansi` | `boolean`，默认 `true` | 仅决定返回文本是否剥离 ANSI CSI/OSC 控制序列，不改变原始游标。 |

成功 `structuredContent` 为：`session_id:string`、`pid:integer`、`pgid:integer`、`mode`、`status`、`exit_code:int|null`、`signal:string|null`、`output:string|null`、`stdout:string|null`、`stderr:string|null`、`next_cursor:integer`、`dropped_bytes:integer`、`has_more:boolean`、`wait_reason`、`started_at:ISO-8601`。PTY 时仅 `output` 为字符串；pipe 时仅 `stdout`、`stderr` 为字符串；不适用字段为 `null`。`wait_reason` 为 `immediate`、`output`、`until`、`exited` 或 `deadline`。

##### `terminal_read`

**草案 Tool 描述（发布到 `tools/list`）：**“从某会话的绝对字节游标读取新增输出。把上一次返回的 `next_cursor` 原样传入，才能不重读；同一 cursor 可被多个读取者并行使用。默认立即返回，`wait_ms` 可在没有可读新输出时长轮询；它不会阻塞其他会话的 Tool。若旧 cursor 已被 1 MiB 环形缓冲覆盖，检查 `dropped_bytes` 并从返回的 `next_cursor` 继续。PTY 看 `output`，pipe 分别看 `stdout` 与 `stderr`。退出会话仍可读取其保留输出。”

| 参数 | 类型、默认值 | 边界与语义 |
|---|---|---|
| `session_id` | `string`，必填 | 必须是未 GC 的 UUID。 |
| `cursor` | `integer`，必填 | `>=0` 且 `<=` 本次读取开始时 `end_offset`；负数或超过末尾为 `INVALID_ARGUMENT`。 |
| `max_bytes` | `integer`，默认 `65,536` | 4 至 1,048,576，表示本次最多消耗的原始输出字节；UTF-8 安全边界可能使实际消耗略少。 |
| `wait_ms` | `integer`，默认 `0` | 0 至 30,000。 |
| `until` | `string | null`，默认 `null` | 规则、上限同 `terminal_start`。 |
| `strip_ansi` | `boolean`，默认 `true` | 同 `terminal_start`。 |

成功 `structuredContent` 为：`session_id`、`status`、`exit_code`、`signal`、`output`、`stdout`、`stderr`、`next_cursor`、`dropped_bytes`、`has_more`、`wait_reason`、`ended_at:ISO-8601|null`。字段的模式规则同 `terminal_start`。

##### `terminal_write`

**草案 Tool 描述（发布到 `tools/list`）：**“向运行中的会话按到达顺序写入 UTF-8 文本和命名控制键。`text` 不会自动添加换行；要执行 shell/REPL 输入请同时使用 `keys=["Enter"]`。写入后返回从写入前末尾开始的新输出，保存其 `next_cursor` 后再用 `terminal_read` 继续。`C-c` 在 PTY 是发送给前台程序的控制字节；在 pipe 只是把 `0x03` 写到 stdin，均不会终止整个会话；要回收整个进程组使用 `terminal_kill`。危险命令会在 start 和每个完整输入行（含跨调用累计、或 Enter 触发的行）被拒绝。”

| 参数 | 类型、默认值 | 边界与语义 |
|---|---|---|
| `session_id` | `string`，必填 | 必须对应 `running` 会话。 |
| `text` | `string`，默认 `""` | UTF-8 后 0 至 65,536 字节，不能含 NUL；与 key 编码合计仍不得超过 65,536 字节。 |
| `keys` | `array<string>`，默认 `[]` | 最多 32 项；仅接受 `Enter`、`Tab`、`Backspace`、`Esc`、`C-c`、`C-d`、`Up`、`Down`、`Left`、`Right`。至少 `text` 非空或 `keys` 非空。 |
| `wait_ms` | `integer`，默认 `0` | 0 至 30,000。 |
| `until` | `string | null`，默认 `null` | 规则、上限同 `terminal_start`。 |
| `strip_ansi` | `boolean`，默认 `true` | 同 `terminal_start`。 |

成功 `structuredContent` 为：`session_id`、`written_bytes:integer`、`status`、`exit_code`、`signal`、`output`、`stdout`、`stderr`、`next_cursor`、`dropped_bytes`、`has_more`、`wait_reason`。`written_bytes` 是实际写进会话 FD/pipe 的 UTF-8 与 key 总字节数；成功时等于已接受字节数。

##### `terminal_list`

**草案 Tool 描述（发布到 `tools/list`）：**“列出当前连接拥有的会话，或查询一个会话的确定状态。需要读输出时用 `terminal_read`，需要继续交互时仅对 `status="running"` 调用 `terminal_write`。默认包含仍在 10 分钟保留窗口内的已退出和 `lost` 会话；设 `include_exited=false` 可只看运行中会话。服务重启前的记录会显示为 `lost`，不能接管。”

| 参数 | 类型、默认值 | 边界与语义 |
|---|---|---|
| `session_id` | `string | null`，默认 `null` | 给定时只返回该会话，找不到返回 `SESSION_NOT_FOUND`；此时不受 `include_exited` 过滤。 |
| `include_exited` | `boolean`，默认 `true` | `false` 只返回 `running` 会话。 |

成功 `structuredContent` 为 `sessions:SessionSummary[]`，并有 `count:integer`。每个 `SessionSummary` 含 `session_id`、`name`、`command`、`cwd`、`mode`、`pid`、`pgid`、`status`、`exit_code`、`signal`、`started_at`、`ended_at`、`last_activity_at`、`output_start_cursor`、`output_end_cursor`、`output_total_bytes`、`timeout_s`。`output_total_bytes` 是会话累计原始输出量，不等于当前缓冲占用。

##### `terminal_kill`

**草案 Tool 描述（发布到 `tools/list`）：**“终止一个会话的整个 Unix 进程组，而不只是父 shell。默认先发送 TERM，等待 `grace_ms`，仍存活就向同一进程组发送 KILL；`signal="KILL"` 立即升级。结果是最终 reaped 状态。对已退出会话此 Tool 只返回其现状；用 `remove=true` 可在返回后立即丢弃其输出和注册表记录，之后不能再读。”

| 参数 | 类型、默认值 | 边界与语义 |
|---|---|---|
| `session_id` | `string`，必填 | 必须是未 GC 的 UUID。`lost` 返回 `SESSION_LOST`。 |
| `signal` | `"TERM" | "INT" | "HUP" | "KILL"`，默认 `"TERM"` | 首发给进程组的 Unix 信号。 |
| `grace_ms` | `integer`，默认 `1,000` | 0 至 30,000；`KILL` 时忽略并立即 SIGKILL；其余信号到期后升级 SIGKILL。 |
| `remove` | `boolean`，默认 `false` | `true` 仅在整组 reaped 后删除内存/注册表记录。 |

成功 `structuredContent` 为：`session_id`、`status`、`exit_code`、`signal`、`termination_reason`（`manual`、`timeout`、`shutdown`）、`escalated:boolean`、`removed:boolean`、`ended_at`。`status` 为 `exited`（程序自行正常退出）、`signaled`（最终被信号终止）、`timed_out`（硬超时路径）或已保留的 `failed_to_start`。后者没有可终止的进程，只幂等返回原状态，`escalated=false`，`remove=true` 可移除失败记录；这是通用终态幂等约定的明确化，不把失败启动误报为正常退出。状态为 `exited` 的既有会话也有 `escalated=false`；`running` 和 `lost` 不属于 kill 成功分支。

#### 时序与数据流

1. client 先以 `terminal_start` 创建会话，保存 `session_id` 与 `next_cursor`。PTY 创建子 session/控制终端，pipe 建立三条 pipe；两个后端都为该会话建立新进程组。
2. 后台 FD reader/drainer 仅追加带来源标签的原始 bytes 到 `OutputBuffer`，更新绝对 `end_offset`，并唤醒该会话的等待条件；不会在 Tool handler 内等待进程输出。
3. `terminal_read`/`terminal_write` 先取快照。无 `until` 时，有可读新输出即返回；有 `until` 时，每次追加都在从本调用起始 cursor 到当前末尾的未剥离 UTF-8 文本上做 `re.search`，仅在命中、进程退出或 deadline 才返回。故 `Password:`、`>>> ` 等无换行部分字符串可匹配。`wait_ms=0` 永不等待。
4. MCP 客户端取消某个等待请求时，SDK v2 递送取消并取消此 handler 的 await；handler 只释放其 waiter，不调用 `kill`、不清空 buffer，后台进程和其他 Tool 继续工作。
5. `terminal_kill`、timeout、stdio EOF 或 lifespan 退出都走同一整组杀灭/等待/reap 路径。进程退出后仍保留环形输出 600 秒；GC 后只能得到 `SESSION_NOT_FOUND`。

#### 输出游标、等待与进程语义

- **绝对游标**：每次后端读取到原始 byte 都分配连续的会话内 `[start,end)` 偏移。`cursor` 是绝对偏移，`next_cursor` 是本次成功返回的最后一个完整 UTF-8 字符之后的原始偏移，不能把显示字符串长度当作 cursor。
- **淘汰**：容量为 1 MiB。若 `cursor < output_start_cursor`，从当前最早保留字节开始读，`dropped_bytes=output_start_cursor-cursor`；它只计被环淘汰的原始字节，绝不把 ANSI 去除字节计入。`has_more` 表示 `next_cursor < output_end_cursor`。
- **编码**：缓冲永远存原始 bytes。读窗口在 UTF-8 字符边界截断；不足一个有效字节序列的尾部留给下一次，避免拆分多字节字符。非法序列用 U+FFFD 替换，并继续处理后续字节。pipe 的 stdout/stderr 按各自流的字节顺序独立增量解码，保持同一个全局 cursor；读窗口回退到对所有流都安全的公共边界，不返回该边界之后的其他流正文以免重复。`max_bytes` 最小为 4，足够容纳一个连续 UTF-8 字符；跨流交错字符尚未完成或超出本次全局窗口时仍须等待/扩大读取窗口。后端 EOF 必须调用 `close_stream`，将该流最后不完整序列输出为 U+FFFD。
- **ANSI**：默认在解码后跨 chunk/分页去除 ANSI CSI/OSC 序列，并把 `\r\n` 规范为 `\n`；stripped 模式暂缓消费未关闭流末尾的单独 CR，直到后续字符或 EOF，以保证跨页 CRLF 正确且不折叠其他 CR。`strip_ansi=false` 才显示原序列与原换行。只保存淘汰起点的有限 ANSI 解析状态，不保留 OSC 正文；从同一 cursor 重读不改变其他读取者的结果。原始段按最多 8 KiB 合并，大 append 的已淘汰前缀同样分块处理，不分配与总输出量同大的临时副本。匹配 `until` 使用未去除 ANSI 的解码文本，避免展示选项改变控制语义；窗口只含本调用起始 cursor 后仍保留的至多 buffer_bytes 字节，不受结果页 max_bytes 限制。Python `re` 不提供通用流式匹配状态，故采用每次输出/EOF revision 改变时对该有界窗口重新匹配，而不保存或扫描全部历史；没有输出变化时不反复扫描。复杂 regex 的 CPU 风险仍按既有风险声明处理。
- **PTY 回显**：PTY 模式下终端会回显写入内容，因此 `terminal_write` 返回的输出包含输入本身；这是终端语义，需在 Tool 描述中说明。
- **pipe 模式**：stdout、stderr 的每个块在到达时写进同一时间序缓冲并有 stream 标签；结果不合成为不可区分的 `output`，而返回 `stdout` 和 `stderr`。两个值之间的跨流显示先后以后台 reader 实际到达顺序为准，不伪造严格 OS 时间顺序。
- **进程模型**：PTY 的 `pty.fork()` 令子进程成为新 session/进程组 leader 并拥有 controlling TTY，环境加 `TERM=xterm-256color`；pipe 的 `start_new_session=True` 令 leader PID=PGID。命令用 `/bin/sh -c`，默认 shell 用 `$SHELL -l`。fork 后的子进程只做 `TIOCSWINSZ`/`chdir`/`execvpe`，失败时 `os._exit(127)`，不执行其他 Python 逻辑（服务进程可能有 AnyIO 工作线程）。所有 PTY FD 设为 nonblocking，pipe 由独立 drain task 持续消费，避免高输出反压死锁。
- **终止与 reap**：先向 `os.killpg(pgid, signal)` 发首信号，等待最多 `grace_ms`，存活则 `SIGKILL` 整组；PTY 仅对自己创建的 leader 用 `waitpid(..., WNOHANG)`，pipe 由 asyncio child watcher 回收，读取 `returncode` 与 drain EOF 分离，避免后台持有流时 `Process.wait()` 阻塞退出报告。leader 退出后仍观察原组；自然消失立即关闭 I/O 并标记 group_cleaned，保留窗口内的幂等 kill/GC 不再次发信号。父 shell 已退出而后台子进程还持有 PTY 时，仍按已保存 pgid 杀整组。PTY job-control 的前台 PGID 若经 `getsid` 验证属于本会话，也纳入该次终止。macOS 退出窗口的 EPERM 只按“仍存在/尚未证明清理成功”处理，最终必须观察组消失，否则在 KILL 后 1 秒报告 INTERNAL_ERROR；不会把 EPERM 当作已杀灭。信号结果采用 `SIGTERM`/`SIGKILL` 等标准名称；显式 KILL 不计 escalated，宽限后升级才计 true。shutdown、kill 和 spawn 所有权任务屏蔽调用方取消，读/写响应等待取消不终止进程。
- **会话状态**：`running`；`exited`（退出码）；`signaled`（信号）；`timed_out`（watchdog 引发）；`failed_to_start`（OS spawn 失败，伴随 `START_FAILED` 结构化错误）；`lost`（重启前遗留、不能接管）。`timeout_s` 满足后使用 TERM、配置的 1,000 ms 宽限、KILL；`terminal_kill` 的 `signal` 与 `grace_ms` 覆盖该次手动序列。
- **连接结束**：stdio stdin EOF 或 server 的正常关闭导致 SDK lifespan `finally`；它首先停止接收新调用，再杀灭本实例所有进程组并 reap。没有 daemon，因此不会把会话移交给未来连接。
- **注册表和重启 sweep**：状态目录 macOS 为 `~/Library/Application Support/terminal-mcp/`，Linux 为 `${XDG_STATE_HOME:-~/.local/state}/terminal-mcp/`，可用 `--state-dir` 覆盖（测试必须用临时目录）。每个实例只写 `sessions/<owner_pid>-<instance_id>.json`，以临时文件 `fsync` 后 `os.replace` 原子更新；文件含 owner 的 PID 与启动时间令牌，每条记录含 leader 的 PID、PGID 与启动时间令牌（均由 `procinfo.process_identity()` 获取，决定 6）。新实例启动时扫描 `sessions/`：owner 身份仍匹配或无法确认的文件跳过；确定 owner 已不存在的文件先取 `fcntl.flock` 排他锁（取不到则跳过），对每条记录再取 `process_identity(leader_pid)`，PID、PGID、启动时间令牌三者都匹配才对 PGID 发 HUP/TERM→KILL；确定不存在则不发信号；无法确认则不发信号并保留该文件留待下次。已处理的记录并入新实例内存中的 `lost` 列表（可 `terminal_list(include_exited=true)` 查询 600 秒后 GC），并删除原文件；新实例不恢复 I/O 或接管。实例正常退出时在杀完全部会话后删除自己的文件。清扫在持锁后校验 inode 防止双实例读取已被另一清扫者删除的旧文件；先预检同一文件全部 leader，任何无法确认均保留整文件，不部分导入。每次非零信号（含升级 KILL）前重新核对完整身份；组存在性探测出现短暂 EPERM 时按仍未证实消失进行有界等待，仍无法确认则保留文件。lost 记录先写入新实例快照，成功后才删除源文件；lost 的 GC/shutdown 不发信号。

#### 横切质量设计

##### 安全、配置与审计

配置默认位置为 macOS `~/Library/Application Support/terminal-mcp/config.toml`，Linux `${XDG_CONFIG_HOME:-~/.config}/terminal-mcp/config.toml`；`terminal-mcp --config <path>` 是本机用户的显式覆盖入口。目录不存在时使用内置默认，不在启动时生成含用户路径的配置。配置样例与默认值如下：

```toml
# config.toml
allowed_roots = ["/服务启动目录的 realpath"]
env_deny_patterns = ["*TOKEN*", "*SECRET*", "*PASSWORD*", "*_KEY", "AWS_*"]
max_sessions = 16
buffer_bytes = 1048576
default_timeout_s = 3600
max_timeout_s = 86400
kill_grace_ms = 1000
exit_retention_s = 600

# User rules only add restrictions; built-in rules remain enabled.
[[dangerous_command_rules]]
id = "rm-root"
pattern = "rm\\s+[^\\n]*-rf[^\\n]*\\s/(?:\\s|$)"
```

- **cwd**：配置的每个 root 和请求 cwd 都先 `Path.resolve(strict=True)`；以 `os.path.commonpath([cwd, root]) == root` 判断包含关系，拒绝符号链接逃逸、相对路径转义、不存在路径和非目录。
- **环境**：继承环境先按不区分大小写的 glob denylist 剥离，再叠加调用 `env`，最后再次剥离。调用方显式提供命中敏感模式的键会得到 `POLICY_DENIED`，不会静默注入。只保留值不含 NUL、键合法、总量受限的环境。TOML `env_deny_patterns` 与内置模式取并集，危险命令自定义规则也只追加，不允许借空列表/替换规则删除默认保护；这落实“本地配置只能收紧”的默认边界。
- **命令**：启动命令和写入的每个完整逻辑行都按预编译规则检查；默认规则 ID 为 `rm-root`、`mkfs`、`dd-device`、`power-control`、`fork-bomb`、`download-shell`，分别覆盖 `rm -rf /`、`mkfs`、`dd ... of=/dev/...`、`shutdown`/`reboot`/`halt`/`poweroff`、经典 fork bomb、`curl|sh`/`wget|sh`。`terminal_write` 维护每会话的未完成行：跨调用拼接后才检查；CRLF 视为一次提交，Backspace 删除一个字符，C-c 清空待提交行；Enter 会检查当前未完成行且规则命中时不发送 Enter。一次 write 内任一完整行被拒绝时，整个 write 原子拒绝且 tracker 不变，避免前一行已执行而后一行失败。未提交输入最多 65,536 UTF-8 字节（`input-line-limit`）；tracker 与实际写操作须受同一会话写锁保护，写失败由 T02 停止继续写或恢复到实际送出的状态；不模拟箭头键/交互编辑器。
- **审计**：审计文件为 state 目录下 `audit.jsonl`，每次 Tool 调用和策略判定追加一行，字段为 `timestamp`、`tool`、`session_id`、`command`（start 命令或被检查的完整 write 行；其他 Tool 为 `null`）、`cwd`、`mode`、`parameter_summary`、`policy:{decision,rule_id}`、`result:{status,exit_code,signal,error_code}`、`duration_ms`。不写 `output`、`stdout`、`stderr` 或环境变量值。审计文件本身可能包含命令文本，必须按本机敏感日志保护，不能提交到 Git。T04 使用调用前 pending 行、每次规则 allow/deny/filter 行及调用完成行；取消等待记录 `CANCELLED` 后继续向 SDK 传播取消，完整 write 行之外不记录输入正文。所有事件按当前请求 ContextVar 关联，避免并发串话；状态结果行保留退出码/信号/稳定错误码。
- **诚实限制**：这是防 Agent 误操作和意外泄密的 best-effort guardrail，不是 OS sandbox，也不能防止当前用户、shell 转义/编码、已允许程序的二次下载、交互式编辑器动作或故意规避规则。服务以当前用户权限执行；MVP 不实施 elicitation 审批，命中规则一律拒绝。

##### 可靠性、性能与观测

每会话分别使用写锁与输出条件，故并发读取不互相推进 cursor，写入按进入 manager 的顺序串行；等待中的会话不持有全局锁。环形缓冲、每次读取 1 MiB 上限、读端 drain 和 16 会话上限形成容量边界。audit 写失败或 registry 写失败是 `INTERNAL_ERROR` 且锁存故障，直到用户修复 state 目录并重启前都拒绝新启动，不静默降级。start 在 spawn 前验证注册表可写，spawn 后采集并保存原生身份；后置写入/身份探测失败则立即 KILL 并 reap 该新进程后报错。后台退出/GC 写失败同样锁存，在后续 Tool 边界报告 INTERNAL_ERROR。持久化故障不阻断已有会话的显式 kill 与 EOF/shutdown 回收；kill 仍清理进程，但因审计/持久化未成功而返回 INTERNAL_ERROR。运行时日志仅记录错误码/ID；发布前检查 `.gitignore` 和 `git status`，确保审计、state、config 本机路径和任何密钥不进入公开仓库。

#### 风险与取舍

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| SDK v2 与旧客户端实现的兼容差异 | stdio handshake、schema 或取消行为不同。 | 锁定 `mcp==2.3.0`，用官方 `ClientSession` 做真实 stdio 契约测试；验证阶段再以 Manus 实测，不把 v1 API 当作可用。 |
| PTY 的 EOF、后台子进程和 job-control 行为在 macOS/Linux 有差异 | 可能遗留进程或读取无法结束。 | 用 `pty.fork()`、明确 PGID、`killpg` 与 reap；为父退出/后台持有 PTY、EOF 清理和 Linux/macOS分别计划集成测试。 |
| Python regex 的复杂模式可消耗 CPU | `until` 可能降低服务响应性。 | 长度限制 1,024；只在本会话输出更新时评估；在文档说明只给简单 prompt/标记 regex。MVP 不承诺正则执行超时。 |
| 基于启动时间令牌的重启识别只能保守判断 | 启动时刻不匹配或查不到时不能安全杀组，旧记录会 `lost`。 | PID、PGID、启动时间令牌三重匹配才 kill，无法确认时不发信号；记录 `lost` 与审计，绝不冒险杀疑似 PID 复用进程。后续 daemon 可做更强所有权。 |
| 多个客户端同时启动服务实例 | 共用注册表会导致新实例误杀活实例的会话，或并发覆盖写。 | 决定 4：按实例分文件 + owner 存活检查 + `flock`；`test_restart.py` 覆盖“活实例的会话不被另一实例清扫”。 |
| 有线程时 `fork` | Python 3.12 对多线程进程 fork 给出 DeprecationWarning，子进程若执行 Python 逻辑可能死锁。 | 子进程只做 `chdir`/`ioctl`/`execvpe`/`_exit`；该警告视为已知并记录，测试不得出现挂起。 |
| 仅本机 macOS 实测 | Linux 兼容性（CON-001）缺少证据。 | T05 增加 GitHub Actions CI（`ubuntu-latest` 与 `macos-latest` 运行全部测试）；推送后以 CI 结果作为 Linux 证据，CI 不可用时在验证记录中注明缺口。 |
| 命令 blocklist 不是安全边界 | 恶意或经过编码的命令可绕过。 | 如实写入 README；默认 cwd/env 限制、直接拒绝和审计只降低误操作风险；需要强隔离时转入后续容器/OS sandbox Requirement。 |

**回滚**：本功能是新仓库的新 CLI，无数据迁移。发布后发现严重问题时，停止 MCP 客户端的 stdio 注册，删除/回退到上一个 Git tag；正在运行的 server 关闭时自动杀本实例组。保留本机 audit/state 仅用于诊断，禁止提交；发布计划未包含 PyPI，GitHub Release 可标记为撤回说明。

#### 需求覆盖

| 规格需求/场景 | 设计覆盖 | Task | 计划验证 |
|---|---|---|---|
| `REQ-F01`、US1、US3、US5 | `server.py` 五 Tool、`session.py` PTY/pipe 后端 | T01、T02、T03 | 真实 stdio 的启动、状态、隔离、TTY/REPL 测试 |
| `REQ-F02`、US2 | `buffer.py` 绝对游标、条件等待、容量 | T01、T02、T03、T04 | 增量/并发/覆盖、退出等待、200 ms 延迟、50 MB 测试 |
| `REQ-F03`、US3 | `keys.py`、写锁、完整行策略 | T01、T02、T03 | Enter、C-c、REPL 和未知 key 测试 |
| `REQ-F04`、US1、US4、US7 | 状态机、exit/reap、registry `lost` | T02、T03、T04 | 退出码、已退出可读不可写、重启失联测试 |
| `REQ-F05`、US2、US4、US7 | PGID、kill escalation、lifespan shutdown | T02、T04 | 子进程整组、SIGKILL 升级、EOF、2 秒清理、restart sweep |
| `REQ-F06` | 低层 `Server` 联合 schema、稳定错误对象/说明 | T01、T03、T04 | `isError`/error schema 与错误码契约测试 |
| `REQ-F07`、US8 | stdio 入口和唯一 README | T03、T05；US8 由主 Agent validate | SDK `ClientSession` 集成；Manus 人工 E2E |
| `REQ-F08`、US6 | `policy.py`、`audit.py`、TOML | T01、T04 | cwd/env/命令/audit 真实 stdio 测试 |

### 实施计划

**关联产品上下文**：`.sdlc-v1/context/REQ-001.product.md`  
**关联设计**：本文档“架构设计”  
**已确认需求**：同上，仅限 `confirmed` 条目。  
**创建日期**：2026-10-21  
**状态**：当前有效方案（主 Agent 已接受）。

#### 背景

按测试先行、单一串行实施者的顺序创建一个最小 Python 包。先让纯函数/容量/策略契约可独立测试，再接入真实 Unix 进程，之后把它们暴露为低层 MCP stdio Tool，最后集中验证安全、重启、性能和面向用户的 README。没有现有应用或迁移负担。

#### 目标 / 非目标

**目标：**

- 实现本文指定的五个 Tool、状态、游标和安全默认，覆盖产品用户故事 1 至 7 的自动化验收。
- 用固定的官方 SDK、真实 stdio client 与真实子进程测试 MCP 线协议和清理行为。
- 将 README 作为唯一全局 usage 入口，能让 Manus、Claude Code、Codex 按命令注册；为 Story 8 准备人工验收。

**非目标：**

- 不实现 Windows、远程/SSH、多用户、HTTP、daemon、断线重连、resize/全屏 TUI、elicitation、progress/resources 或输出全量落盘。
- 不把 DesktopCommanderMCP、tmux 或 `ptyprocess` 引入项目；不发布 PyPI。

#### 技术栈

- **语言**：Python 3.12。
- **包与运行**：uv，`src` layout，项目控制台命令 `terminal-mcp`。
- **MCP**：官方 `mcp==2.3.0`，低层 `mcp.server.Server` + stdio transport；v2 的 `MCPServer` 是原 FastMCP，作为评估结论记录而非本期 server 基类。
- **并发与进程**：标准库 `asyncio`、`pty`、`os`、`signal`、`subprocess`、`fcntl`/`termios`；没有原生编译依赖。
- **测试**：`pytest>=9.1,<10`、MCP/AnyIO 已有运行时支持；不额外引入 `pytest-asyncio`。

#### 实施层决定

##### 决定 1：先契约和纯组件，后 Unix 进程与 stdio 集成

**背景**：输出边界、策略拒绝和错误 schema 是 Agent 可靠组合的基础，PTY/EOF 失败却不应掩盖它们。

**实际候选：**

1. 先写完进程/MCP server，再补单元与集成测试。
2. 先让 buffer、key、策略、schema 有失败测试，再纵向接入 PTY/pipe、MCP 和生命周期。

**选择：**2，五项 Task 串行执行；每项先写本 Task 指定的最小失败测试并确认失败原因。

**理由：**在没有既有代码的新仓库中，纯组件的确定性测试可锁住游标/规则语义；真实 stdio 集成随后验证最容易出错的取消、PGID 与 EOF。

**覆盖：**`REQ-F01` 至 `REQ-F08`；设计：所有组件。

**决定层级：**计划层实施顺序，不改变产品范围或已确认设计。

#### 测试策略与验收映射

集成 fixture 用 `mcp.client.stdio.StdioServerParameters` 和 `stdio_client` 启动实际 `terminal-mcp --config <temp>/config.toml`，再以 `ClientSession(read, write)`、`initialize()`、`call_tool()` 驱动；不以函数直调替代 Tool 协议。异步测试使用 `@pytest.mark.anyio`。每项业务失败断言 `is_error`、`structured_content.error.code` 与 hint，而非只检查文字。

T03 的共享 fixture 同时显式传入临时 `--state-dir`、`--allowed-root` 和 cwd/HOME，单连接 12 秒、单调用 5 秒、清理 4 秒上限；fixture 只终止该连接返回的会话。US1 的短命令退出断言使用 `wait_ms=2000, until="(?!)"` 确定等待退出：没有 until 时既有契约仍为“有新输出即返回”，不能把不同事件循环调度下首次结果可能为 running 的情况误判成失败或改成隐式等退出。普通增量输出、提前退出和取消等待分别独立覆盖。SDK lifespan 的正常、异常与取消清理另有窄范围故障注入测试，不替代 T04 的真实 EOF/整组回收成功标准测试。

| 产品验收/边界 | 计划测试（路径） | 主要断言 |
|---|---|---|
| US1-1：`echo hello` 结束 | `tests/integration/test_mcp_stdio.py::test_start_waits_for_output_and_exit` | `session_id`、`hello`、`exited`、`exit_code=0`。 |
| US1-2：退出码 3 的 list | `tests/integration/test_mcp_stdio.py::test_list_reports_nonzero_exit` | `terminal_list` 返回 `exited` 与 3。 |
| US1-3：不存在命令 | `tests/integration/test_mcp_stdio.py::test_missing_command_is_readable` | 127+原因或 `START_FAILED` 的结构化错误。 |
| US2-1：增量游标 | `tests/integration/test_mcp_stdio.py::test_incremental_read_has_no_duplicate_or_gap` | 多次 `next_cursor` 无重复遗漏、运行中。 |
| US2-2：等待不阻塞另一会话 | `tests/integration/test_mcp_stdio.py::test_waiting_read_does_not_block_other_write` | 10 秒 read 期间另一会话 write 在短阈值内完成。 |
| US2-3：缓冲覆盖 | `tests/unit/test_buffer.py::test_old_cursor_reports_dropped_bytes` 与 stdio 回归 | `dropped_bytes>0`、只留最新、占用不超容量。 |
| US2-4：会话 timeout | `tests/integration/test_lifecycle.py::test_stdio_two_second_timeout`（并有 PTY/pipe 核心测试） | 2 秒 timeout 后 `timed_out` 且组消失。 |
| US3-1：Python REPL + Enter/until | `tests/integration/test_mcp_stdio.py::test_repl_write_and_until_prompt` | 输出有 42，`>>> ` 无换行也能匹配。 |
| US3-2：C-c 中断前台 sleep | `tests/integration/test_mcp_stdio.py::test_ctrl_c_keeps_shell_running` | sleep 结束，shell `running` 并重新出现提示。 |
| US3-3：PTY TTY 检测 | `tests/integration/test_mcp_stdio.py::test_pty_has_controlling_tty` | `test -t 1` 成功。 |
| US4-1：杀父和后台 sleep | `tests/integration/test_lifecycle.py::test_kill_reaps_whole_process_group` | 记录的两个 PID 在 2 秒内均不存在。 |
| US4-2：TERM 忽略后升级 | `tests/integration/test_lifecycle.py::test_kill_escalates_to_sigkill` | `escalated=true` 且最终结束。 |
| US4-3：stdio EOF | `tests/integration/test_lifecycle.py::test_stdio_eof_reaps_all_groups` | server 退出，相关进程表为 0。 |
| US4-4：退出后 write、仍可读 | `tests/integration/test_mcp_stdio.py::test_exited_session_rejects_write_but_retains_output` | `SESSION_EXITED`，read 仍返回余下 bytes。 |
| US5-1：cwd/env 隔离 | `tests/integration/test_mcp_stdio.py::test_sessions_isolate_cwd_and_env` | 各自 `pwd`/`X` 输出正确无串扰。 |
| US5-2：达到上限 | `tests/integration/test_mcp_stdio.py::test_session_limit_preserves_existing_sessions` | 第 17 个 `LIMIT_REACHED`，前 16 个仍运行。 |
| US6-1：禁止 `/etc` cwd | `tests/integration/test_policy_audit.py::test_disallowed_cwd_starts_nothing` | `POLICY_DENIED`，无 PID/进程。 |
| US6-2：过滤 `GITHUB_TOKEN` | `tests/integration/test_policy_audit.py::test_sensitive_env_is_not_inherited` | `env` 输出没有 token。 |
| US6-3：start/write 拒绝危险命令并审计 | `tests/integration/test_policy_audit.py::test_dangerous_start_and_complete_write_are_denied_and_audited` | 两处 `POLICY_DENIED`、规则 ID/audit 判定存在且未执行。 |
| US6-4：任意调用审计字段 | `tests/integration/test_policy_audit.py::test_audit_has_required_metadata_without_output_body` | 时间、tool、会话、命令/cwd、policy、result；无输出字段。 |
| US7-1：强杀后 restart sweep | `tests/integration/test_restart.py::test_restart_sweeps_matching_pipe_group_and_lists_lost` | 遗留组结束，旧 ID 在 list 为 `lost`。 |
| US7 补充：多实例并存 | `tests/integration/test_restart.py::test_live_instance_sessions_are_not_swept` | 实例 A 有运行中会话时启动实例 B（同一 `--state-dir`），A 的会话进程仍存活且可读写；B 的 list 中不出现 A 的会话。 |
| 边界 1：cwd 不存在/非目录 | `tests/unit/test_policy.py::test_invalid_cwd` | `INVALID_ARGUMENT`，不 spawn。 |
| 边界 2：非法 UTF-8/跨块多字节 | `tests/unit/test_buffer.py::test_incremental_utf8_and_invalid_bytes` | 不拆字符、非法为 U+FFFD、后续不丢。 |
| 边界 3：无换行 prompt | `tests/integration/test_mcp_stdio.py::test_until_matches_partial_line` | `Password:`/`>>> ` 均可触发 until。 |
| 边界 4：50 MB 突发输出 | `tests/integration/test_capacity.py::test_50mb_output_is_bounded` | server 仍响应，容量/RSS 增量符合 SC-005。 |
| 边界 5：等待期间退出 | `tests/integration/test_mcp_stdio.py::test_wait_returns_early_when_process_exits` | 未等满 `wait_ms` 即最终状态。 |
| 边界 6：非法 cursor | `tests/integration/test_mcp_stdio.py::test_invalid_cursor_is_structured_error` | 负数和超过末尾均为 `INVALID_ARGUMENT`。 |
| 边界 7：并发 read/write | `tests/integration/test_mcp_stdio.py::test_concurrent_reads_are_independent_and_writes_are_serial` | 相同 cursor 独立；输入顺序不交错。 |
| 边界 8：父退出、后台持有 PTY | `tests/integration/test_lifecycle.py::test_kill_reaps_group_after_parent_exit` | kill 后后台子进程消失、FD/reap 完成。 |

**成功标准测量：**

- **SC-001**：上表 US1 至 US7 的所有测试属于默认 `pytest -q` 收集集；没有 `xfail` 或跳过，测试报告逐行对应表中 30 项。
- **SC-003**：`test_wait_latency_under_200ms` 令子进程输出其 `time.monotonic_ns()`，等待 read 收到后记录 client `monotonic_ns()`；在正常 macOS/Linux 测试机断言差值 `<200 ms`，并保存失败时的实际延迟。
- **SC-004**：`test_kill_reaps_whole_process_group` 和 `test_stdio_eof_reaps_all_groups` 以 20 ms 轮询 `procinfo.process_identity(<child-pid>)`（或对测试自建 PID 的 `os.kill(pid, 0)`），从 kill/EOF 计时起断言 2 秒内无目标 PID；还断言 server process 已退出或 session 已 reaped。
- **SC-005**：`test_50mb_output_is_bounded` 由官方 stdio 测试 harness 获取其创建的 server PID，并与临时实例注册表文件名互证；50 MB 写入期间每 20 ms 用 `procinfo.process_rss(<pid>)` 采样（server PID 取自测试启动的子进程句柄）。与任务开始前 idle RSS 比较，断言峰值增量 `<= 3 * buffer_bytes`（3 MiB 默认），并检查 buffer 的 retained bytes `<=1 MiB`；该测试只在 macOS/Linux 执行。
- **SC-002 / US8**：不自动化替代真实客户端。主 Agent 在 validate 阶段按 README 把 stdio command 注册到 Manus，让 Agent 仅看 Tool 描述完成 `python3 -i`、表达式、读取、终止；记录原始 Tool 调用、无参数错误重试的结果与环境。当前 Task 只交付此验证所需文档。

实现者每一 Task 至少执行其相关测试，并在最终执行：

```bash
UV_CACHE_DIR=.work/uv-cache uv lock --locked
UV_CACHE_DIR=.work/uv-cache uv run pytest -q
UV_CACHE_DIR=.work/uv-cache uv run terminal-mcp --help
```

说明：本机执行策略下，访问 PyPI 的 `uv` 命令需在 exec 调用时声明需要网络；`mcp==2.3.0`、`pytest` 及其依赖已预热到 `.work/uv-cache`，网络不可用时可加 `--offline`。

#### 实施阶段

##### 阶段 1：准备与纯契约

- FEAT-001-T01 建立 Python/uv 包、Tool schema、配置、buffer、键和策略的测试先行基础。— **覆盖：**`REQ-F02`、`REQ-F03`、`REQ-F06`、`REQ-F08`；**设计：**`contracts.py`、`config.py`、`buffer.py`、`keys.py`、`policy.py`、`audit.py`、`registry.py`。

##### 阶段 2：本地会话核心

- FEAT-001-T02 实现 PTY/pipe `SessionManager`、进程组、写入、timeout、reap 与保留 GC。— **覆盖：**`REQ-F01` 至 `REQ-F05`；**设计：**`session.py`、`buffer.py`。

##### 阶段 3：MCP 垂直能力

- FEAT-001-T03 暴露五个低层 MCP stdio Tool 并完成故事 1、2、3、5 的真实协议测试。— **覆盖：**`REQ-F01` 至 `REQ-F07`；**设计：**`server.py`、所有 Tool 契约。

##### 阶段 4：安全与故障生命周期

- FEAT-001-T04 完成审计、重启 sweep 和安全/容量/清理的故事 4、6、7 与成功标准验证。— **覆盖：**`REQ-F02`、`REQ-F04` 至 `REQ-F06`、`REQ-F08`；**设计：**`audit.py`、`registry.py`、`policy.py`、`session.py`。

##### 阶段 5：全局 usage 入口与收尾

- FEAT-001-T05 编写 README 唯一 usage 入口、锁定依赖并完成默认测试套件。— **覆盖：**`REQ-F06`、`REQ-F07`、US8 验证准备；**设计：**全局 usage 与发布准备。

#### 工程任务

**输入**：本文档“架构设计”和“实施计划”。  
**前置条件**：主 Agent 接受本工程上下文，并登记 Feature/Task。  
**测试**：严格测试先行；一个实现子 Agent 串行执行，不标注并行候选。

##### 阶段 1：准备

1. **FEAT-001-T01 — 建立受测的包契约、配置和纯组件**
   - **代码范围**：`pyproject.toml`、`uv.lock`、`src/terminal_mcp/__init__.py`、`src/terminal_mcp/__main__.py`、`src/terminal_mcp/contracts.py`、`src/terminal_mcp/config.py`、`src/terminal_mcp/buffer.py`、`src/terminal_mcp/keys.py`、`src/terminal_mcp/policy.py`、`src/terminal_mcp/audit.py`、`src/terminal_mcp/registry.py`、`tests/unit/test_buffer.py`、`tests/unit/test_keys.py`、`tests/unit/test_policy.py`、`tests/unit/test_contracts.py`、`tests/unit/test_audit_registry.py`、`tests/conftest.py`。
   - **依赖**：无。
   - **完成条件**：锁定 Python 3.12、`mcp==2.3.0` 和 pytest；五 Tool 的 input/成功/错误 schema、稳定码、平台路径/TOML 默认、1 MiB 原始字节环、UTF-8/ANSI 规则、键映射、cwd/env/完整行规则和 JSONL/注册表原子写入均有通过的最小实现。
   - **验证**：先令上述单元测试因模块缺失/期望不符失败，再执行 `UV_CACHE_DIR=.work/uv-cache uv run pytest -q tests/unit` 通过。

##### 阶段 2：会话核心

2. **FEAT-001-T02 — 实现可清理的 PTY 与 pipe 会话管理器**
   - **代码范围**：`src/terminal_mcp/session.py`、对 `buffer.py`/`contracts.py` 的必要最小调整、`tests/integration/test_session_core.py`、`tests/integration/test_lifecycle.py`。
   - **依赖**：`FEAT-001-T01`。
   - **完成条件**：PTY 使用 `pty.fork()` 和 controlling TTY，pipe 分流 stdout/stderr；新进程组、非阻塞 drain/partial write、每会话写锁、watchdog、kill 升级、reap、10 分钟保留/GC 与取消 waiter 不杀进程的核心行为均实际运行。
   - **验证**：先编写会失败的真实子进程测试，再执行 `UV_CACHE_DIR=.work/uv-cache uv run pytest -q tests/integration/test_session_core.py tests/integration/test_lifecycle.py`；其中包括子组、timeout、parent-exit 及取消语义。

##### 阶段 3：故事 1、2、3、5 的 MCP 垂直交付

3. **FEAT-001-T03 — 发布五个 stdio Tool 并覆盖会话、游标、交互和隔离**
   - **代码范围**：`src/terminal_mcp/server.py`、`src/terminal_mcp/__main__.py`、对 `contracts.py`/`session.py` 的必要最小调整、`tests/integration/test_mcp_stdio.py`。
   - **依赖**：`FEAT-001-T02`。
   - **完成条件**：低层 `Server` 只发布五个本文契约中的 Tool；所有成功有 `structuredContent`，预期失败有 `isError` 的结构化 error；真实 `stdio_client + ClientSession` 覆盖 US1、US2、US3、US5 和相关边界。
   - **验证**：先添加每个故事的失败集成测试，再执行 `UV_CACHE_DIR=.work/uv-cache uv run pytest -q tests/integration/test_mcp_stdio.py`；确认 `tools/list` 的 schema/description 可被 client 读取。

##### 阶段 4：故事 4、6、7 与容量可靠性

4. **FEAT-001-T04 — 完成连接关闭、重启清扫、安全审计和性能边界**
   - **代码范围**：`src/terminal_mcp/procinfo.py`、`src/terminal_mcp/session.py`、`src/terminal_mcp/server.py`、`src/terminal_mcp/policy.py`、`src/terminal_mcp/audit.py`、`src/terminal_mcp/registry.py`、`tests/unit/test_procinfo.py`、`tests/integration/test_lifecycle.py`、`tests/integration/test_policy_audit.py`、`tests/integration/test_restart.py`、`tests/integration/test_capacity.py`。
   - **依赖**：`FEAT-001-T03`。
   - **完成条件**：lifespan EOF shutdown、按实例分文件且检查 owner 存活与 PID/PGID/启动时间令牌的 restart sweep（`procinfo.py`，决定 6）（含多实例并存不误杀）、`lost` 查询、审计字段、安全拒绝、SC-003 读延迟、SC-004 两秒清理和 SC-005 50 MB RSS/缓冲边界均被可重复测试覆盖。
   - **验证**：先写失败的 US4/US6/US7、八个边界和 SC 测试，再执行 `UV_CACHE_DIR=.work/uv-cache uv run pytest -q tests/integration/test_lifecycle.py tests/integration/test_policy_audit.py tests/integration/test_restart.py tests/integration/test_capacity.py`。

##### 阶段 5：全局使用入口与最终收尾

5. **FEAT-001-T05 — 交付 README 全局 usage 入口和可复现本地验证**
   - **代码范围**：`README.md`、`LICENSE`（MIT）、`.github/workflows/ci.yml`（`ubuntu-latest`、`macos-latest` 上运行 `uv run pytest -q`）、`pyproject.toml`、`uv.lock`、必要时 `tests/` 的测试命令注释；不增加第二份使用文档。
   - **依赖**：`FEAT-001-T04`。
   - **完成条件**：README 是唯一全局 usage 入口，按 Story 1 至 8 指回产品上下文；列出 actor、前置本机权限/环境、配置参数的 TOML/CLI 来源、默认范围、五 Tool 的组合步骤、错误恢复、当前支持边界；列出 uv/uvx、Manus/Claude Code/Codex 的 stdio 注册命令和 Story 8 的人工步骤；`uv.lock` 与声明相符。
   - **验证**：先以 README 命令从干净 uv cache 启动并跑 `--help`，再执行 `UV_CACHE_DIR=.work/uv-cache uv run pytest -q`。主 Agent 的 validate 阶段按 README 对 Manus 完成 US8，不能用本 Task 的合成测试替代。

#### 依赖与执行顺序

`FEAT-001-T01 → FEAT-001-T02 → FEAT-001-T03 → FEAT-001-T04 → FEAT-001-T05`。所有 Task 由一个实现子 Agent 串行、测试先行完成；任一失败先修复当前 Task，不跨 Task 提前实现。

#### 全局 usage 入口与参数来源

`README.md` 是本项目唯一面向使用者的 usage 入口，Build 必须维护它而不是复制产品规格。它以 `US1` 至 `US8` 导航到 `.sdlc-v1/context/REQ-001.product.md` 的既有故事：US1 启动/取结果，US2 游标读，US3 交互输入，US4 kill/EOF，US5 隔离，US6 策略/audit，US7 restart `lost`，US8 Manus E2E。

README 将明确参数所有者：Tool 参数以 `tools/list` 的 schema/description 为权威，默认安全限制以 config TOML 为权威，`--config` 是本机路径覆盖入口，`UV_CACHE_DIR` 是安装/测试缓存位置；不记录任何 secret。README 的目标注册形式为：

```text
# 已克隆仓库
uv run --directory <repo> terminal-mcp --allowed-root <dir>

# 发布后从公开 Git 仓库运行
uvx --from git+https://github.com/arnow117/terminal-mcp terminal-mcp --allowed-root <dir>
```

Manus 使用本机 MCP/自定义 MCP 的上述 command form（command 为 `uv`，其余为 args）；Claude Code 使用 `claude mcp add terminal-mcp -- uv run --directory <repo> terminal-mcp --allowed-root <dir>`；Codex 使用 `codex mcp add terminal-mcp -- uv run --directory <repo> terminal-mcp --allowed-root <dir>`。公开分发地址已确认是 `arnow117/terminal-mcp`；README 也给出等价 `uvx` 与通用 `mcpServers` JSON。客户端均以当前用户启动 server，显式 `<dir>` 使 `allowed_roots` 不依赖注册 command 的 cwd。

## 实现记录（实际）

- **FEAT-001-T01（实现完成，待主 Agent 验收）**：审阅并修正既有包/config/contracts/keys/policy 草稿，完成 `buffer.py`、`audit.py`、`registry.py`、`uv.lock` 和五个 `tests/unit/test_*.py`；五 Tool 联合 schema/稳定错误、默认路径/CLI/TOML、原始字节环/跨流 UTF-8/ANSI/CRLF、命名键、cwd/env/完整行策略、单次 O_APPEND JSONL 和按实例原子快照均有纯组件测试。保留 `terminal_kill` 对 `failed_to_start` 的幂等成功，相关契约已统一。历史首次 RED 为缺包 4 个收集错误（exit 2），随后依赖缓存不足导致受阻；主 Agent 补齐缓存和环境后，本次完全离线恢复，先运行 `uv run --offline pytest -q tests/unit` 再现缺 buffer/audit 的 3 个收集错误（exit 2），新增回归又分别证实 denylist 被替换/CRLF 重复提交（2 failed）、大 append 复制前缀/regex OverflowError（2 failed）和 TOML regex OverflowError（1 failed）。修复/重构后，设置 `UV_CACHE_DIR=.work/uv-cache UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1` 执行 `uv run --offline pytest -q tests/unit` 得 **117 passed**（exit 0）；`uv run --offline terminal-mcp --help` 和 `uv lock --locked --offline` 均 exit 0（37 packages）。中途 stdlib tracemalloc 尝试在仓库外生成 pyc 被策略拒绝，后续禁用 bytecode 写入，不申请额外权限。测试隔离 HOME/XDG/config/state，单测 15 秒上限；无实际进程信号、无网络、无新增依赖、未修改 lifecycle state。服务运行保留 T03 stub，异步会话/实际 sweep/README 分别留给 T02/T04/T05，当前证据不声称完成真实 MCP 或完整 US1–US8 验收。
- **FEAT-001-T02（实现完成，待主 Agent 验收）**：新增 `src/terminal_mcp/session.py` 与 `tests/integration/test_session_core.py`、`test_lifecycle.py`，最小扩展 `buffer.py` 的 raw_text/revision；实现真实 PTY/pipe、新 session/进程组、尺寸/TERM、partial write/EAGAIN、写锁、五种等待原因、取消隔离、timeout/信号升级、leader reap 与流 EOF 分离、自然退出清理、600 秒保留/GC 和并行 shutdown_all。首次 RED：两测试模块因缺 session 模块而收集失败（exit 2）；补充 RED 分别锁定自然退出未完成清理（2 failed）、EOF 的 U+FFFD 未重匹配（2 failed）与 macOS 瞬时 EPERM 清理（1 failed + teardown error）。修复后设置 `UV_CACHE_DIR=.work/uv-cache UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp`，执行 `uv run --offline pytest -q tests/unit tests/integration/test_session_core.py tests/integration/test_lifecycle.py` 得 **159 passed**（7.95 s，exit 0），随后两个集成文件连续复跑两次均 **42 passed**（7.57/7.79 s）；`uv lock --locked --offline` 为 exit 0（37 packages），无新增依赖。测试只使用临时 config/state/cwd/HOME 和自己创建的 PID，断言 kill/shutdown 后 2 秒内 PID 消失；自然退出、父退出后后台持有 PTY/pipe、TERM 忽略、立即 kill、取消 read/write/kill wait、控制 TTY/C-c、REPL、上下文隔离/上限、GC、fork/exec 失败均覆盖。最初一次运行因 stdlib pyc 写到仓库外被拒，后续禁用 bytecode；本机自建 shell 可执行文件冷启动有延迟，登录参数测试使用有界 5 秒等待。仅交付内部会话核心，README 按原计划留给 T05，MCP/EOF 接线、restart/audit、Linux CI 与完整成功标准分别由 T03/T04/T05 后续验证；未修改或提交 state.json。

- **FEAT-001-T03（实现完成，待主 Agent 验收）**：新增 `src/terminal_mcp/server.py`，接通 `__main__.py` 的真实 stdio CLI，扩展 `tests/conftest.py` 并新增 `tests/integration/test_mcp_stdio.py`；按已安装 mcp 2.3.0 源码使用构造式 handler 注册、SDK 消息流和 lifespan context，只发布 contracts.py 的五 Tool/schema/英文描述。成功返回 structuredContent 与有界短文本，预期业务错误返回 isError/error/code/message/hint 及同文摘要，内部异常脱敏；每次分派前校验参数，lifespan 在正常、异常、取消退出时均等待 shutdown_all。首次 RED：`uv run --offline pytest -q tests/integration/test_mcp_stdio.py --tb=short` 得 **3 failed, 40 errors**（2.14 s，exit 1），确认原因是 CLI 尚为占位实现而关闭握手连接及缺失 server 模块；实现后同命令得 **43 passed**（13.56 s），首次全量 **202 passed**（23.05 s）。补充 START_FAILED 保留身份、INTERNAL_ERROR 脱敏、取消 lifespan 三个测试并移除未使用的 fixture 开关后，设置 `UV_CACHE_DIR=.work/uv-cache UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp` 运行 `uv run --offline pytest -q` 得 **205 passed**（23.38 s，exit 0）；`uv lock --locked --offline`（37 packages）、`uv run --offline terminal-mcp --help`、`git diff --check` 均 exit 0。真实 stdio 覆盖 US1-1~3、US2-1~3、US3-1~3、US4-4、US5-1~2 与边界 3/5/6/7，另验证 pipe 分流、取消 read 不杀进程、终态 kill/remove 和所有 Tool 的结构化参数错误。无需修改 contracts.py/session.py，无新依赖、网络、真实用户 state 目录或 lifecycle 写入；审计/重启/真实 EOF 成功标准、README/Linux CI/Manus 实测分别留给 T04/T05/validate。

- **FEAT-001-T04（实现完成，待主 Agent 验收）**：按决定 6 新增标准库原生 `procinfo.py`（macOS libproc / Linux procfs），统一 `owner_start_time`/`leader_start_time`；完成按实例注册表、状态变更原子持久化、flock/inode/owner/leader 三元身份保守清扫、SIGKILL 重启遗留组回收、600 秒 lost/SESSION_LOST、审计与策略回调、故障锁存和 EOF 删除实例文件。复审并更新前次测试草稿；本次原生模块 RED 为 **4 个收集错误**（exit 2），接线 RED 为 **2 failed**；原生单测先行 **8 passed**，首轮专项 **34 passed**。首次全量 **6 failed, 226 passed**（旧注册表参数和 T03 mock 接口未更新），以最小改动调整 `tests/unit/test_policy.py`、`test_audit_registry.py`、`tests/integration/test_mcp_stdio.py`；加强多进程重启测试后发现 macOS 短暂组探测 EPERM（**1 failed, 237 passed**），新增确定性 RED 后修复为有界等待；另新增审计失败不应阻止显式 kill 的 RED 后修复。所有运行设置 `UV_CACHE_DIR=.work/uv-cache UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp` 且使用 `uv run --offline`。最终未再改代码的两次完整 `pytest -q` 均 **240 passed**（46.72 s / 34.92 s，exit 0）；日志为 `.work/t04-full-green-1.log`、`.work/t04-full-green-2.log`，此前 RED 与专项日志同在忽略目录 `.work/`。专项测得 wait 延迟 **19.291 ms**、50 MB 输出 RSS 增长 **1,654,784 bytes**（小于 3 MiB）、突发期间最大 list 延迟 **2.213 ms**，retained bytes 始终不超过 1 MiB；两次最终全量也执行相同严格性能断言。US4-3、US6-1~4、US7-1、US2-4、边界 1/2/4/8 与 SC-003/004/005 均覆盖；原生身份测试含 self、child、exited child、pid 1、ABI/错误返回和 Linux stat 尾括号解析。未调用受禁进程命令、未联网、未添加依赖、未写真实用户 state/config/HOME、未修改或提交 state.json；README/CI 仍按计划留给 T05，Linux 真机与 Manus US8 验证仍由后续阶段完成。

- **FEAT-001-T05（实现完成，待主 Agent 验收）**：新增唯一全局 usage 入口 `README.md`、MIT `LICENSE`、双平台 GitHub Actions `.github/workflows/ci.yml`，并在 `pyproject.toml` 补充 README/MIT 元数据；README 按实际 `tools/list` 契约记录五 Tool、游标/REPL/kill 流程、状态/错误恢复、TOML/路径/审计注册表、安全及生命周期边界，以及 Manus/Claude Code/Codex/JSON 的本地 `uv` 和公开 `uvx` 注册命令与 US1–US8（含 Manus 手工 E2E）映射。`uv.lock` 经检查无须变更。使用 `UV_CACHE_DIR=.work/uv-cache UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp` 验证 `uv lock --locked --offline`、`uv run --offline terminal-mcp --help`、`grep -n "/Users/" README.md`（无匹配）、Ruby YAML 结构检查和 `git diff --check` 均 exit 0；`uv run --offline pytest -q` 得 **240 passed**（38.32 s，exit 0）。未改运行时代码、测试或 `.sdlc-v1/state.json`，未联网、未新增依赖；Linux CI 运行与 Manus US8 仍留主 Agent validate。

## 验证记录（实际）

- **状态**：`PASS`（2026-10-21，主 Agent 执行与核对）。验证方式：`correctness`（单元 + 真实 stdio 集成测试）+ 真实客户端 E2E（Manus）。
- **被测版本**：`c2d633ab4f10572762ad7356ba74846f0598fa9c`（实现提交 `0188a33` 之上仅有 `.sdlc-v1/state.json` 变化）。
- **命令与结果**：
  - 本机 macOS 26.4.1 arm64、CPython 3.12.13、mcp 2.3.0，2026-10-21T21:02:55+08:00：`UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp uv lock --locked --offline`（37 packages，exit 0）；`uv run --offline terminal-mcp --help`（exit 0）；`uv run --offline pytest -q` → **240 passed in 35.20s**（exit 0）；`uv run --offline pytest -q -s tests/integration/test_capacity.py` → 2 passed，`wait_latency_ms=15.096`，`rss_growth=1720320`（baseline 80,019,456 → peak 81,739,776），突发期间最大调用 4.870 ms。
  - GitHub Actions run `39610078999`（push 到 `feat/FEAT-001-terminal-mcp-mvp`，headSha `c2d633a`，2026-10-21T13:40:40Z→13:42:11Z）：`uv sync --locked` + `uv run pytest -q`；ubuntu-latest **240 passed in 45.93s**，macos-latest **240 passed in 63.11s**，均 success。
  - Manus E2E：创建 stdio 自定义连接器 `terminal-mcp`（uid `1d2aacd6-7c28-4355-afdb-4a20f83174e8`，命令 `bash -lc "... exec uvx --from git+https://github.com/arnow117/terminal-mcp@c2d633a… terminal-mcp"`）；主 Agent 先 `tools/list` 冒烟确认五个 Tool 发布。随后新建云端沙箱任务 `Kir4ooJTDyTA9AdtssSEUh`，只启用该连接器、只给自然语言指令（启动 `python3 -i`、计算 `6*7`、读取、终止），不提供任何 Tool 用法。该 Agent 仅读 `tools/list` 与参数 schema 后依次调用 `terminal_start(command="python3 -i", mode=pty, until=">>> ", wait_ms=5000)` → `terminal_write(text="6*7", keys=["Enter"], until=…)` → `terminal_read(cursor=143)` → `terminal_kill(signal=TERM, grace_ms=1000)`，输出 `42`，四次调用均无错误码、无重试，`dropped_bytes=0`，最终 `status=signaled`；沙箱为 Linux（Python 3.12.3），约 63 秒完成。

| 验收条件 | 结果 | 证据或缺口 |
|---|---|---|
| SC-001：US1~US7 全部验收场景自动化覆盖并通过 | PASS | 本机与 CI 两平台 240 passed；场景映射见“测试策略与验收映射”及 T03/T04 实现记录（`test_mcp_stdio.py`、`test_lifecycle.py`、`test_policy_audit.py`、`test_restart.py`）。 |
| US8 / SC-002：Manus 中 Agent 仅凭 Tool 描述完成交互流程且无参数错误重试 | PASS | Manus 任务 `Kir4ooJTDyTA9AdtssSEUh` 一次组合成功，0 错误、0 重试。 |
| SC-003：等待中的读取 200 ms 内拿到新输出 | PASS | 本机实测 15.096 ms；CI 两平台严格断言通过。 |
| SC-004：kill / 客户端断开后 2 秒内进程为 0 | PASS | `test_kill_reaps_whole_process_group`、`test_stdio_eof_reaps_all_groups` 等在三处运行均通过。 |
| SC-005：50 MB 输出时 RSS 增长不超过 3 倍缓冲上限 | PASS | 本机 growth 1,720,320 B（上限 3,145,728 B），保留缓冲 ≤1 MiB；CI 两平台断言通过。 |
| 边界情况 1~8 | PASS | 单元与集成测试覆盖，见测试策略映射；全部在全量套件中通过。 |

- **覆盖与缺口**：Linux 证据来自 GitHub Actions ubuntu-latest 与 Manus 云端沙箱实际运行；Manus 实测为单次代表性流程，不代表长期多用户使用。连接器当前固定指向验证提交 `c2d633a`，发布后应改为 `main` 或 `v0.1.0` 标签（发布阶段处理）。未发现实现缺陷或环境阻塞残留；验证中遇到的仅是推送权限问题（gh 令牌缺 `workflow` scope、SSH agent 位于沙箱外），已通过用户加载 SSH 密钥后一次性提权推送解决，不影响被测实现。

## 评审记录（实际）

- **状态**：尚未执行：实现和验证证据具备后进行评审。
- **范围、发现与处理**：`none`。

## 发布与观察记录（实际）

- **产品观察计划引用**：`.sdlc-v1/context/REQ-001.product.md` 的“发布后观察计划”；发布后 1 至 2 周观察错误码分布、`dropped_bytes` 和孤儿进程数，反馈至 `REQ-002`。
- **技术发布准备、实际结果、反馈证据与缺口**：尚未执行：先由主 Agent 在后续阶段创建公共 GitHub 仓库、推送 `main`、打 `v0.1.0` 标签、创建 GitHub Release，并检查不含密钥、审计日志或本机私有路径。
