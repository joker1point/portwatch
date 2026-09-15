# portwatch 交接文档

> 面向对象：**完全没有接触过本项目的新同事**。目标：照着本文能跑起来、能看懂、能接着改。
> 文档口径：所有架构、表结构、接口、命令**均以当前仓库真实代码为准**（撰写时逐文件读过
> `collector.py` / `server.py` / `history.py` / `README.md` / `web/src/**` 并对运行中的服务与
> `history.db` 做过实测）。凡属推断或未验证的内容，本文都会显式标注。
> 已知缺陷一节不做美化，按「现象 / 影响 / 定位线索 / 建议修法」原样列出。

---

## 0. 交接基本信息

| 项 | 内容 |
|---|---|
| 项目根目录 | `<项目根>` |
| 代码规模 | 后端 3 个 Python 文件（`collector.py` 313 行 / `server.py` 452 行 / `history.py` 720 行）+ 前端 14 个 TS/CSS 文件 + 测试 3 个文件（16 个用例） |
| 技术栈 | Python 3.11（psutil + FastAPI + uvicorn + sqlite3）、React 19 + Vite + TypeScript |
| 当前状态 | 可运行。后端已在 `127.0.0.1:8787` 常驻运行（撰写本文时 `/api/health` 返回 `status: ok`、`cycles: 937`），前端 dev server 配置为 `127.0.0.1:5173` |
| 历史库现状 | `history.db` 约 5.6 MB：`port_event` 7154 行（added 3583 / removed 3571 / changed 0）、`port_occupancy` 3388 行（开区间 16 / `observed_removed` 3337 / `reconciled` 35） |
| 交付物 | 本文档 + 仓库本身（含 `README.md`、`tests/` 冒烟测试与 CI 配置；README 侧重"设计说明"，本文侧重"接手操作 + 隐性知识 + 缺陷清单"，两者互补） |

### 阅读顺序建议

1. 想先跑起来 → 第 2 章（快速上手）。
2. 想理解设计 → 第 3 章（目录职责）→ 第 4 章（架构）。
3. 准备动代码 → 第 5 章（工程决策与踩坑，**最值钱的一章**）→ 第 6 章（已知缺陷与未完成项）。
4. 排期/规划 → 第 7 章（后续路线）。

---

## 1. 项目定位与背景

**一句话**：把"本机哪个端口被哪个进程占了"这件事，从 `netstat` 的一行 IP:PORT 变成一条可追溯的
归因链路，并把每次占用的出现/消失**留档成可回溯的历史**。

### 1.1 解决什么问题

Windows 上排查"端口被占用"的常规路径是 `netstat -ano` 拿到 PID，再去任务管理器/`tasklist` 反查进程名。
这条路断在两个地方：

- **链路断**：只能看到 PID，看不到这个进程的完整命令行、可执行文件路径、工作目录、启动时间。
  而"到底是我 3 天前起的那个测试服务，还是某个后台常驻进程"往往只能靠 `cmdline` / `cwd` 才能判断。
- **时间断**：`netstat` 只有"此刻"。端口什么时候被占上的、什么时候悄悄消失的、某进程是否规律性
  反复占用同一端口——全部没有记录，只能靠人守在那儿看。

### 1.2 做成了什么

- **归因链路**：`端口 → 协议 → PID → 进程名 → exe 路径 → 启动命令行 → 工作目录 → 进程启动时间`，
  并给出**归因完整度**（`access`：`full` / `partial` / `denied` / `gone`），权限不足时如实降级而不是报错。
- **实时可视化**：单页仪表盘，2 秒一轮的增量推送（SSE），端口新增有进入动画、属性变化有闪烁、
  右侧一条"变化时间线"记录 `added / removed / changed`。
- **历史留档**：只追加的事件表（真相源）+ 由事件物化出来的占用区间表，支持"某端口在某个时间段
  被谁占着"这类回溯查询（P1 待补查询 API 与视图，见第 7 章）。
- **可处置性判定**：明确区分"能处置"与"系统保留"，对内核进程/归因受限条目在 UI 上打保护标记，
  避免用户误杀系统服务。

### 1.3 项目性质

这是作者的**面试作品集项目**，因此除了功能，代码表达的工程取向同样重要，接手后建议保留这些取向：

- **诚实优先**：时间语义不精确就写进字段名（`first_seen_at` / `occurred_at`）和文档，不假装精确（见 5.4）。
- **分层纪律**：事件表只能被采集层的真实观测追加，心跳/对账这类"推断"绝不污染事件表（见 5.5）。
- **故障隔离**：历史层是增强能力，任何 DB 异常都不允许影响实时 SSE 主功能（见 5.6）。
- **安全边界**：服务具备处置本机进程的能力，因此只监听 `127.0.0.1`、CORS 只放行 localhost（见 5.8）。

---

## 2. 快速上手

### 2.1 环境与版本

| 组件 | 撰写本文时的实机版本 | 说明 |
|---|---|---|
| 操作系统 | Windows 11，Build 26200（`platform.platform()` 回报 `Windows-10-10.0.26200-SP0`） | 采集层的性能优化（`ppid_map`）是 Windows 专属 |
| Python | 3.11.8（实机常驻服务由 MarvisAgent 内置 runtime 启动） | 3.10+ 即可（代码用了 `X | None` 语法与 `from __future__ import annotations`） |
| Node / npm | node v25.1.0 / npm 11.6.2 | 仅前端需要 |
| 后端依赖 | `psutil>=5.9`、`fastapi>=0.115`、`uvicorn>=0.30` | 见 `requirements.txt` |
| 前端依赖 | React 19、react-dom 19、Vite 8、TypeScript 7、@vitejs/plugin-react | 见 `web/package.json` |

**权限**：采集层能拿多少字段取决于进程权限。以管理员身份运行才能拿到大部分进程的 `cmdline` / `cwd`；
非管理员时 `access=partial` 的条目会明显变多，`/api/meta` 的 `is_admin` 会如实回报。

### 2.2 依赖安装

```powershell
cd <项目根>
pip install -r requirements.txt

cd web
npm install
```

> 历史坑（开源发布前已清理）：本仓库文件的早期版本里，`requirements.txt` 末行带有一句生成平台
> 追加的中文标注，它不是合法 requirement，会让 `pip install -r` 直接抛 InvalidRequirement 而失败；
> 同类标注也曾出现在 `README.md` 与本文顶部。当前仓库已清理干净——若你从别处拿到带标注的副本，
> 删掉该行即可（详见 6.4、6.5）。

### 2.3 启动

**方式 A：分别启动 API 与前端（开发态，推荐）**

```powershell
# 终端 1：API 层（同时自动装配历史层）
cd <项目根>
python server.py                 # → http://127.0.0.1:8787
# 等价写法：uvicorn server:app --host 127.0.0.1 --port 8787

# 终端 2：前端
cd <项目根>\web
npm run dev                      # → http://127.0.0.1:5173
```

- 前端默认连 `http://127.0.0.1:8787`，可在 `web/.env.local` 里用 `VITE_API_BASE` 覆盖
  （见 `web/src/api.ts` 的 `API_BASE`）。
- `npm run build` = `tsc --noEmit && vite build`，产物在 `web/dist`；`npm run preview` 走 4173 端口。
- 两个端口都是 `127.0.0.1`，**不要改成 `0.0.0.0`**（原因见 5.8）。

**方式 B：只跑采集层（不启服务、不写历史库）**

```powershell
cd <项目根>
python collector.py                      # 采集一次，打印控制台摘要表
python collector.py --json ports.json    # 同时把快照写成 JSON（schema: portwatch/v1）
python collector.py --raw                # 关闭 IPv4/IPv6 双栈合并（key 里带 IP）
python collector.py --tcp-only           # 只采 TCP 监听，不含 UDP
```

采集层单跑是**排查归因问题的第一手段**：它不经服务、不经 SSE、不落库，输出即真实采集结果。

### 2.4 历史库位置与用法

