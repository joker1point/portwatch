#!/usr/bin/env python3
"""
portwatch / API 层

在采集层之上提供两个接口：
    GET /api/ports   —— 当前端口占用快照（支持过滤 / 搜索）
    GET /api/stream  —— SSE 增量推送（新增 / 消失 / 变化）

架构要点：
    - 单例 Scanner 后台轮询，多客户端共享同一份采集结果，避免 N 个页面重复采集
    - 采集为阻塞调用，统一走线程池，不阻塞事件循环
    - 增量以 (协议, 端口, PID) 为条目身份键做 diff
    - 每轮 delta 在广播 SSE 的同时，非阻塞投递给历史层落库（见 history.py）
    - 每轮采集结束后另投递一次"活跃区间心跳"刷新 last_seen_at（无 delta 也刷）
    - 仅监听 127.0.0.1：该服务具备对本机进程的处置能力，绝不能暴露到局域网

用法:
    python server.py
    # 或: uvicorn server:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

import collector
import history

logger = logging.getLogger("portwatch.server")

HOST = "127.0.0.1"
PORT = 8787
DEFAULT_INTERVAL = 2.0
# 启动时对账一次，之后每 5 分钟增量对账一次
RECONCILE_INTERVAL = 300.0
# 启动对账前等待首轮采集的最长时间（100 * 0.1s）
WARMUP_TICKS = 100

# 系统内核进程，永远不允许处置
PROTECTED_PIDS = {0, 4}

# 这些字段发生变化即视为"条目已变更"
WATCH_FIELDS = ("process", "exe", "cmdline", "cwd", "parent_pid", "access")


# --------------------------------------------------------------------------- #
# 条目工具
# --------------------------------------------------------------------------- #

def entry_key(entry: dict[str, Any]) -> str:
    """条目身份键。端口 + 协议 + PID 三者确定一个唯一占用者。"""
    return f"{entry['proto']}:{entry['port']}:{entry['pid']}"


def annotate(entry: dict[str, Any]) -> dict[str, Any]:
    """给条目补充对外字段：身份键、可处置性判定。

    protected 取保守策略：归因信息受限的条目一律视为不可处置，
    宁可前端少给一个按钮，也不要让用户误杀系统服务。
    """
    item = dict(entry)
    item["key"] = entry_key(entry)

    reasons: list[str] = []
    if entry["pid"] in PROTECTED_PIDS:
        reasons.append("系统内核进程")
    if entry.get("access") != "full":
        reasons.append("归因信息受限")

    item["protected"] = bool(reasons)
    item["protected_reason"] = " / ".join(reasons) or None
    return item


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    """消失的条目只需回传身份信息，无需完整归因链路。"""
    return {
        "key": entry_key(entry),
        "port": entry["port"],
        "proto": entry["proto"],
        "pid": entry["pid"],
        "process": entry.get("process"),
    }


def compute_delta(prev: dict[str, dict], curr: dict[str, dict]) -> dict[str, Any]:
    """以身份键对比两轮快照，产出增量。"""
    added = [annotate(curr[k]) for k in curr.keys() - prev.keys()]
    removed = [summarize(prev[k]) for k in prev.keys() - curr.keys()]

    changed: list[dict[str, Any]] = []
    for key in curr.keys() & prev.keys():
        before, after = prev[key], curr[key]
        if any(before.get(f) != after.get(f) for f in WATCH_FIELDS):
            changed.append(annotate(after))

    added.sort(key=lambda e: e["port"])
    changed.sort(key=lambda e: e["port"])
    removed.sort(key=lambda e: e["port"])

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


def filter_entries(
    entries: list[dict[str, Any]],
    proto: str = "all",
    keyword: str | None = None,
    access: str = "all",
) -> list[dict[str, Any]]:
    """按协议 / 权限状态 / 关键词过滤。关键词同时命中端口号与归因链路各字段。"""
    result = entries

    if proto != "all":
        result = [e for e in result if e["proto"] == proto]
    if access != "all":
        result = [e for e in result if e.get("access") == access]

    if keyword:
        needle = keyword.strip().lower()
        result = [
            e
            for e in result
            if needle in str(e["port"])
            or any(
                needle in (e.get(field) or "").lower()
                for field in ("process", "cmdline", "exe", "cwd", "hint")
            )
        ]

    return result


# --------------------------------------------------------------------------- #
# 扫描器：单例轮询 + 广播
# --------------------------------------------------------------------------- #

class Scanner:
    """周期采集本机端口占用，维护最新快照并向所有订阅者广播增量。"""

    def __init__(self, interval: float = DEFAULT_INTERVAL, queue_size: int = 64) -> None:
        self.interval = interval
        self.queue_size = queue_size
        self.latest: dict[str, Any] | None = None
        self.index: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self.cycles = 0
        # 历史层落库入口（同步非阻塞）。为 None 时表示未装配历史层，
        # 逻辑与加历史层之前完全一致。
        self.event_sink: Any = None
        # 历史层心跳入口（同步非阻塞）：每轮采集结束后刷新活跃区间 last_seen_at。
        # 与 event_sink 分离，因为心跳在"本轮没有任何 delta"时也必须发生。
        self.heartbeat_sink: Any = None
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="portwatch-scanner")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                await self._broadcast("error", {"message": self.error})
            await asyncio.sleep(self.interval)

    async def tick(self) -> dict[str, Any] | None:
        """采集一轮；与上一轮有差异时广播 delta 并返回它。"""
        async with self._lock:
            snapshot = await run_in_threadpool(collector.collect)
            if snapshot.get("error"):
                self.error = snapshot["error"]
                return None

            curr = {entry_key(e): e for e in snapshot["entries"]}
            first_round = self.latest is None

            delta = None
            if not first_round:
                delta = compute_delta(self.index, curr)

            self.latest = snapshot
            self.index = curr
            self.error = None
            self.cycles += 1

        emitted = None
        if delta and (
            delta["counts"]["added"] or delta["counts"]["removed"] or delta["counts"]["changed"]
        ):
            await self._broadcast("delta", delta)
            # 历史层落库：同步非阻塞投递，与 SSE 广播互不影响；
            # 写入协程内部的任何 DB 异常都不会冒泡到这里。
            if self.event_sink is not None:
                self.event_sink(delta)
            emitted = delta

        # 历史层心跳：每轮采集结束都对"仍在占用"的活跃区间刷新 last_seen_at。
        # 必须放在 delta 判断之外——端口被稳定占用时本轮没有任何 delta，
        # 若只在有 delta 时刷新，last_seen_at 会永远停在 first_seen_at。
        # 心跳只 UPDATE 区间表、不写事件表，也不改变 SSE 广播契约。
        if self.heartbeat_sink is not None:
            self.heartbeat_sink(self.latest)

        return emitted

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, payload))
            except asyncio.QueueFull:
                # 消费不过来的连接直接丢弃，避免拖累采集循环
                self._subscribers.discard(queue)


scanner = Scanner()
history_writer = history.HistoryWriter()


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #

async def _wait_first_snapshot() -> dict[str, Any] | None:
    """等待首轮采集完成（最长 WARMUP_TICKS * 0.1s）。"""
    for _ in range(WARMUP_TICKS):
        if scanner.latest is not None:
            break
        await asyncio.sleep(0.1)
    return scanner.latest


async def _reconcile_loop() -> None:
    """启动对账一次，之后每 RECONCILE_INTERVAL(5min) 增量对账。"""
    await _wait_first_snapshot()
    while True:
        snapshot = scanner.latest
        if snapshot is not None:
            try:
                closed = await history_writer.reconcile(snapshot)
                if closed:
                    logger.info("对账闭合 %d 条残留占用区间", closed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 对账失败不得影响采集与广播
                logger.warning("对账任务异常: %s", exc)
        await asyncio.sleep(RECONCILE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await history_writer.start()
    scanner.event_sink = history_writer.enqueue
    # 心跳独立装配：每轮采集结束都刷新活跃区间 last_seen_at，无 delta 也刷
    scanner.heartbeat_sink = history_writer.heartbeat
    await scanner.start()
    reconcile_task = asyncio.create_task(_reconcile_loop(), name="portwatch-reconcile")
    try:
        yield
    finally:
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass
        scanner.event_sink = None
        scanner.heartbeat_sink = None
        # 先停采集（停止生产），再停历史层（把队列里剩余的 delta 落完）
        await scanner.stop()
        await history_writer.stop()


app = FastAPI(title="portwatch API", version="0.2.0", lifespan=lifespan)

# 只放行本机来源（含任意 localhost 端口），方便前端 dev server 直接联调
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(event: str, payload: Any) -> str:
    """SSE 帧。data 固定为单行 JSON，避免多行解析歧义。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if scanner.latest is not None else "warming_up",
        "cycles": scanner.cycles,
        "subscribers": scanner.subscriber_count,
        "last_error": scanner.error,
        "history": history_writer.stats(),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    snapshot = scanner.latest or {}
    return {
        "schema": collector.SCHEMA,
        "interval": scanner.interval,
        "host": snapshot.get("host"),
        "platform": snapshot.get("platform"),
        "is_admin": snapshot.get("is_admin"),
        "collected_at": snapshot.get("collected_at"),
        "stats": snapshot.get("stats", {}),
        "subscribers": scanner.subscriber_count,
    }


