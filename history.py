#!/usr/bin/env python3
"""
portwatch / 端口占用历史层 (P0)

在"采集层 -> API 层 -> 前端"三层架构之上加一层**只追加的历史真相源**，
补齐实时 SSE 只能看到"此刻"、无法回溯"刚才发生了什么"的短板。
不改变任何对外契约，现有实时 SSE 行为保持原样。

组成:
    port_event      append-only 事件表（added / removed / changed），唯一真相源
    port_occupancy  区间表（由事件流物化出来的"某占用从何时到何时"），
                    ended_at IS NULL 表示该占用当前仍然存在

数据通路:
    Scanner.tick() 每轮 diff 出 delta
        ├─ 原路径: 立即广播 SSE（对外契约完全不变）
        ├─ delta 落库: 非阻塞投递到 asyncio.Queue -> 独立写入协程批量落库
        └─ 心跳: 每轮采集结束后投递一次"活跃区间心跳"，刷新 last_seen_at

    写入协程与采集循环完全解耦:
      * 投递(enqueue / heartbeat)是同步非阻塞调用，绝不 await、绝不阻塞 2 秒采集循环；
      * 落库走 asyncio.to_thread 包裹的同步 sqlite3，不占用事件循环；
      * 任何 DB 异常只记日志并计数，绝不冒泡到采集循环，也就不会中断 SSE 广播。

last_seen_at 心跳（修复"last_seen_at 永远停在 first_seen_at"的缺陷）:
    端口被稳定占用时，采集层每轮 diff 都是空的，既没有 changed 也没有任何 delta。
    若把 last_seen_at 的刷新挂在 delta（特别是 changed）上，该字段就会一直停在
    first_seen_at 的值，区间时长彻底失真。因此心跳**不依赖 delta 触发**：
    每轮采集结束后（无论本轮有没有 delta）都把当前快照里仍在监听的占用实例
    投递一次心跳，对 interval 做
        UPDATE port_occupancy SET last_seen_at = ? WHERE inv_key = ? AND ended_at IS NULL
    语义约束:
      * 只 UPDATE port_occupancy.last_seen_at，**绝不写 port_event**——
        事件表永远只由采集层 delta 追加，心跳不是一次"事件"；
      * 只更新仍开着的区间（ended_at IS NULL）；已闭合区间不再被心跳拉扯；
      * 库里没有对应区间（例如服务启动前就已存在的端口）时 UPDATE 自然空转，
        不会凭空造出区间。

时间语义（诚实声明，不假装精确）:
    采样是离散的（默认 2 秒一轮），一次 diff 只能把变化定位在
        (上一轮采样时刻, 本轮采样时刻]
    这个半开区间内。因此表中的 occurred_at / first_seen_at / last_seen_at
    记录的是"观测到该变化的采样时刻"，**不是**事件真实发生的时刻——
    存在最多一个采样间隔（默认 2 秒）的不确定性；若两个变化落在同一个
    采样区间内先后发生，其先后顺序会丢失。字段名刻意用 first_seen_at /
    occurred_at 而非 start_time / event_time，就是为了不误导使用者。

已知限制:
    采集层的 diff 身份键目前仍是 proto:port:pid（见 server.entry_key），
    因此当某个 PID 被系统复用、且新进程恰好占用同一端口时，采集层产不出
    delta，历史层也就不会有 removed/added，表现为"占用一直被算作同一个区间"。
    本层用 inv_key = proto:port:pid:create_time 已经在存储侧区分了 PID 复用，
    但源头漏检无法在存储侧补救，后续需要把采集层的身份键升级为
    proto:port:pid:create_time。

用法（通常由 server.py 自动装配）:
    python history.py --db history.db --dump    # 打印库内区间与事件摘要
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("portwatch.history")

# 库文件默认落在项目根目录，可用环境变量覆盖（便于测试隔离）
DEFAULT_DB_PATH = Path(
    os.environ.get("PORTWATCH_DB") or (Path(__file__).resolve().parent / "history.db")
)

# 攒够 20 条 或 距首条满 1 秒，二者先到即 flush
BATCH_SIZE = 20
FLUSH_INTERVAL = 1.0

END_REASON_OBSERVED = "observed_removed"  # 采集层观测到条目消失
END_REASON_RECONCILED = "reconciled"      # 启动/增量对账时发现快照中已不存在

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS port_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_key     TEXT    NOT NULL,
    inv_key       TEXT,
    occurred_at   TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('added', 'removed', 'changed')),
    proto         TEXT,
    port          INTEGER,
    pid           INTEGER,
    process_name  TEXT,
    exe           TEXT,
    cmdline       TEXT,
    cwd           TEXT,
    create_time   TEXT,
    local_addrs   TEXT,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS port_occupancy (
    inv_key       TEXT    PRIMARY KEY,
    proto         TEXT    NOT NULL,
    port          INTEGER NOT NULL,
    pid           INTEGER NOT NULL,
    process_name  TEXT,
    exe           TEXT,
    cmdline       TEXT,
    cwd           TEXT,
    create_time   TEXT,
    local_addrs   TEXT,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    ended_at      TEXT,
    end_reason    TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_entry_time ON port_event(entry_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_inv_time   ON port_event(inv_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_port_time  ON port_event(port, occurred_at);
CREATE INDEX IF NOT EXISTS idx_occ_port_first   ON port_occupancy(port, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_occ_pid_first    ON port_occupancy(pid, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_occ_open         ON port_occupancy(ended_at) WHERE ended_at IS NULL;
"""