```powershell
# 默认库：<项目根>\history.db（连同 history.db-wal / history.db-shm，WAL 模式）
# 覆盖方式：环境变量 PORTWATCH_DB 指向别的文件
$env:PORTWATCH_DB = "D:\tmp\portwatch-test.db"
python server.py
```

**要点**：`PORTWATCH_DB` 在 `history.py` **导入时**读取（`DEFAULT_DB_PATH` 是模块级常量），
所以必须在启动进程前设置，进程内改环境变量无效。

```powershell
# 排障：打印库内事件与区间摘要（history.py 自带的唯一 CLI 入口）
cd <项目根>
python history.py --dump
python history.py --db D:\tmp\portwatch-test.db --dump    # 指定库
```

`--dump` 输出两部分：`port_event` 全量事件行（含 `inv_key`）、`port_occupancy` 全量区间行
（`first= / last= / ended=(reason)` 或"仍在占用"）。区间是否健康，看 `last_seen_at` 有没有在推进即可。

### 2.5 冒烟自检（接手第一天请全跑一遍）

```powershell
# 1) 服务活着、采集循环在跑、历史层在落库
curl http://127.0.0.1:8787/api/health
#    期望：status=ok；cycles 持续增长；subscribers 随页面数变化；
#          history.errors=0、history.heartbeats 与 cycles 大致同步、last_heartbeat_keys>0

# 2) 宿主与轮询参数
curl http://127.0.0.1:8787/api/meta
#    期望：schema=portwatch/v1、interval=2.0、total>0、is_admin 反映当前权限

# 3) 快照 + 过滤
curl "http://127.0.0.1:8787/api/ports?proto=tcp&q=8787"
curl "http://127.0.0.1:8787/api/ports?access=partial"

# 4) SSE（首帧 snapshot，之后 delta/心跳；Ctrl+C 退出）
curl -N http://127.0.0.1:8787/api/stream

# 5) 自动化冒烟：三层契约（16 个用例，不需要管理员权限；受限时采集层用例如实 skip）
pip install -r requirements-dev.txt
python -m pytest tests -q
```

**端到端验证历史层**（强烈建议每改一次历史层就跑，做法见附录 C）：起一个独立的监听进程占端口 →
看 `port_event` 出现 `added`、`port_occupancy` 出现开区间 → 关掉监听 → 看 `removed` 与闭合区间 →
强杀 portwatch 服务再重启 → 看对账把残留开区间标 `reconciled`。

> **单测与端到端的分工**（两者不能互相替代，改动采集层 / 历史层时都要跑）：
> `tests/` 钉**契约与不变量**——快照结构、diff 语义、`last_seen_at` 必须被心跳推进、
> `port_event` 只由真实观测追加、心跳与对账不凭空造区间；跑得快、可在 CI 上跨平台回归。
> 附录 C 的链路则证明**真实链路真的通**：真进程、真端口、真落库、真对账。

### 2.6 常见问题速查

| 现象 | 定位 |
|---|---|
| 页面一直"等待首帧快照" | API 未启动 / 不在 8787 / 被防火墙拦；先 `curl /api/health` |
| `status: warming_up` | 首轮采集未完成（后端最多等 100×0.1s=10s 才发首帧），稍等或看日志 |
| `last_error` 非空 | 采集异常信息（如 `net_connections` 被拒绝 → 需管理员） |
| 大量条目显示"受限" | 未以管理员运行，`exe/cmdline/cwd` 取不到属预期 |
| `/api/health` 的 `history.errors` 持续增长 | 历史库写入失败，看服务日志的 `portwatch.history` 警告行 |
| `open_intervals` 远小于 `active_intervals` | **正常**，口径差异，见 6.7 |

---

## 3. 目录结构与各文件职责

```
portwatch/
├─ collector.py            采集层：psutil 拉本机监听端口，沿 PID 归因到进程全字段（313 行）
├─ server.py                API 层：FastAPI + 单例 Scanner + SSE 广播 + 历史层装配（452 行）
├─ history.py              历史层：SQLite 事件表/区间表、批量落库、心跳、对账（720 行）
├─ requirements.txt         后端依赖（注意末行非注释标注，见 6.4）
├─ README.md                项目说明：架构速览、运行方式、历史层设计、时间语义、已知限制（98 行）
├─ .gitignore               已覆盖 history.db* / *.db-wal / ports.json / __pycache__ / node_modules 等（50 行）
├─ LICENSE                  MIT
├─ conftest.py              把仓库根插进 sys.path，让测试能直接 import 三个模块
├─ requirements-dev.txt     测试依赖（-r requirements.txt + pytest）
├─ tests/                   冒烟测试：采集层契约 / API 层 diff 语义 / 历史层三条设计纪律（跑法见 2.5）
├─ tools/listener.py        验证用小工具：起一个真实监听进程，复现「新增 → 消失」全链路
├─ docs/                    README 用的真实运行截图与动图（本机用户名已脱敏）
├─ .github/workflows/tests.yml  CI：ubuntu + windows × py3.11 / 3.13 跑 pytest
├─ ports.json               采集层 `--json` 单跑产物（历史快照，117 KB，已被 .gitignore 忽略）
├─ history.db (+ -wal/-shm) 历史库（运行时生成，WAL 模式；已被 .gitignore 忽略）
├─ __pycache__/             Python 字节码缓存（已忽略）
└─ web/                     前端（React 19 + Vite + TS）
   ├─ index.html            入口 HTML（内联 SVG favicon、title「portwatch · 端口占用」）
   ├─ package.json          dev / build / preview 三个脚本
   ├─ vite.config.ts        host 127.0.0.1、port 5173、build.outDir=dist、sourcemap=true
   ├─ tsconfig.json         strict + noUnusedLocals/Parameters + noEmit（构建时用 tsc 只做类型检查）
   ├─ README.md             前端说明：运行方式、目录、设计取向（工业仪器面板风格）
   ├─ dist/                 构建产物（存在）
   ├─ node_modules/         依赖（存在）
   └─ src/
      ├─ main.tsx           挂载 React root，加载 tokens.css + app.css
      ├─ App.tsx            页面骨架：Header + FilterBar + PortList + Timeline，连接中断横幅
      ├─ types.ts           与 API 层对齐的类型（PortEntry / Meta / TimelineEvent / NormalizedDelta）
      ├─ api.ts             API_BASE / STREAM_URL、fetchMeta、fetchPorts、normalizeDelta 容错归一
      ├─ filters.ts         前端过滤与排序：rowStateOf（ok/limited/protected）、applyFilters、sortEntries
      ├─ hooks/usePortStream.ts  SSE 连接 + useReducer 状态机（核心：snapshot 灌全量、delta 局部更新）
      ├─ components/Header.tsx    品牌、SSE 状态脉冲、统计读数（监听条目/可处置/归因受限/轮询间隔）
      ├─ components/FilterBar.tsx 搜索框 + 协议段选择 + 可处置性段选择 + 命中计数
      ├─ components/PortList.tsx  列表与行渲染（端口/协议/进程+PID/命令行/状态徽标，进入与闪烁动画）
      ├─ components/Timeline.tsx  右侧变化时间线（最多 200 条）
      ├─ components/EmptyState.tsx 空态（区分"无数据"与"被过滤空"两种情况）
      └─ styles/tokens.css, styles/app.css  设计令牌（OKLCH 色彩/间距/动效）与组件样式
```

**职责边界（改代码前务必确认改动落在哪一层）**：

- 采集层**不关心** diff、不关心历史、不写库，只负责"给我一份当前快照"。
- API 层负责 diff、广播、过滤、可处置性标注；**不直接写库**，只把 delta 交给历史层入口。
- 历史层负责持久化与物化区间；**不参与**对外接口契约（除 `/api/health` 里的 `history` 统计段）。
- 前端只消费 SSE 与 REST，不做业务推断；`normalizeDelta` 的容错是为了"接口演进不炸前端"。

---

## 4. 架构说明

### 4.0 四层全景与数据流

