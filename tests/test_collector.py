"""采集层冒烟测试：快照结构契约 + 归因链路（端口 → PID → 进程）。

这些测试不依赖管理员权限：拿不到全局连接表时按 skip 处理；
"自己起的监听端口必须能被自己看到"这条在受限环境下同样成立，因此保持严格。
"""

from __future__ import annotations

import os
import socket

import pytest

import collector

REQUIRED_FIELDS = (
    "port",
    "proto",
    "hint",
    "listen_addrs",
    "pid",
    "process",
    "exe",
    "cmdline",
    "cwd",
    "parent_pid",
    "started_at",
    "access",
)


def _snapshot(**kwargs):
    snap = collector.collect(**kwargs)
    if snap.get("error"):
        pytest.skip(f"采集层在当前环境不可用：{snap['error']}")
    return snap


@pytest.fixture(scope="module")
def snapshot():
    """整轮采集有点贵（要扫进程表），模块内共用一份。"""
    return _snapshot()


def test_snapshot_contract(snapshot):
    """输出结构、统计口径与字段取值域：快照 schema 是对外契约（portwatch/v1）。"""
    snap = snapshot

    assert snap["schema"] == "portwatch/v1"
    assert snap["collected_at"]
    assert isinstance(snap["is_admin"], bool)
    assert isinstance(snap["host"], str) and snap["host"]

    stats = snap["stats"]
    for key in ("total", "tcp", "udp", "access_full", "access_partial", "skipped_no_pid"):
        assert key in stats, f"stats 缺少 {key}"

    entries = snap["entries"]
    assert stats["total"] == len(entries)
    assert stats["tcp"] + stats["udp"] == len(entries)
    assert stats["access_full"] + stats["access_partial"] == len(entries)

    for entry in entries:
        for field in REQUIRED_FIELDS:
            assert field in entry, f"条目缺少字段 {field}"
        assert isinstance(entry["port"], int) and 0 < entry["port"] <= 65535
        assert entry["proto"] in ("tcp", "udp")
        assert entry["access"] in ("full", "partial", "denied", "gone")
        assert isinstance(entry["listen_addrs"], list)
        assert isinstance(entry["pid"], int)

    # 输出顺序稳定：按 (端口, 协议) 升序
    assert entries == sorted(entries, key=lambda e: (e["port"], e["proto"]))


def test_hint_is_display_only(snapshot):
    """端口语义提示只是展示增强：命中就是那串文案，命中不到必须是 None。"""
    for entry in snapshot["entries"]:
        expected = collector.PORT_HINTS.get(entry["port"])
        assert entry["hint"] == expected


def test_own_listening_port_is_attributed_to_this_process():
    """采集层必须能看到"自己刚起的监听端口"，并且把 PID 归因到自己。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        snap = _snapshot(include_udp=False)
        hits = [e for e in snap["entries"] if e["port"] == port and e["proto"] == "tcp"]
        assert hits, f"采集层没有看到本次测试起的监听端口 {port}"

        entry = hits[0]
        assert entry["pid"] == os.getpid(), "PID 归因错误"
        assert entry["listen_addrs"] == ["127.0.0.1"]
        # 自己的进程至少能拿到进程名，不应是 denied/gone
        assert entry["access"] in ("full", "partial")
        assert entry["process"] and "python" in entry["process"].lower()
    finally:
        sock.close()


def test_ppid_map_contains_self():
    """ppid 映射是 Windows 性能优化后的产物，至少要能稳定包含自己的父进程信息。"""
    mapping = collector.build_ppid_map()
    assert isinstance(mapping, dict)
    assert os.getpid() in mapping
    assert isinstance(mapping[os.getpid()], int)