# 已在 port_event 中独占列存在的字段，其余字段统一进 detail(JSON)
_EVENT_COLUMNS = {
    "key",
    "inv_key",
    "occurred_at",
    "event_type",
    "proto",
    "port",
    "pid",
    "process",
    "exe",
    "cmdline",
    "cwd",
    "started_at",
    "listen_addrs",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def entry_key_of(entry: dict[str, Any]) -> str:
    """采集层身份键：proto:port:pid（与 server.entry_key 保持一致）。"""
    return f"{entry.get('proto')}:{entry.get('port')}:{entry.get('pid')}"


def inv_key_of(entry: dict[str, Any]) -> str | None:
    """占用实例键：proto:port:pid:create_time。

    比 entry_key 多带进程创建时间，用于在存储侧区分"同一个 PID 被复用"的
    前后两个不同进程实例。
    """
    proto = entry.get("proto")
    port = entry.get("port")
    pid = entry.get("pid")
    if proto is None or port is None or pid is None:
        return None
    create_time = entry.get("started_at") or entry.get("create_time") or ""
    return f"{proto}:{port}:{pid}:{create_time}"


def _detail_json(entry: dict[str, Any]) -> str:
    extra = {k: v for k, v in entry.items() if k not in _EVENT_COLUMNS}
    return json.dumps(extra or None, ensure_ascii=False)


def _addrs_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry.get("listen_addrs") or [], ensure_ascii=False)


def _event_row(
    event_type: str,
    entry: dict[str, Any],
    entry_key: str,
    inv_key: str | None,
    ts: str,
) -> tuple:
    return (
        entry_key,
        inv_key,
        ts,
        event_type,
        entry.get("proto"),
        entry.get("port"),
        entry.get("pid"),
        entry.get("process"),
        entry.get("exe"),
        entry.get("cmdline"),
        entry.get("cwd"),
        entry.get("started_at"),
        _addrs_json(entry),
        _detail_json(entry),
    )