```
┌──────────────────────────── 采集层 collector.py ────────────────────────────┐
│ psutil.net_connections(kind="inet")                                          │
│   → TCP 只留 CONN_LISTEN / UDP 全收 → 按 (proto,port,pid) 分桶（双栈合并）    │
│   → build_ppid_map() 一次拿全量 ppid 映射                                     │
│   → collect_process(pid) 按 PID 缓存，逐字段降级                              │
│ 输出：{schema: portwatch/v1, collected_at, host, platform, is_admin, stats, entries[]}
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ run_in_threadpool（阻塞调用不占事件循环）
┌──────────────────────────────────▼──────────── API 层 server.py ─────────────┐
│ Scanner 单例，每 2.0s tick 一次：                                             │
│   curr = {entry_key: entry}   entry_key = "proto:port:pid"                    │
│   delta = compute_delta(prev, curr)  → added / removed / changed              │
│     ├─ 有内容 → _broadcast("delta")  → 每个订阅者的 asyncio.Queue             │
│     ├─ 有内容 → event_sink(delta)    → 历史层 enqueue（同步非阻塞，不 await） │
│     └─ 每轮（不论有无 delta）→ heartbeat_sink(latest) → 历史层心跳            │
│ 接口：/api/health、/api/meta、/api/ports、/api/stream(SSE)                    │
└───────────────┬───────────────────────────────────────────┬──────────────────┘
                │ SSE: snapshot → delta* → heartbeat*        │ 历史层入口
┌───────────────▼──────────── 前端 web/src ──────────────┐  │
│ usePortStream: EventSource + useReducer                  │  │
│   snapshot → 重建 entries Map（全量）                    │  │
│   delta    → 局部 set/delete + 推入 Timeline             │  │
│   heartbeat→ 标记连接存活                                 │  │
│ App → Header / FilterBar / PortList / Timeline           │  │
└──────────────────────────────────────────────────────────┘  │
┌─────────────────────────────────────────────────────────────▼──────────────┐
│ 历史层 history.py（与实时链路完全解耦）                                     │
│   asyncio.Queue ──独立写入协程──→ sqlite3（asyncio.to_thread）              │
│     攒批：20 条 或 1 秒 或 遇到心跳消息（立即 flush）                        │
│   port_event（append-only 真相源）  +  port_occupancy（区间物化）           │
│   对账：启动 1 次 + 每 300s，闭合并标 end_reason='reconciled'                │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 采集层（`collector.py`）

**归因链路**：`端口 → PID → 进程名 → exe → cmdline → cwd → 启动时间`，任一字段拿不到就标
`access=partial`，**绝不抛异常**（`_safe()` 统一吞掉 `AccessDenied / NoSuchProcess / ZombieProcess / OSError`）。

关键实现点：

1. **一次抓全 + 本地分桶**：`psutil.net_connections(kind="inet")` 取所有 inet 连接，然后
   - TCP：只保留 `conn.status == CONN_LISTEN`（`ESTABLISHED` / `TIME_WAIT` 会造成噪音且不符合"占用端口"语义）；
   - UDP：无连接状态，全收（可用 `--tcp-only` 关掉）；
   - `conn.pid is None`（无权限）的条目直接丢弃并计入 `stats.skipped_no_pid`。
2. **双栈合并**：默认 key 为 `(proto, port, pid)`，把同一进程同一端口的 IPv4/IPv6 记录合并成一条，
   地址收进 `listen_addrs`（排序后输出）。`--raw` 时 key 追加 `conn.laddr.ip`，不做合并。
3. **进程信息按 PID 缓存**：`proc_cache[pid]`，同一进程占多个端口只查一次
   （每次 `exe()` / `cwd()` 都是系统调用，能省则省）。
4. **`build_ppid_map()`**：一次拿到全量 `{pid: ppid}`，替代"逐进程调 `Process.ppid()`"——
   这是本项目最大的性能陷阱，详见 5.1。
5. **端口语义提示**：`PORT_HINTS` 硬编码了约 24 个常见端口（3306 MySQL、6379 Redis、9200 ES…），
   只做展示增强，命中不到就是 `null`，不影响任何逻辑。
6. **输出结构**（`schema = portwatch/v1`）：

```json
{
  "schema": "portwatch/v1",
  "collected_at": "2026-09-13T16:52:13",
  "host": "…", "platform": "Windows-10-10.0.26200-SP0", "is_admin": true,
  "stats": {"total": 167, "tcp": 103, "udp": 64,
            "access_full": 158, "access_partial": 9, "skipped_no_pid": 0},
  "entries": [
    {"port": 8787, "proto": "tcp", "hint": null, "listen_addrs": ["127.0.0.1"],
     "pid": 73684, "process": "python.exe", "exe": "…\\python.exe",
     "cmdline": "… server.py", "cwd": "…\\portwatch", "parent_pid": 45128,
     "started_at": "2026-09-13T13:26:08", "access": "full"}
  ]
}
```

> 注意：采集层输出的条目**没有** `key` / `protected` 字段——这两个由 API 层的 `annotate()` 补齐，
> 历史层读到的是补齐后的结构。

### 4.2 API 层（`server.py`）

**Scanner 单例**（`scanner = Scanner()`，模块级）。多客户端共享同一份采集结果，页面数量再多也只有
一条采集链路（这是把采集放在服务端而不是前端各拉一次的根本原因）。

- `interval = 2.0s`；`tick()` 用 `asyncio.Lock` 串行化，采集本体走
  `run_in_threadpool(collector.collect)`，**不阻塞事件循环**。
- **首轮不产 delta**（`first_round = self.latest is None`），避免把开机那一刻的全部端口当成"新增"刷屏；
  历史层因此只能用 `added` 事件开区间，也就产生了"不回溯启动前占用"的口径问题（6.7）。
- **diff 语义**：
  - 身份键 `entry_key(entry) = f"{proto}:{port}:{pid}"`；
  - `added` / `removed` 为集合差；`changed` 只在 `WATCH_FIELDS = (process, exe, cmdline, cwd, parent_pid, access)`
    任一变化时产生（**不含** `listen_addrs`，所以监听地址变化不会产生 `changed`）；
  - `removed` 条目经 `summarize()` 只回传身份信息 `{key, port, proto, pid, process}`（消失的进程无需完整链路）；
  - 三个数组各自按 `port` 升序，便于前端稳定渲染。
- **可处置性标注** `annotate()`：
  - `protected = (pid in {0, 4}) or (access != "full")`，即 **PID 0/4（System Idle / System）或任何归因受限条目都视为不可处置**；
  - 原因字符串进 `protected_reason`（`"系统内核进程"` / `"归因信息受限"`）。
  - 这是**保守策略**：宁可少给一个按钮，也不让用户误杀系统服务。副作用是 `svchost` / `lsass` 这类
    因权限拿不到 `cmdline` 的进程走的是"归因受限"分支，而不是硬编码白名单（见 5.3）。
- **订阅与广播**：每个 SSE 连接一个 `asyncio.Queue(maxsize=64)`；`put_nowait` 失败即
  `discard` 掉该订阅者（消费不过来的连接宁可断，也不拖慢采集循环）。
- **lifespan 装配顺序**（很重要，改动时别打乱）：

```
history_writer.start()            # 建库/建表，启动写入协程
scanner.event_sink = history_writer.enqueue      # delta 落库入口
scanner.heartbeat_sink = history_writer.heartbeat # 心跳入口（独立，因无 delta 也要刷）
scanner.start()                   # 开始轮询
create_task(_reconcile_loop())    # 启动对账一次 + 每 300s 增量对账
… yield …
reconcile_task.cancel() → 清空两个 sink → scanner.stop()（先停生产）→ history_writer.stop()（再 flush 队列）
```

**HTTP 接口**：

| 方法 | 路径 | 参数 | 返回要点 |
|---|---|---|---|
| GET | `/api/health` | — | `status`(`ok`/`warming_up`)、`cycles`、`subscribers`、`last_error`、`history`(落库统计)、`ts` |
| GET | `/api/meta` | — | `schema`、`interval`(秒)、`host`、`platform`、`is_admin`、`collected_at`、`stats`、`subscribers` |
| GET | `/api/ports` | `proto`(all/tcp/udp)、`q`(关键词)、`access`(all/full/partial)、`limit`(0=不截断) | `total` / `matched` / `entries[]`（已 `annotate`）；首轮未完成时返回 `warming_up: true` |
| GET | `/api/stream` | — | SSE：首帧 `snapshot`，之后 `delta`；15s 空闲发传输层 `heartbeat` |

- `/api/ports` 的关键词检索覆盖 `port / process / cmdline / exe / cwd / hint`（`filter_entries()`）；
  前端 `filters.ts` 的本地检索还额外覆盖 `pid / proto / protected_reason / listen_addrs`——两边能力不同，属预期。
- **CORS**：`allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"`，任意本机端口的前端都能联调，
  外网来源一律拒绝。
