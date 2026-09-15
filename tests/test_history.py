"""历史层不变量测试（临时库，不碰真实 history.db）。

重点钉住三条设计纪律（改代码时最容易被破坏）：
1. last_seen_at 由**心跳**推进——稳定占用不产生 delta 时也必须前进
   （这正是历史层修复过的缺陷：last_seen_at 永远停在 first_seen_at，
   导致强杀后对账闭合的区间时长被虚增）；
2. port_event 只由采集层的真实观测追加——心跳与对账都不许写事件表；
3. 心跳/对账不会凭空造区间，只作用于 ended_at IS NULL 的区间。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import history

ENTRY = {
    "key": "tcp:8080:4321",
    "proto": "tcp",
    "port": 8080,
    "pid": 4321,
    "process": "python.exe",
    "exe": "C:\\Python311\\python.exe",
    "cmdline": "python server.py",
    "cwd": "C:\\work\\portwatch",
    "started_at": "2026-09-15T09:59:00",
    "listen_addrs": ["127.0.0.1"],
    "hint": "HTTP 备用 / Tomcat",
    "access": "full",
    "protected": False,
}

T1 = "2026-09-15T10:00:00"
T2 = "2026-09-15T10:00:04"


def _delta(added=(), removed=(), changed=(), ts=T1):
    return {
        "ts": ts,
        "added": list(added),
        "removed": list(removed),
        "changed": list(changed),
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def _removed_entry():
    """removed 载荷只带身份信息（与 server.summarize 一致）。"""
    return {"key": ENTRY["key"], "port": 8080, "proto": "tcp", "pid": 4321, "process": "python.exe"}


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_inv_key_and_detail_helpers():
    assert history.entry_key_of(ENTRY) == "tcp:8080:4321"
    # 占用实例键：比身份键多带进程创建时间，用于区分 PID 复用
    assert history.inv_key_of(ENTRY) == "tcp:8080:4321:2026-09-15T09:59:00"
    # 缺 started_at 时退化为空串尾缀（仍可用于物化区间）
    assert history.inv_key_of({"proto": "tcp", "port": 1, "pid": 2}) == "tcp:1:2:"
    # 缺 proto/port/pid 才返回 None
    assert history.inv_key_of({"port": 8080}) is None

    detail = json.loads(history._detail_json(ENTRY))
    assert detail["hint"] == "HTTP 备用 / Tomcat"
    # 独占列字段不应重复进 detail
    for column in ("port", "pid", "cmdline", "cwd", "started_at", "listen_addrs"):
        assert column not in detail


def test_enqueue_before_start_is_safe():
    """投递入口是同步非阻塞的：未启动时只计 dropped，绝不抛异常。"""
    writer = history.HistoryWriter(db_path="not-created.db")
    writer.enqueue(_delta(added=[ENTRY]))          # 不应抛异常
    assert writer.stats()["dropped"] == 1
    assert writer.heartbeat(None) is False
    assert writer.stats()["flushed_deltas"] == 0


def test_added_heartbeat_removed_pipeline(tmp_path):
    """完整链路：added 开区间 → 心跳推进 last_seen_at → removed 闭合区间。"""
    db = tmp_path / "history.db"

    async def scenario():
        writer = history.HistoryWriter(db_path=db, batch_size=1, flush_interval=0.05)
        await writer.start()
        writer.enqueue(_delta(added=[ENTRY], ts=T1))
        await asyncio.sleep(0.2)
        writer.heartbeat({"collected_at": T2, "entries": [ENTRY]})
        await asyncio.sleep(0.2)
        writer.enqueue(_delta(removed=[_removed_entry()], ts="2026-09-15T10:00:06"))
        await asyncio.sleep(0.2)
        await writer.stop()
        return writer.stats()

    stats = asyncio.run(scenario())

    assert stats["errors"] == 0
    assert stats["dropped"] == 0
    assert stats["flushed_deltas"] == 2

    events = _rows(db, "SELECT event_type, occurred_at, proto, port, pid FROM port_event ORDER BY id")
    assert [e["event_type"] for e in events] == ["added", "removed"]
    assert events[0]["occurred_at"] == T1
    assert (events[0]["proto"], events[0]["port"], events[0]["pid"]) == ("tcp", 8080, 4321)

    occupancy = _rows(db, "SELECT * FROM port_occupancy")
    assert len(occupancy) == 1
    row = occupancy[0]
    assert row["inv_key"] == "tcp:8080:4321:2026-09-15T09:59:00"
    assert row["first_seen_at"] == T1
    # 关键回归：稳定占用期间 last_seen_at 必须被心跳推进（否则时长会虚增）
    assert row["last_seen_at"] == T2
    assert row["last_seen_at"] != row["first_seen_at"]
    assert row["ended_at"] == "2026-09-15T10:00:06"
    assert row["end_reason"] == "observed_removed"


def test_heartbeat_never_creates_interval_nor_event(tmp_path):
    """库里没有对应区间时，心跳只是空转：不造区间、不写事件表。"""
    db = tmp_path / "history.db"

    async def scenario():
        writer = history.HistoryWriter(db_path=db, batch_size=1, flush_interval=0.05)
        await writer.start()
        assert writer.heartbeat({"collected_at": T1, "entries": [ENTRY]}) is True
        await asyncio.sleep(0.2)
        await writer.stop()

    asyncio.run(scenario())
    assert _rows(db, "SELECT COUNT(*) AS n FROM port_occupancy")[0]["n"] == 0
    assert _rows(db, "SELECT COUNT(*) AS n FROM port_event")[0]["n"] == 0


def test_reconcile_closes_residual_and_writes_no_event(tmp_path):
    """"对账是推断、不是观测"：只闭合区间并标 reconciled，绝不写 port_event。"""
    db = tmp_path / "history.db"

    async def scenario():
        writer = history.HistoryWriter(db_path=db, batch_size=1, flush_interval=0.05)
        await writer.start()
        writer.enqueue(_delta(added=[ENTRY], ts=T1))
        await asyncio.sleep(0.2)
        closed = await writer.reconcile({"collected_at": T2, "entries": []})
        await writer.stop()
        return closed

    assert asyncio.run(scenario()) == 1

    occupancy = _rows(db, "SELECT ended_at, end_reason, last_seen_at FROM port_occupancy")
    assert len(occupancy) == 1
    assert occupancy[0]["end_reason"] == "reconciled"
    assert occupancy[0]["ended_at"]
    # 对账只改区间表：事件表里仍然只有那条真实观测到的 added
    events = _rows(db, "SELECT event_type FROM port_event")
    assert [e["event_type"] for e in events] == ["added"]


def test_changed_updates_addr_but_does_not_move_interval(tmp_path):
    """changed 只刷新 local_addrs / last_seen_at，既不新建也不切换区间。"""
    db = tmp_path / "history.db"
    changed_entry = dict(ENTRY, listen_addrs=["127.0.0.1", "::1"])

    async def scenario():
        writer = history.HistoryWriter(db_path=db, batch_size=1, flush_interval=0.05)
        await writer.start()
        writer.enqueue(_delta(added=[ENTRY], ts=T1))
        await asyncio.sleep(0.2)
        writer.enqueue(_delta(changed=[changed_entry], ts=T2))
        await asyncio.sleep(0.2)
        await writer.stop()

    asyncio.run(scenario())

    occupancy = _rows(db, "SELECT * FROM port_occupancy")
    assert len(occupancy) == 1, "changed 不应该新建区间"
    assert occupancy[0]["first_seen_at"] == T1
    assert occupancy[0]["last_seen_at"] == T2
    assert occupancy[0]["ended_at"] is None
    assert json.loads(occupancy[0]["local_addrs"]) == ["127.0.0.1", "::1"]

    events = _rows(db, "SELECT event_type FROM port_event ORDER BY id")
    assert [e["event_type"] for e in events] == ["added", "changed"]