_INSERT_EVENT = """
INSERT INTO port_event (
    entry_key, inv_key, occurred_at, event_type, proto, port, pid,
    process_name, exe, cmdline, cwd, create_time, local_addrs, detail
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# 开区间：条目首次出现。若同 inv_key 的历史区间已闭合（理论上不该发生），
# 以"同一个进程实例重新开始占用"处理，复用它原本的 first_seen_at。
_OPEN_OCCUPANCY = """
INSERT INTO port_occupancy (
    inv_key, proto, port, pid, process_name, exe, cmdline, cwd,
    create_time, local_addrs, first_seen_at, last_seen_at, ended_at, end_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
ON CONFLICT(inv_key) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    ended_at     = NULL,
    end_reason   = NULL,
    process_name = excluded.process_name,
    exe          = excluded.exe,
    cmdline      = excluded.cmdline,
    cwd          = excluded.cwd,
    local_addrs  = excluded.local_addrs
"""

# changed 只刷新 local_addrs / last_seen_at，绝不新建或切换区间
_TOUCH_OCCUPANCY = """
UPDATE port_occupancy
   SET local_addrs = ?, last_seen_at = ?
 WHERE inv_key = ? AND ended_at IS NULL
"""

_CLOSE_OCCUPANCY = """
UPDATE port_occupancy
   SET ended_at = ?, end_reason = ?
 WHERE inv_key = ? AND ended_at IS NULL
"""

# 心跳：只刷新 last_seen_at，只作用于仍开着的区间，绝不写 port_event
_HEARTBEAT_OCCUPANCY = """
UPDATE port_occupancy
   SET last_seen_at = ?
 WHERE inv_key = ? AND ended_at IS NULL
"""

_FIND_OPEN_BY_ENTRY = """
SELECT inv_key FROM port_occupancy
 WHERE proto = ? AND port = ? AND pid = ? AND ended_at IS NULL
 ORDER BY first_seen_at DESC
 LIMIT 1
"""


class HistoryWriter:
    """delta + 心跳批量落库器：asyncio.Queue + 独立写入协程。

    对外暴露三个同步非阻塞入口（enqueue / heartbeat）和三个 async 入口
    （start / stop / reconcile）。设计上保证：
      * enqueue / heartbeat 永不阻塞、永不抛异常；
      * 所有 sqlite3 调用都在 asyncio.to_thread 里执行；
      * DB 异常只记日志，不影响调用方（尤其是采集循环与 SSE 广播）。

    队列里跑两种消息（都是 (kind, payload) 二元组）:
      ("delta",     delta_dict)          —— 采集层 diff 出来的一次变化
      ("heartbeat", (ts, [(entry_key, inv_key), ...]))
                                         —— 本轮仍在监听、需要刷新 last_seen_at
                                            的活跃占用实例
    心跳消息同时充当"立即刷"信号：它到达即结束本次攒批，保证 last_seen_at
    尽量贴近观测时刻（服务被强杀时最多只丢当前这一轮，不会连续滞后多轮）。
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        batch_size: int = BATCH_SIZE,
        flush_interval: float = FLUSH_INTERVAL,
    ) -> None:
        self.db_path = Path(db_path)
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = float(flush_interval)

        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._conn: sqlite3.Connection | None = None
        # 串行化"写库"与"对账"两条路径，同时保证 _active 只在单个线程内被改动
        self._lock = asyncio.Lock()

        # entry_key -> inv_key：removed 事件里没有 started_at，
        # 需要靠这里把区间闭合到正确的占用实例上。
        # 除 delta 路径（added/changed）维护外，每轮心跳都用当前快照整体重建，
        # 因此它是"当前仍在占用"的真实索引，active_intervals 也据此统计。
        self._active: dict[str, str] = {}

        self._flushed_deltas = 0
        self._dropped = 0
        self._errors = 0
        # 库内 ended_at IS NULL 的区间数（每次心跳落库后顺带统计，供 /api/health 对齐口径）
        self._open_intervals = 0
        # 心跳计数：written = 实际落库的心跳轮数，dropped = 队列投递失败次数
        self._heartbeats = 0
        self._heartbeat_dropped = 0
        self._last_heartbeat_at: str | None = None
        self._last_heartbeat_keys = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = asyncio.Queue()
        try:
            await asyncio.to_thread(self._ensure_schema)
            logger.info("历史层已启动，库文件: %s", self.db_path)
        except Exception as exc:
            # 建库失败也不能拖垮整个服务：实时 SSE 必须继续可用
            self._errors += 1
            logger.warning("历史层初始化失败(实时 SSE 不受影响): %s", exc)
        self._task = asyncio.create_task(self._run(), name="portwatch-history-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        await self._flush_pending()
        logger.info("历史层已停止，累计落库 delta %d 条", self._flushed_deltas)

    def enqueue(self, delta: dict[str, Any]) -> None:
        """非阻塞投递一条 delta。绝不在采集循环里 await，也绝不抛异常。"""
        queue = self._queue
        if queue is None:
            self._dropped += 1
            return
        try:
            queue.put_nowait(("delta", delta))
        except Exception as exc:  # 队列异常也不允许影响采集循环
            self._dropped += 1
            logger.warning("投递历史事件失败(已丢弃): %s", exc)

    def heartbeat(self, snapshot: dict[str, Any] | None) -> bool:
        """每轮采集结束后调用：刷新本轮"仍在占用"的活跃区间 last_seen_at。

        与 delta 无关，本轮没有任何 delta 时同样会调用——这正是修复
        "稳定占用时 last_seen_at 停在 first_seen_at" 的关键。

        入参是当前快照（server.Scanner.latest）。时间戳取快照的 collected_at
        （本轮采集完成的时刻），而不是落库时刻，这样即使队列有积压，
        last_seen_at 反映的仍是"观测时刻"。

        投递内容除 ts 外还带上本轮活跃条目的 (entry_key, inv_key) 对，
        写入协程据此重建内存活跃索引 _active，使 active_intervals 反映
        "当前仍在占用"的真实数量，并让后续 removed 事件能直接命中区间。

        返回是否成功投递（仅用于自检，调用方不需要判断）。
        """
        queue = self._queue
        if queue is None or not snapshot:
            return False

        pairs: list[tuple[str, str]] = []
        for entry in snapshot.get("entries") or []:
            inv_key = inv_key_of(entry)
            if inv_key is None:
                continue
            pairs.append((entry.get("key") or entry_key_of(entry), inv_key))
        if not pairs:
            return False

        ts = snapshot.get("collected_at") or now_iso()
        try:
            queue.put_nowait(("heartbeat", (ts, pairs)))
            return True
        except Exception as exc:
            # 心跳丢了只是 last_seen_at 少刷一轮，绝不允许影响采集循环
            self._heartbeat_dropped += 1
            logger.warning("投递心跳失败(已丢弃): %s", exc)
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "flushed_deltas": self._flushed_deltas,
            "dropped": self._dropped,
            "errors": self._errors,
            # 内存活跃索引：当前快照中仍在占用的实例数（心跳每轮重建）
            "active_intervals": len(self._active),
            # 库内真值：port_occupancy 中 ended_at IS NULL 的区间数
            # 两者口径不同（历史层不回溯服务启动前就存在的占用，详见 README 已知限制）
            "open_intervals": self._open_intervals,
            "heartbeats": self._heartbeats,
            "heartbeat_dropped": self._heartbeat_dropped,
            "last_heartbeat_at": self._last_heartbeat_at,
            "last_heartbeat_keys": self._last_heartbeat_keys,
        }

    # ------------------------------------------------------------------ #
    # 写入协程
    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        while True:
            batch = await self._collect_batch()
            if batch:
                await self._flush(batch)

    async def _collect_batch(self) -> list[tuple[str, Any]]:
        """等到首条消息后开始攒批，满足任一条件即返回：

        * 攒够 batch_size 条；
        * 距首条已满 flush_interval 秒；
        * 批内出现心跳消息 —— 心跳意味着"本轮观测已完成"，立即落库才能让
          last_seen_at 贴近观测时刻（服务被强杀时最多只丢当前这一轮，
          不会连续滞后好几轮）。delta 的批量语义不受影响：同一轮的 delta
          本就成串到达，仍会按 batch_size 攒批。
        """
        queue = self._queue
        if queue is None:
            return []

        first = await queue.get()
        batch: list[tuple[str, Any]] = [first]
        if self._is_heartbeat(first) or len(batch) >= self.batch_size:
            return batch

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.flush_interval
        while len(batch) < self.batch_size:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            batch.append(msg)
            if self._is_heartbeat(msg):
                break
        return batch

    @staticmethod
    def _is_heartbeat(msg: Any) -> bool:
        return isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "heartbeat"

    async def _flush(self, batch: list[tuple[str, Any]]) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self._write_batch_sync, batch)
            except Exception as exc:
                self._errors += 1
                # 只记日志：DB 问题不允许中断实时 SSE 广播
                logger.warning("历史层写库失败(不影响实时广播): %s", exc)

    async def _flush_pending(self) -> None:
        """关停前把队列里剩余消息落库（尽力而为）。"""
        queue = self._queue
        if queue is None:
            return
        pending: list[tuple[str, Any]] = []
        while True:
            try:
                pending.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for i in range(0, len(pending), self.batch_size):
            await self._flush(pending[i : i + self.batch_size])
        self._queue = None

    # ------------------------------------------------------------------ #
    # 对账
    # ------------------------------------------------------------------ #

    async def reconcile(self, snapshot: dict[str, Any]) -> int:
        """把库里所有"仍开着"的区间与当前快照比对，快照中不存在的闭区间。

        返回本次闭合的区间数。服务启动时执行一次，之后每 5 分钟增量执行。
        说明: 对账只修复区间表，不写事件表——它并非一次真实观测，
        port_event 始终保持"只由采集层 delta 追加"的语义。
        """
        async with self._lock:
            try:
                return await asyncio.to_thread(self._reconcile_sync, snapshot)
            except Exception as exc:
                self._errors += 1
                logger.warning("对账失败: %s", exc)
                return 0

    # ------------------------------------------------------------------ #
    # 同步 sqlite3 部分（一律在线程池中执行）
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._ensure_conn()
        with conn:
            conn.executescript(SCHEMA_SQL)

    def _write_batch_sync(self, batch: list[tuple[str, Any]]) -> None:
        conn = self._ensure_conn()
        for kind, payload in batch:
            if kind == "heartbeat":
                self._apply_heartbeat_sync(conn, payload)
                continue
            try:
                # 每条 delta 独立事务：单条坏数据不会连累整批
                with conn:
                    self._apply_delta(conn, payload)
            except Exception as exc:
                self._errors += 1
                logger.warning("跳过一条异常 delta: %s", exc)
            else:
                self._flushed_deltas += 1

    def _apply_heartbeat_sync(self, conn: sqlite3.Connection, payload: tuple) -> None:
        """把某一轮采集观测到的活跃区间 last_seen_at 刷成该轮时刻。

        payload = (ts, [(entry_key, inv_key), ...])，ts 是该轮采集完成的时刻。
        只 UPDATE port_occupancy.last_seen_at，不写 port_event；
        只作用于 ended_at IS NULL 的区间，已闭合区间不受影响。

        顺带用本轮真实快照重建内存活跃索引 _active（只在写入线程内改动，
        不引入跨线程共享），使 active_intervals 反映当前仍在占用的实例数。
        """
        ts, pairs = payload
        keys = [inv_key for _, inv_key in pairs]
        try:
            with conn:
                conn.executemany(_HEARTBEAT_OCCUPANCY, [(ts, key) for key in keys])
            # 顺带对齐库内真值：开区间数（走 (ended_at) IS NULL 部分索引，成本可忽略）
            row = conn.execute(
                "SELECT COUNT(*) FROM port_occupancy WHERE ended_at IS NULL"
            ).fetchone()
            open_intervals = int(row[0]) if row else 0
        except Exception as exc:
            self._errors += 1
            logger.warning("心跳刷新失败(不影响采集与广播): %s", exc)
        else:
            self._heartbeats += 1
            self._last_heartbeat_at = ts
            self._last_heartbeat_keys = len(keys)
            self._open_intervals = open_intervals
            self._active = dict(pairs)

    def _apply_delta(self, conn: sqlite3.Connection, delta: dict[str, Any]) -> None:
        ts = delta.get("ts") or now_iso()

        for entry in delta.get("added") or []:
            self._on_added(conn, entry, ts)
        for entry in delta.get("changed") or []:
            self._on_changed(conn, entry, ts)
        for entry in delta.get("removed") or []:
            self._on_removed(conn, entry, ts)

    def _on_added(self, conn: sqlite3.Connection, entry: dict[str, Any], ts: str) -> None:
        entry_key = entry.get("key") or entry_key_of(entry)
        inv_key = inv_key_of(entry)
        conn.execute(_INSERT_EVENT, _event_row("added", entry, entry_key, inv_key, ts))
        if inv_key is None:
            # 缺 proto/port/pid，无法物化区间，事件仍保留在真相源里
            logger.warning("added 事件缺少身份字段，未开区间: %s", entry_key)
            return
        self._active[entry_key] = inv_key
        conn.execute(
            _OPEN_OCCUPANCY,
            (
                inv_key,
                entry.get("proto"),
                entry.get("port"),
                entry.get("pid"),
                entry.get("process"),
                entry.get("exe"),
                entry.get("cmdline"),
                entry.get("cwd"),
                entry.get("started_at"),
                _addrs_json(entry),
                ts,  # first_seen_at
                ts,  # last_seen_at
            ),
        )

    def _on_changed(self, conn: sqlite3.Connection, entry: dict[str, Any], ts: str) -> None:
        entry_key = entry.get("key") or entry_key_of(entry)
        inv_key = inv_key_of(entry)
        conn.execute(_INSERT_EVENT, _event_row("changed", entry, entry_key, inv_key, ts))
        if inv_key is None:
            return
        self._active[entry_key] = inv_key
        conn.execute(_TOUCH_OCCUPANCY, (_addrs_json(entry), ts, inv_key))

    def _on_removed(self, conn: sqlite3.Connection, entry: dict[str, Any], ts: str) -> None:
        entry_key = entry.get("key") or entry_key_of(entry)
        inv_key = self._resolve_inv_key(conn, entry, entry_key)
        conn.execute(_INSERT_EVENT, _event_row("removed", entry, entry_key, inv_key, ts))
        if inv_key is None:
            logger.warning("removed 事件找不到对应开着区间: %s", entry_key)
            return
        conn.execute(_CLOSE_OCCUPANCY, (ts, END_REASON_OBSERVED, inv_key))

    def _resolve_inv_key(
        self, conn: sqlite3.Connection, entry: dict[str, Any], entry_key: str
    ) -> str | None:
        """removed 条目不携带 started_at，靠内存映射回推 inv_key。

        内存映射未命中（进程重启 / 事件丢失）时退回按 proto:port:pid 查开着区间。
        """
        inv_key = self._active.pop(entry_key, None)
        if inv_key:
            return inv_key
        row = conn.execute(
            _FIND_OPEN_BY_ENTRY,
            (entry.get("proto"), entry.get("port"), entry.get("pid")),
        ).fetchone()
        return row["inv_key"] if row else None

    def _reconcile_sync(self, snapshot: dict[str, Any]) -> int:
        conn = self._ensure_conn()
        ts = now_iso()

        live: dict[str, str] = {}
        for entry in snapshot.get("entries") or []:
            inv_key = inv_key_of(entry)
            if inv_key is not None:
                live[inv_key] = entry_key_of(entry)

        rows = conn.execute(
            "SELECT inv_key, proto, port, pid FROM port_occupancy WHERE ended_at IS NULL"
        ).fetchall()

        closed = 0
        with conn:
            for row in rows:
                inv_key = row["inv_key"]
                if inv_key in live:
                    # 仍在占用：只刷新 last_seen_at，不动区间边界
                    conn.execute(
                        "UPDATE port_occupancy SET last_seen_at = ? "
                        " WHERE inv_key = ? AND ended_at IS NULL",
                        (ts, inv_key),
                    )
                    continue
                cursor = conn.execute(
                    _CLOSE_OCCUPANCY, (ts, END_REASON_RECONCILED, inv_key)
                )
                if cursor.rowcount:
                    closed += 1
                    self._active.pop(f"{row['proto']}:{row['port']}:{row['pid']}", None)
        if closed:
            logger.info("对账闭合 %d 条残留区间 (end_reason=%s)", closed, END_REASON_RECONCILED)
        return closed