- **SSE 帧格式**：`sse(event, payload)` 固定为 `event: <type>\ndata: <单行 JSON>\n\n`
  （刻意压成单行，避免多行 data 的解析歧义）。
- **两条心跳不是一个东西，务必区分**：
  - SSE **传输层心跳**：`/api/stream` 里 15s 没消息就发 `event: heartbeat`，前端收到即标记"连接存活"；
  - 历史层 **`last_seen_at` 心跳**：每轮采集结束刷新区间表字段，与 SSE 无关（见 4.4）。
  两者同名不同义，是阅读代码时最容易混的点（已列入 6.10 的重命名建议）。

### 4.3 前端（`web/src`）

**状态管理**：单 `useReducer`（无 Redux/Zustand），state 结构：

```ts
{ entries: Map<string, PortEntry>   // key = entry_key
  events: TimelineEvent[]           // 最多 200 条（MAX_EVENTS），新的在前
  meta, connected, everConnected, beat, lastSyncAt,
  enteredKeys: Set<string>, flashedKeys: Set<string> }   // 动画用，900ms 后清空
```

**SSE 消费**（`hooks/usePortStream.ts`）：

- 三个监听器：`snapshot`（灌全量，重建 Map）、`delta`（局部增删改 + 推时间线）、
  `heartbeat`（置 `connected = true`）；
- `es.onerror` → `connected = false`，页面顶部出横幅提示"连接已中断，EventSource 正在自动重连…"
  （首屏延迟 2s 才显示横幅，避免刚打开就闪一条告警）；
- **重连策略**：`nonce` 变化即重建整个 EventSource（EventSource 自带断线重连，但"陈旧的增量序列"
  只能靠整体重来）；触发条件是 `visibilitychange` 回到可见且距上次同步超过
  `STALE_RECONNECT_MS = 30s`；
- `normalizeDelta()` 对 delta 载荷做容错归一：兼容 `{added,removed,changed}` 数组形态、
  `removed` 为字符串或对象、以及单条形态 `{kind, entry}`——目的是接口演进时不炸前端。

**渲染与交互**：

- `App.tsx`：`sortEntries`（端口升序，同端口 tcp 在前）→ `applyFilters`（协议 / 可处置性 / 关键词）→ 列表；
  统计口径 `actionable`（`rowStateOf==='ok'`）与 `limited`；
- `rowStateOf()`：`protected` → `'protected'`；`access !== 'full'` → `'limited'`；否则 `'ok'`。
  三种状态在 UI 上是"正常 / 受限 / 保护"徽标；
- 行渲染含进入动画（`row--enter`）与变更闪烁（`row--flash`），高亮 900ms 后由
  `clearHighlights` 清掉，避免 DOM 上残留 class；
- `Header` 的脉冲点用 `key={beat}` 触发元素重建来重播动画——**"2 秒轮询的心跳"是靠这个被感知的**；
- 设计取向（`web/README.md`）：工业仪器面板，暖调深灰 + 琥珀强调色；端口号等宽 + `tabular-nums` 对齐；
  动效只用 `transform` / `opacity` 且遵循 `prefers-reduced-motion`。

### 4.4 历史层（`history.py`）

**目标**：补齐"实时只能看到此刻"的短板，且**不改变任何对外契约**（SSE 帧结构、字段、`/api/ports` 返回都保持原样）。

**两张表，两种角色**：

| 表 | 角色 | 关键列 |
|---|---|---|
| `port_event` | **append-only 事件真相源**（唯一信任锚）：`added` / `removed` / `changed` | `id, entry_key, inv_key, occurred_at, event_type, proto, port, pid, process_name, exe, cmdline, cwd, create_time, local_addrs, detail(JSON 兜底字段)` |
| `port_occupancy` | **区间物化表**：一次占用从 `first_seen_at` 到 `ended_at` | `inv_key(PK), proto, port, pid, process_name, exe, cmdline, cwd, create_time, local_addrs, first_seen_at, last_seen_at, ended_at, end_reason` |

- **占用实例键** `inv_key = proto:port:pid:create_time`：比 `entry_key` 多带进程创建时间，
  用于在**存储侧**区分"同一个 PID 被复用"的前后两个进程实例；
  `create_time` 取 `entry.started_at`（缺失时为空串，见 6.2 的已知限制）。
- **`end_reason` 只有两个值**：`observed_removed`（采集层真实观测到条目消失）与
  `reconciled`（对账时发现快照里已不存在，属"推断闭合"）。
- **索引**：`port_event(entry_key, occurred_at)`、`port_event(inv_key, occurred_at)`、
  `port_event(port, occurred_at)`、`port_occupancy(port, first_seen_at)`、
  `port_occupancy(pid, first_seen_at)`、以及 `port_occupancy(ended_at) WHERE ended_at IS NULL` 的**部分索引**
  （对账/统计"开区间"走它，成本可忽略）。
- **连接策略**：`sqlite3.connect(..., check_same_thread=False, timeout=10)` +
  `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`；**所有** sqlite3 调用都在
  `asyncio.to_thread` 里执行。

**写入通路（性能与稳定性设计的核心）**：

```
Scanner.tick() ──enqueue(delta)──┐  同步非阻塞 put_nowait，绝不 await，绝不抛异常
Scanner.tick() ──heartbeat(snap)─┤
                                 ▼
                        asyncio.Queue（无界的普通 Queue）
                                 ▼
                    独立写入协程 _run → _collect_batch → _flush
                    攒批条件：满 20 条 / 距首条满 1s / 批内出现心跳消息（立即 flush）
                                 ▼
                    asyncio.to_thread(_write_batch_sync)
                      · 每条 delta 一个独立事务（单条坏数据不连累整批）
                      · 任何 DB 异常只 logger.warning + errors 计数，绝不冒泡
```

- **心跳消息同时充当"立即刷"信号**：保证 `last_seen_at` 尽量贴近观测时刻——服务被强杀时
  最多丢当前这一轮，不会连续滞后多轮。
- **落库语义**（`_apply_delta`）：
  - `added` → INSERT 事件 + `_OPEN_OCCUPANCY`（开区间，`first_seen_at = last_seen_at = ts`），
    并把 `entry_key → inv_key` 记入内存 `_active`；`inv_key` 缺失（缺 proto/port/pid）时事件仍留档、只是不开区间；
  - `changed` → INSERT 事件 + `_TOUCH_OCCUPANCY`（只刷新 `local_addrs`，**不新建也不切换区间**）；
  - `removed` → INSERT 事件 + `_CLOSE_OCCUPANCY`（`ended_at=ts, end_reason='observed_removed'`）；
    `removed` 载荷不带 `started_at`，所以 `inv_key` 靠内存 `_active.pop(entry_key)` 回推，
    未命中时退回"按 `proto:port:pid` 查最新开区间"（进程重启/事件丢失时的兜底）。
