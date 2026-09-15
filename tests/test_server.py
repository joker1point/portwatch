"""API 层纯函数测试：身份键、可处置性标注、diff 语义、过滤。

这些函数不依赖 psutil / 数据库，是 server.py 里最该被钉死的契约：
其中"listen_addrs 变化不产生 changed""removed 只回身份信息"是文档里
明确写过的行为，改代码时很容易被无意破坏。
"""

from __future__ import annotations

import server


def _entry(**overrides):
    entry = {
        "proto": "tcp",
        "port": 8080,
        "hint": "HTTP 备用 / Tomcat",
        "listen_addrs": ["127.0.0.1"],
        "pid": 4321,
        "process": "python.exe",
        "exe": "C:\\Python311\\python.exe",
        "cmdline": "python server.py",
        "cwd": "C:\\work\\portwatch",
        "parent_pid": 1000,
        "started_at": "2026-09-15T09:59:00",
        "access": "full",
    }
    entry.update(overrides)
    return entry


def test_entry_key_format():
    assert server.entry_key(_entry()) == "tcp:8080:4321"
    assert server.entry_key(_entry(port=53, proto="udp", pid=4)) == "udp:53:4"


def test_annotate_protection_is_conservative():
    """可处置性判定是保守策略：内核 PID 与"归因受限"都不可处置。"""
    assert server.annotate(_entry())["protected"] is False
    assert server.annotate(_entry())["protected_reason"] is None

    kernel = server.annotate(_entry(pid=4))
    assert kernel["protected"] is True
    assert kernel["protected_reason"] == "系统内核进程"

    limited = server.annotate(_entry(access="partial"))
    assert limited["protected"] is True
    assert limited["protected_reason"] == "归因信息受限"

    both = server.annotate(_entry(pid=0, access="denied"))
    assert both["protected_reason"] == "系统内核进程 / 归因信息受限"

    # 标注不能改动原始条目
    original = _entry()
    server.annotate(original)
    assert "protected" not in original


def test_delta_added_removed_changed():
    base = _entry(pid=1, port=8080)
    prev = {server.entry_key(base): base}

    # 新增一个端口
    fresh = _entry(pid=2, port=9090)
    curr = {server.entry_key(base): base, server.entry_key(fresh): fresh}
    delta = server.compute_delta(prev, curr)
    assert delta["counts"] == {"added": 1, "removed": 0, "changed": 0}
    assert delta["added"][0]["port"] == 9090
    assert delta["added"][0]["protected"] is False
    assert delta["ts"]

    # 全消失
    delta = server.compute_delta(prev, {})
    assert delta["counts"] == {"added": 0, "removed": 1, "changed": 0}
    assert delta["removed"][0]["key"] == "tcp:8080:1"
    # removed 只回身份信息，不回完整归因链路
    assert set(delta["removed"][0]) == {"key", "port", "proto", "pid", "process"}


def test_watch_fields_define_changed():
    """WATCH_FIELDS 内的字段变化产生 changed；监听地址变化刻意不产生。"""
    base = _entry(pid=1)
    key = server.entry_key(base)
    prev = {key: base}

    addr_only = _entry(pid=1, listen_addrs=["127.0.0.1", "::1"])
    assert server.compute_delta(prev, {key: addr_only})["counts"]["changed"] == 0

    for field, value in (
        ("process", "python3.11.exe"),
        ("exe", "C:\\other\\python.exe"),
        ("cmdline", "python server.py --verbose"),
        ("cwd", "C:\\other"),
        ("parent_pid", 2000),
        ("access", "partial"),
    ):
        changed = server.compute_delta(prev, {key: _entry(pid=1, **{field: value})})
        assert changed["counts"]["changed"] == 1, f"{field} 变化未触发 changed"
        assert changed["changed"][0]["port"] == 8080


def test_delta_arrays_are_sorted_by_port():
    prev = {f"tcp:{port}:{port}": _entry(port=port, pid=port) for port in (9090, 8080, 7070)}
    delta = server.compute_delta(prev, {})
    assert [e["port"] for e in delta["removed"]] == [7070, 8080, 9090]


def test_filter_entries():
    items = [
        server.annotate(_entry(port=3306, pid=11, process="mysqld.exe", hint="MySQL")),
        server.annotate(_entry(port=6379, pid=12, process="redis-server.exe", access="partial")),
        server.annotate(_entry(port=5353, pid=13, proto="udp", process="svchost.exe")),
    ]

    def ports(result):
        return sorted(e["port"] for e in result)

    assert ports(server.filter_entries(items, proto="tcp")) == [3306, 6379]
    assert ports(server.filter_entries(items, proto="udp")) == [5353]
    assert ports(server.filter_entries(items, access="partial")) == [6379]
    assert ports(server.filter_entries(items, keyword="3306")) == [3306]
    assert ports(server.filter_entries(items, keyword="redis")) == [6379]
    # 关键词命中范围包含端口语义提示
    assert ports(server.filter_entries(items, keyword="mysql")) == [3306]
    # 组合条件
    assert ports(server.filter_entries(items, proto="tcp", access="partial")) == [6379]
    assert server.filter_entries(items, keyword="不存在的关键词") == []