# --------------------------------------------------------------------------- #
# 自检 / 排障入口
# --------------------------------------------------------------------------- #

def dump(db_path: str | Path) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    lines: list[str] = [f"库文件: {db_path}"]

    events = conn.execute("SELECT * FROM port_event ORDER BY id").fetchall()
    lines.append(f"port_event 共 {len(events)} 行")
    for row in events:
        lines.append(
            f"  #{row['id']:<4} {row['occurred_at']}  {row['event_type']:<8} "
            f"{row['proto']}:{row['port']} pid={row['pid']} "
            f"proc={row['process_name']} inv={row['inv_key']}"
        )

    occ = conn.execute("SELECT * FROM port_occupancy ORDER BY first_seen_at").fetchall()
    lines.append(f"port_occupancy 共 {len(occ)} 行")
    for row in occ:
        state = f"ended={row['ended_at']} ({row['end_reason']})" if row["ended_at"] else "仍在占用"
        lines.append(
            f"  {row['inv_key']}  first={row['first_seen_at']} last={row['last_seen_at']} {state}"
        )

    conn.close()
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="portwatch 端口占用历史层")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 库文件路径")
    parser.add_argument("--dump", action="store_true", help="打印库内区间与事件摘要")
    args = parser.parse_args()

    if args.dump:
        print(dump(args.db))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