- **心跳**（`heartbeat()` + `_apply_heartbeat_sync`）：入参是当前快照，时间戳取
  `snapshot["collected_at"]`（**观测时刻**，不是落库时刻——队列积压也不会让字段漂移）；
  投递 `(ts, [(entry_key, inv_key), ...])`，写入线程 `executemany`
  `UPDATE port_occupancy SET last_seen_at=? WHERE inv_key=? AND ended_at IS NULL`；
  同时**用本轮真实快照重建内存活跃索引 `_active`**（只在写入线程内改动，无跨线程共享），
  并顺带统计库内开区间数 `_open_intervals`。库里没有对应区间时 UPDATE 自然空转，不会凭空造区间。
- **对账**（`reconcile()`）：启动后等待首轮快照（最长 10s）执行一次，之后每
  `RECONCILE_INTERVAL = 300s` 一次。逻辑：取库内所有 `ended_at IS NULL` 的区间，
  与当前快照的 `inv_key` 集合比对——命中者只刷 `last_seen_at`（区间不闭合），
  未命中者闭合并标 `end_reason='reconciled'`。**对账只修复区间表，不写事件表**（它不是真实观测）。
- **健康指标口径**：`/api/health` 的 `history` 段同时给出
  `active_intervals`（内存活跃索引 = 当前快照中仍在占用的实例数）与
  `open_intervals`（库内真值 = `ended_at IS NULL` 行数）。两者口径不同，
  正常情况 `open_intervals ≤ active_intervals`，差额来源见 6.7。

### 4.5 时间语义（诚实声明，务必传达）

采样是**离散**的（默认 2 秒一轮），一次 diff 只能把变化定位在

```
(上一轮采样时刻, 本轮采样时刻]
```

这个半开区间内。因此 `occurred_at` / `first_seen_at` / `last_seen_at` 记录的是**观测到该变化的采样时刻**，
不是事件真实发生的时刻：

- 存在最多一个采样间隔（默认 2 秒）的不确定性；
- 若两个变化落在同一采样区间内先后发生，**它们的先后顺序会丢失**；
- 对仍未闭合的区间，`last_seen_at` 的含义是"截至最后一轮采样，该占用仍在监听"的最近观测时刻；
  服务停机期间无人采样，该值自然停在停机前最后一轮。

字段名刻意用 `first_seen_at` / `occurred_at` 而非 `start_time` / `event_time`，就是为了不误导使用者。
**任何基于这张表算"占用时长"的代码，都必须把 2 秒的采样不确定性带到结论里。**

---

## 5. 关键工程决策与踩坑（隐性知识，代码里读不出来）

> 本章是给接手人省时间用的：这些结论都来自实际踩过，读代码只能看到"结果写法"，看不到"为什么"。

### 5.1 Windows 上 `psutil.Process.ppid()` 是性能陷阱 → 改用私有 API 一次快照

- **坑**：Windows 上 `psutil.Process(pid).ppid()` **每次调用都会重建一次全量进程快照**（单次约 45ms）。
  采集器需要对上百个 PID 取父进程，逐进程调用 110 次累计约 **5.4 秒**，
  整轮采集实测从"毫秒级"恶化到 **4476ms**——2 秒轮询周期完全被吃穿。
- **解法**（`build_ppid_map()`）：优先调用 `psutil._pswindows.ppid_map()` **一次**拿到全量 `{pid: ppid}`，
  整轮采集实测降到 **73ms**；拿不到时退回 `psutil.process_iter(["pid","ppid"])`（较慢但可用）。
- **注意**：`_pswindows` 是**私有 API**，跨版本可能被移除，且**非 Windows 平台不存在**。
  因此必须保留 `try/except` 兜底分支（代码里已保留），**升级 psutil 后请重新回归这条路径**。
- 相关细节：进程信息（`name/exe/cwd/cmdline`）按 PID 缓存，避免同一进程多端口时重复查询。

### 5.2 不要用 `Start-Process -RedirectStandardOutput` 启动常驻服务

- **坑**：用 `Start-Process python server.py -RedirectStandardOutput xxx.log` 拉起常驻服务后，
  **输出句柄不会释放**，调用方会一直等输出流结束（EOF），命令执行挂满超时，看起来像"启动失败"。
- **解法**：启动常驻服务时不要重定向输出句柄——用 `-WindowStyle Hidden` 让进程独立跑，
  或让服务自己把日志写文件；需要看日志时直接去读文件。
- 本项目的服务最终就是以"**脱离（DETACHED）**"方式启动的，避免句柄泄漏导致后续命令全部卡住。
  这条经验同样适用于"用脚本拉起别的常驻进程"的所有场景。

### 5.3 Windows 内核进程取不到 `cmdline` / `cwd`，不要反复重试提权

- **现象**：`System`（PID 4）、`svchost`、`lsass`、`wininit`、`services` 等进程，
  **即便以管理员身份运行**，`exe` / `cmdline` / `cwd` 也可能为空（`psutil.AccessDenied`）。
- **本质**：这是内核/会话隔离的安全边界，不是 bug，**提权也拿不到**。不要在这上面浪费轮次
  "换个方式再读一次"。
- **设计回应**：
  - 采集层逐字段降级，字段缺失只把条目打成 `access=partial`，绝不抛异常、绝不阻断整轮采集；
  - API 层 `annotate()` 采用**保守策略**：`pid in {0,4}` 或 `access != "full"` 一律
    `protected=true`，UI 上显示"保护"徽标并禁止处置；
  - 于是 `svchost` / `lsass` 之所以被保护，走的是"归因受限"这条通用路径，
  **不是**硬编码进程名单——新增保护对象时优先考虑是否已由 `access` 自动覆盖。

### 5.4 时间语义必须诚实：观测时刻 ≠ 事件时刻

- 决策：字段命名用 `first_seen_at` / `occurred_at` / `last_seen_at`，而不是 `start_time` / `event_time`；
  README 与本文都显式声明"最多一个采样间隔的不确定性"。**宁可在文档里承认不精确，也不要让下游
  误以为这是精确时间戳。**
- 具体表现：同轮采样内的先后顺序会丢失；跨采样的事件时序只能保证在 2 秒粒度内。
- 交接提醒：`last_seen_at - first_seen_at` **不是**精确占用时长（见 6.0 的背景：这正是心跳缺陷
  造成"时长虚增"的那条链路）。

### 5.5 分层纪律：事件表只由真实观测追加，"推断"只能改区间表

- **规则**：`port_event` **只能**由采集层 delta（`added` / `removed` / `changed`）追加；
  心跳刷新、对账闭合这类"不是一次真实观测"的动作，**只允许 UPDATE `port_occupancy`**。
- **原因**：事件表是整个历史层唯一的信任锚。一旦让推断写进事件表，就再也分不清"观测到的"和"猜出来的"，
  后续任何统计都无法自证。
- 这条纪律是**后续所有改动的红线**：新增任何历史层逻辑前，先问"我是观测到了，还是推断的？"

### 5.6 历史层绝不允许影响实时链路

- 投递侧：`enqueue()` / `heartbeat()` 都是**同步非阻塞** `put_nowait`，**绝不 await**、**绝不抛异常**
  （队列未就绪时只 `dropped += 1`）。
- 消费侧：sqlite3 一律走 `asyncio.to_thread`；每条 delta 独立事务；DB 异常只 `logger.warning` + 计数。
- 装配侧：历史层初始化失败（如建库失败）只记日志，服务照常启动——**实时 SSE 是主功能，历史层是增强**。
- 停机侧：**先停采集（停止生产），再 flush 队列**，避免丢最后几轮数据。

### 5.7 为什么是"2 秒轮询 + diff"，而不是监听事件

- 备选方案（Windows ETW / WMI 事件订阅 / 内核回调）复杂度、权限要求与跨平台成本都高，
  且难以保证"不漏、不重"。
- 轮询 + diff 简单、可移植、代价可控（优化后整轮约 73ms），
  代价是引入"采样不确定性"——用 5.4 的方式如实声明，而不是假装精确。
- 因此：`interval` 是产品语义的一部分，缩短它会改变历史层精度与 CPU 开销，
  **不要把 2.0s 当成随手可调的常数**。