@app.get("/api/ports")
async def list_ports(
    proto: str = Query("all", pattern="^(all|tcp|udp)$"),
    q: str | None = Query(None, description="搜索端口号 / 进程名 / 命令行 / 路径"),
    access: str = Query("all", pattern="^(all|full|partial)$"),
    limit: int = Query(0, ge=0, description="0 表示不截断"),
) -> dict[str, Any]:
    snapshot = scanner.latest
    if snapshot is None:
        return {
            "schema": collector.SCHEMA,
            "warming_up": True,
            "total": 0,
            "matched": 0,
            "interval": scanner.interval,
            "entries": [],
        }

    items = [annotate(e) for e in snapshot["entries"]]
    matched = filter_entries(items, proto=proto, keyword=q, access=access)

    return {
        "schema": collector.SCHEMA,
        "warming_up": False,
        "collected_at": snapshot["collected_at"],
        "is_admin": snapshot["is_admin"],
        "interval": scanner.interval,
        "total": len(items),
        "matched": len(matched),
        "stats": snapshot["stats"],
        "entries": matched[:limit] if limit else matched,
    }


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE：首帧 snapshot 全量，之后仅推 delta / heartbeat。"""

    async def generate():
        queue = scanner.subscribe()
        try:
            # 等待首轮采集完成（最长 10s）
            for _ in range(100):
                if scanner.latest is not None:
                    break
                await asyncio.sleep(0.1)

            latest = scanner.latest
            if latest is not None:
                yield sse(
                    "snapshot",
                    {
                        "schema": collector.SCHEMA,
                        "collected_at": latest["collected_at"],
                        "is_admin": latest["is_admin"],
                        "stats": latest["stats"],
                        "entries": [annotate(e) for e in latest["entries"]],
                    },
                )

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield sse(
                        "heartbeat",
                        {"ts": datetime.now().isoformat(timespec="seconds"), "cycles": scanner.cycles},
                    )
                    continue
                yield sse(event, payload)
        finally:
            scanner.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