### 5.8 只监听 `127.0.0.1`、CORS 只放行 localhost

服务具备对本机进程的处置能力（后续会接"结束进程"这类动作），因此：

- `server.py` 硬编码 `HOST = "127.0.0.1"`；前端 dev server 也绑定 `127.0.0.1`；
- CORS 用正则只放行 `http://localhost:*` / `http://127.0.0.1:*`。
- **不要把 host 改成 `0.0.0.0`**，也不要把 CORS 放开到 `*`。

### 5.9 其它值得保留的小决策

| 决策 | 原因 |
|---|---|
| TCP 只留 `CONN_LISTEN` | `ESTABLISHED`/`TIME_WAIT` 数量巨大且不符合"端口占用"语义 |
| `removed` 只回身份信息 | 进程已消失，完整归因链路无意义，减小 SSE 载荷 |
| 前端不引入状态库 | 单个 `useReducer` + `Map` 足够，减少依赖与心智负担 |
| `normalizeDelta` 做多形态容错 | 采集/接口结构演进时不炸前端 |
| SSE 帧 data 压成单行 JSON | 避免多行 `data:` 的解析歧义 |
| 每个订阅者独立队列且满了就丢弃 | 慢消费者不允许拖慢采集循环 |
| 端口语义提示硬编码在采集层 | 成本低、可读性收益高，且与业务逻辑解耦 |

---

## 6. 已知缺陷与未完成项

> 排序原则：**能影响数据正确性的在前**。
> 每条按「现象 / 影响 / 定位线索 / 建议修法」写；已完成修复的保留在小节 6.0（它是本项目
> 最容易被再次引入的坑）。

### 6.0 【已修复，保留现场】`last_seen_at` 心跳刷新失效

- **现象**：端口被稳定占用、长时间没有 delta 时，区间表的 `last_seen_at` **永远等于 `first_seen_at`**。
- **影响**：进程被强杀后残留的悬空区间，在对账闭合时 `ended_at` 取到的是**服务重启时刻**而非真实消失时刻，
  占用时长会被**虚增**（服务停机多久就虚增多久）。这是数据正确性问题，优先级高。
- **根因**：`last_seen_at` 的刷新挂在 delta 路径（尤其是 `changed`）上，而稳定占用**不产生任何 delta**，
  刷新逻辑根本没有机会执行。
- **实测证据（历史库原始行，未修复前）**：

```
tcp:8801:41552:2026-09-13T13:37:25   first=13:37:27  last=13:37:27  ended=13:37:34  (observed_removed)
            ↑ first == last，中间跨约 3 个采样周期（2s/轮）却未刷新
```

- **修法**（已在当前代码中实现）：
  1. `Scanner.tick()` 在**每轮采集结束后无条件**调用 `self.heartbeat_sink(self.latest)`，
     位置在 delta 判断**之外**，且不得只依赖 `changed`；
  2. `HistoryWriter.heartbeat(snapshot)` 投递 `(collected_at, [(entry_key, inv_key), ...])`，
     写入线程 `executemany` `UPDATE port_occupancy SET last_seen_at=? WHERE inv_key=? AND ended_at IS NULL`；
  3. 心跳**只改区间表、绝不写 `port_event`**（遵守 5.5 的分层纪律）；
  4. 心跳消息在攒批逻辑里充当"立即 flush"信号，使 `last_seen_at` 尽量贴近观测时刻。
- **修复后验证证据（同一库）**：

```
tcp:8814:69452:...   first=15:51:34  last=15:51:55  ended=15:51:57   → 心跳推进 21s，随后正常闭合
tcp:8815:98012:...   first=15:53:06  last=15:53:16  ended=16:19:46 (reconciled)
                     → 强杀服务 40s 后重启，last_seen_at 冻结在消失前的 15:53:16，
                       未被改写成重启时刻 16:19:46（这正是本次修复要保住的性质）
udp:57676:2936:2026-09-10T07:38:58  first=13:27:13  last=16:53:23  ended=16:53:25
                     → 长驻 UDP 占用稳定存在 3.4 小时期间 last_seen_at 持续推进（span≈12370s）
```

- **回归方法**：附录 C 的"独立监听进程法"（场景 a：稳定占用看 `last_seen_at` 是否前进；
  场景 b：强杀服务后重启，看 `last_seen_at` 是否被改写）。
- ⚠️ **不要误判**：`history.db` 里 **13:37 左右的旧行仍是 `first == last`**（那时还没有心跳），
  这是历史遗留数据，不是缺陷复现。判断依据看区间的 `first_seen_at` 是否早于心跳修复的时点。

### 6.1 【中】采集层 delta 身份键缺 `create_time`，PID 复用会漏检

- **现象**：`server.entry_key()` 仍是 `proto:port:pid`。当某个 PID 被系统复用、且新进程恰好占用同一端口时，
  采集层两轮 diff 得到的是同一个 key，**产不出 `removed` + `added`**。
- **影响**：历史层把前后两段不同的占用**并成一个区间**（漏检）；
  该端口的"谁在什么时候占的"会被算错。注意这属于**源头漏检**，存储侧无法补救。
- **定位线索**：`server.py:entry_key()`；`history.py` 顶部 docstring 的"已知限制"；
  `inv_key_of()` 已用 `proto:port:pid:create_time` 在存储侧区分实例——但**由于源头没产 delta，
  新实例的 `added` 根本不会到达**，所以这个区分只在"确实观测到变化"的场景下生效。
- **建议修法**：把采集层身份键升级为 `proto:port:pid:create_time`。
  ⚠️ **注意爆炸半径**：`entry_key` 出现在 `port_event.entry_key`、SSE `delta` 载荷、`/api/ports` 的 `key`、
  前端 `entries` 的 Map 键与高亮集合、以及 `parseKey()`（前端按 `proto:port:pid` 三段 split）。
  改动需要**前后端同步**：前端 `parseKey` 需要容忍多一段尾缀，历史层 `entry_key_of()` 也要同步。
  建议连同一次**全链路回归**（新增/消失/属性变化/时间线/历史库落库）一起提交。

### 6.2 【中】前端未做人工回归实测

- **现象**：`server.py` 接入历史层后被修改（`tick()` 末尾新增心跳投递），
  存储的 delta 与 SSE 广播路径按设计未变；**后端侧**已实测验证 SSE 首帧结构与首条 entry 字段未变，
  但**浏览器端页面**未在本次流程中系统走查一遍（视觉、筛选、动画、断线横幅、时间线）。
- **影响**：前端与后端契约若存在细微不匹配（字段名/形态），只能由用户偶然发现。
- **定位线索**：`web/src/hooks/usePortStream.ts` 的 `normalizeDelta`、`App.tsx` 的 `meta.interval` 读取
  （API 下发的是 `interval`（秒），前端做了 `seconds → ms` 的转换）。
- **建议修法**：接手后先跑一次人工回归清单：打开页面 → 看到首帧与脉冲 →
  起/停一个监听端口 → 看到进入动画、时间线新增/消失 → 搜索与协议/可处置性筛选 → 刷新与断线重连提示。

### 6.3 【低】`port_event` 没有保留期清理，会持续增长

- **现象**：事件表只追加、从不删除。撰写本文时 7154 行 / 库 5.6 MB（约 3.5 小时数据，
  含一次压力测试产生的大量 UDP 短连接）。长期运行会线性增长，`-wal` 文件同步增长。
- **影响**：磁盘占用；后续全表扫描类查询变慢（已有索引可缓解）。
- **定位线索**：`history.py` 中不存在任何 DELETE / 归档逻辑；README「已知限制 3」已声明。
- **建议修法**：P1 引入保留策略，三选一或组合：
  ① 定时 `DELETE FROM port_event WHERE occurred_at < ?`（记得配合 `VACUUM`/`PRAGMA wal_checkpoint`）；
  ② 按天归档到 `port_event_archive` 表；③ 迁到 PostgreSQL 后直接用分区表 drop partition（见第 7 章）。

### 6.4 【已处理】`requirements.txt` 末行的平台标注会让 `pip install -r` 失败

- **现象**（清理前）：文件最后一行是生成平台追加的中文标注「（内容由 AI 生成，仅供参考）」，
  **不是合法 requirement**。
- **影响**：实测 `packaging.requirements.Requirement(...)` 解析该行抛
  `InvalidRequirement: Expected package name at the start of dependency specifier`，
  即 `pip install -r requirements.txt` 会直接失败——**新同事第一步就卡住**。
- **定位线索**：`requirements.txt` 末行（0-based 第 6 行）。
- **处置**：开源发布前已删除该行。若你拿到的是带标注的副本，删掉它或在行首加 `#` 即可（详见 2.2）。

### 6.5 【已处理】README / HANDOVER 顶部的 AIGC front-matter 元数据块

- **现象**（清理前）：`README.md` 与本文顶部各有一段 YAML 形式的生成平台元数据
  （`Label` / `ProduceID` / `ContentProducer` / 两段 `ReservedCode`），随后才是正文标题。
- **影响**：仓库首页 / 编辑器预览里，读者第一眼看到的是一坨平台元数据，不像正式项目文档；
  若用 Markdown 渲染器解析 front-matter，正文会被吞。
- **定位线索**：两个文件的开头。
- **处置**：开源发布前已剥离本文顶部的元数据块；`README.md` 已整体重写（无 front-matter）。

### 6.6 【低·运维】当前常驻服务加载的是旧版 `history.py`

- **现象**：撰写本文时运行中的服务（`python server.py`，PID 73684）来自上一次启动，
  其 `/api/health` 的 `history` 段**没有 `open_intervals` 字段**，而当前代码 `stats()` 已包含该字段。
- **影响**：只是健康指标缺失（口径校验少一个数据点），不影响落库与 SSE 正确性；
  但容易让人误判"代码没生效"。
- **定位线索**：`curl /api/health` 的 `history` 键集合 vs `history.py:HistoryWriter.stats()`。
- **建议修法**：改动 `history.py` / `server.py` 后**重启服务**再验证；
  重启后 `/api/health` 会出现 `open_intervals`，且 `_open_intervals` 由心跳线程顺带统计。

### 6.7 【信息·口径澄清】三条容易被误判为缺陷的正常现象

1. **`open_intervals < active_intervals` 是正常的**：历史层**不回溯服务启动前就已存在的占用**——
   区间只能由"观测到的 `added`"开出，因此服务首次启动时就一直在监听的进程（系统服务、浏览器、
   常驻 UDP 等）**没有区间行**；它们之后退出时事件表会留下 `removed`，
   但没有区间可闭合（实测：`removed` 事件中 32 条 `inv_key` 为 NULL）。
2. **对账"明细条数与统计数不一致"**：历史 P0 报告中 `reconciled` 明细列了 6 条而统计为 8 条，
   原因是**多次对账累计**（每次启动/每 5 分钟一次，逐次打印）。当前库里 `reconciled` 共 35 条。
   属统计口径问题，不是数据错乱。
3. **UDP 短连接会制造大量区间**：UDP 无连接状态、端口随机（如 49153+、63xxx），
   实测一次压力场景就产生上千个短区间。做统计时先按 `proto` 过滤，别把 UDP 噪音算进"真实占用"。

### 6.8 【已闭环·历史遗留】临时脚本与 `.gitignore`

- 上一轮修复任务曾中途被打断，验证用的临时脚本（如 `listener.py`）可能残留在项目根。
- **当前实测状态（已核实）**：项目根只有
  `.gitignore / collector.py / history.py / server.py / README.md / requirements.txt / ports.json /
  history.db(+wal/shm) / __pycache__/ / web/`——**无临时脚本残留**；
  `.gitignore` 已存在并覆盖 `history.db*`、`*.db-wal/-shm`、`ports.json`、`__pycache__/`、`*.py[cod]`、
  `.venv/`、`node_modules/`、`dist/`、`*.log` 及编辑器/系统文件。
- 交接后如需做验证，请把临时脚本写到**本文档同级的临时目录**，不要污染项目根。

### 6.9 【低】SSE 的 `error` 事件前端未监听

- **现象**：`Scanner._run()` 捕获采集异常后会 `_broadcast("error", {"message": ...})`，
  但前端 `usePortStream.ts` 只注册了 `snapshot` / `delta` / `heartbeat` 三个监听器，
  **`error` 事件被静默丢弃**（`EventSource.onerror` 是连接级错误，与此无关）。
- **影响**：采集出错时页面上看不到任何提示，只能通过 `/api/health` 的 `last_error` 发现。
- **定位线索**：`server.py:Scanner._run()` 的 except 分支；`usePortStream.ts` 的 addEventListener 列表。
- **建议修法**：前端加 `es.addEventListener('error', ...)` 把消息接到横幅/时间线；
  或把采集错误也纳入 `/api/meta` 展示。

### 6.10 【低·可读性】两个 `heartbeat` 同名不同义

- **现象**：`/api/stream` 的 SSE **传输层心跳**（15s 空闲发一次，`event: heartbeat`）与
  历史层的 **`last_seen_at` 心跳**（每轮采集后刷新区间字段）在代码与文档里都叫 heartbeat。
- **影响**：新接手人极易混淆，改一处时误伤另一处。
- **定位线索**：`server.py:stream()` 的 `sse("heartbeat", ...)`；`history.py:heartbeat()`。
- **建议修法**：重命名为 `stream_keepalive` / `occupancy_heartbeat`，或至少在注释中固化区分（当前代码注释已部分说明）。

### 6.11 【低】未接线的代码残留

- `web/src/api.ts` 的 `fetchPorts()`：**全仓库无调用点**（实测 grep），是 REST 降级路径的预留
  （`/api/ports` 接口本身是通的、可直接 curl）。
- `usePortStream` 返回的 `reconnect()`：`App.tsx` 未使用（目前只在内部由 `visibilitychange` 触发重建）。
- `types.ts` 的 `Meta` 里 `scan_interval_ms / interval_ms / last_scan_at / first_scan_at / counts`
  等字段 API 层未下发，属早期形态的兼容残留；`TimelineEvent.note` 同理未被写入。
- **建议**：不要无脑删除 `fetchPorts`（它是不依赖 SSE 的降级能力），
  但建议在 README 或注释里标注"预留、未接线"，避免后来者误以为有 bug。

---

## 7. 后续路线建议

### P1（建议优先，收益/成本比最高）

1. **区间查询 API + 前端视图**：`GET /api/history/intervals?port=&proto=&pid=&from=&to=&open_only=`
   直接查 `port_occupancy`（`(port, first_seen_at)` 索引已就绪）。前端补甘特图/热力图视图，
   让"某端口一天里被谁占了几段"一眼可见。这是把历史层价值真正交付给用户的关键一步。
2. **bootstrap 首轮全量记 `added`**：启动首轮把当前快照整体写入 `added`（或直接开区间），
   解决"不回溯启动前占用"（6.7-1）导致 `open_intervals` 长期小于 `active_intervals` 的口径问题。
   ⚠️ 需同时定义语义：bootstrap 产生的区间 `first_seen_at` 只是"服务启动时刻"，
   文档要同步声明，别让它冒充真实开始时刻。
3. **采集层身份键升级为 `proto:port:pid:create_time`**（6.1），含前后端同步回归。
4. **`port_event` 保留期与归档**（6.3）。
5. **前端补 `error` 事件展示**（6.9）+ 清理 `requirements.txt` / `README.md` 平台标注（6.4、6.5）。

### P2（规模上量后的选择）

6. **切换 PostgreSQL 分区表**：`port_event` 按天/月分区（`occurred_at`）、`port_occupancy` 按 `port` 哈希分区，
   配 BRIN 索引；解决 SQLite 单文件在全量留档下的写入与查询瓶颈。
7. **Redis 缓存活跃区间**：把 `_active` 索引与最近区间放到 Redis，支持多实例/多采集端共享。
8. **多机采集**：采集层作为 agent 上报，API 层聚合；届时需重新考虑鉴权与"只监听 127.0.0.1"的边界（5.8）。

### UI/表达层

9. **如实标注采样精度**：在列表/区间视图上显式提示"时间含 ±1 个采样间隔（2s）的观测不确定性"，
   并说明"同轮采样内的先后顺序不可辨"（呼应 5.4）。
10. **时长口径显式化**：任何展示"占用时长"的地方标注是 `last_seen_at - first_seen_at`（观测跨度）
    还是真实时长，避免把观测值当精确值（6.0 的教训）。

---

## 8. 交接验收清单

接手人建议按顺序完成，全部通过即代表已掌握项目：

- [ ] 安装依赖（含处理 `requirements.txt` 末行）与前端 `npm install`
- [ ] `python collector.py` 能打印摘要；`--json` / `--raw` / `--tcp-only` 三个开关行为符合预期
- [ ] `python server.py` 启动，`/api/health` 返回 `status=ok` 且 `cycles` 持续增长
- [ ] `npm run dev` 打开页面，看到首帧列表、脉冲点跳动、时间线初始为空
- [ ] 起一个测试监听端口（见附录 C），页面看到进入动画与时间线 `added`；关掉后看到 `removed`
- [ ] `python history.py --dump` 能看到上述端口的 `added` / `removed` 事件与已闭合区间，且 `last_seen_at` 在占用期间**持续前进**
- [ ] 强杀服务再重启，`/api/health` 与 `--dump` 能看到残留区间被标 `reconciled`
- [ ] `python -m pytest tests -q` 全绿（权限受限环境里采集层用例会如实 skip，不算失败）
- [ ] 读懂第 5 章每一条，能向别人解释"为什么事件表只由 delta 写入"
- [ ] 读完第 6 章，能说清 6.1（PID 复用漏检）的爆炸半径与 6.4（依赖安装坑）的规避方式

---

## 附录 A：接口契约速查

**SSE `/api/stream`**（`Content-Type: text/event-stream`，`Cache-Control: no-cache`，
`X-Accel-Buffering: no`）：

```
event: snapshot                       ← 连接后的第一帧，全量
data: {"schema":"portwatch/v1","collected_at":"...","is_admin":true,"stats":{...},"entries":[...]}

event: delta                          ← 有变化时推送
data: {"ts":"2026-09-13T16:52:13","added":[PortEntry...],"removed":[{key,port,proto,pid,process}...],
       "changed":[PortEntry...],"counts":{"added":1,"removed":0,"changed":0}}

event: heartbeat                      ← 传输层保活（15s 无消息）
data: {"ts":"2026-09-13T16:52:28","cycles":937}

event: error                          ← 采集异常（⚠️ 前端目前未监听，见 6.9）
data: {"message":"..."}
```

`PortEntry` 字段（`annotate()` 之后）：`key, port, proto, hint, listen_addrs[], pid, process, exe,
cmdline, cwd, parent_pid, started_at, access, protected, protected_reason`。

**REST**：`/api/health`、`/api/meta`、`/api/ports`（参数 `proto` / `q` / `access` / `limit`），
字段含义见 4.2 表格。所有接口均为 GET、无鉴权、仅本机可访问。

## 附录 B：表结构摘要（与 `history.py:SCHEMA_SQL` 一致）

```sql
CREATE TABLE port_event (               -- append-only 事件真相源
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_key TEXT NOT NULL,              -- proto:port:pid
  inv_key TEXT,                         -- proto:port:pid:create_time（可能为 NULL）
  occurred_at TEXT NOT NULL,            -- 观测时刻
  event_type TEXT NOT NULL CHECK (event_type IN ('added','removed','changed')),
  proto TEXT, port INTEGER, pid INTEGER,
  process_name TEXT, exe TEXT, cmdline TEXT, cwd TEXT,
  create_time TEXT,                     -- = entry.started_at
  local_addrs TEXT,                     -- JSON 数组
  detail TEXT                           -- 其余字段的 JSON 兜底
);

CREATE TABLE port_occupancy (           -- 区间物化表
  inv_key TEXT PRIMARY KEY,
  proto TEXT NOT NULL, port INTEGER NOT NULL, pid INTEGER NOT NULL,
  process_name TEXT, exe TEXT, cmdline TEXT, cwd TEXT,
  create_time TEXT, local_addrs TEXT,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  ended_at TEXT,                        -- NULL = 仍在占用
  end_reason TEXT                       -- observed_removed | reconciled
);

-- 索引
idx_event_entry_time(entry_key, occurred_at)
idx_event_inv_time(inv_key, occurred_at)
idx_event_port_time(port, occurred_at)
idx_occ_port_first(port, first_seen_at)
idx_occ_pid_first(pid, first_seen_at)
idx_occ_open(ended_at) WHERE ended_at IS NULL     -- 部分索引
```

## 附录 C：端到端验证方法（"独立监听进程法"）

> 与 `tests/` 的分工见 2.5：单测钉契约，本附录钉真实链路。

```python
# temp/listener.py —— 起一个真实监听进程模拟端口占用
import socket, os, sys, time
port = int(sys.argv[1]); ready = sys.argv[2]; stop = sys.argv[3]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen(1)
open(ready, "w").write("ok")          # 通知"已就绪"
while not os.path.exists(stop):        # 轮询 stop 文件
    time.sleep(0.2)
s.close()
```

```powershell
# 1) 启动监听（⚠️ 不要用 -RedirectStandardOutput，见 5.2）
Start-Process python -ArgumentList "temp\listener.py 8899 temp\r.txt temp\s.txt" -WindowStyle Hidden
# 2) 等若干采样周期（≥6s）→ 查事件与区间
python history.py --dump | Select-String 8899
#    期望：port_event 出现 added；port_occupancy 开区间，且 last_seen_at 随时间前进（心跳生效）
# 3) 让监听退出 → 再看：removed 事件 + 区间闭合（end_reason=observed_removed）
New-Item temp\s.txt -ItemType File -Force | Out-Null
# 4) 模拟崩溃：强杀 portwatch 服务本身，等一会儿再重启
Stop-Process -Id <server_pid> -Force      # 真实数据场景请自行确认后再执行
python server.py
#    期望：启动对账把残留开区间标 end_reason='reconciled'，
#          且 last_seen_at 停在消失前的观测时刻（不被改写成重启时刻）
```

验证完请把 `temp/` 下脚本留在临时目录，**不要**散落到项目根（6.8）。

## 附录 D：名词表

| 名词 | 含义 |
|---|---|
| `entry_key` | 采集层/API 层条目身份键：`proto:port:pid`（6.1 的待升级点） |
| `inv_key` | 存储侧占用实例键：`proto:port:pid:create_time`，区分 PID 复用 |
| `delta` | 两轮快照的差异，含 `added` / `removed` / `changed` 与 `counts` |
| 区间 / occupancy | 一次占用从 `first_seen_at` 到 `ended_at` 的物化记录；`ended_at IS NULL` 表示仍在占用 |
| `observed_removed` | 采集层真实观测到条目消失而闭合的区间 |
| `reconciled` | 对账时发现快照中已不存在而**推断**闭合的区间 |
| 心跳（历史层） | 每轮采集后刷新活跃区间 `last_seen_at` 的动作，不写事件表 |
| 心跳（SSE） | `/api/stream` 15s 保活的 `event: heartbeat`，与上者无关 |
| 对账 | 用当前快照校正库内开区间的过程：命中者刷 `last_seen_at`，未命中者闭合 |
| `access` | 归因完整度：`full` / `partial` / `denied` / `gone` |
| `protected` | 可处置性判定的保守结论：`pid ∈ {0,4}` 或 `access != full` |

---

*版本：随仓库 `README.md`（历史层 P0）同步。若代码与本文不一致，以代码为准，并请顺手更新本文。*