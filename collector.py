#!/usr/bin/env python3
"""
portwatch / 数据采集层 (MVP)

采集本机处于监听/绑定状态的 TCP / UDP 端口，并沿 PID 追溯完整归因链路：
    端口 -> PID -> 进程名 -> exe 路径 -> 启动命令行 -> 工作目录 -> 进程启动时间

用法:
    python collector.py                    # 采集并打印摘要
    python collector.py --json ports.json  # 同时写出 JSON 快照
    python collector.py --raw              # 不做 IPv4/IPv6 双栈合并
    python collector.py --all-proto        # 仅 TCP LISTEN (默认) / 含 UDP

设计要点:
    - 逐字段降级: 系统进程的 exe / cmdline / cwd 常因权限缺失，缺失即标 partial，绝不抛异常
    - 进程信息按 PID 缓存，避免同一进程多端口时重复查询
    - 双栈合并: 同一 (协议, 端口, PID) 的 IPv4/IPv6 记录合并为一条，地址列表保留
"""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import socket
import sys
from datetime import datetime
from typing import Any

import psutil

SCHEMA = "portwatch/v1"

PROTO_TCP = "tcp"
PROTO_UDP = "udp"

# 常见端口语义标注（低成本、高可读性）
PORT_HINTS: dict[int, str] = {
    1433: "SQL Server",
    1521: "Oracle",
    2181: "ZooKeeper",
    2375: "Docker daemon (plain)",
    2376: "Docker daemon (TLS)",
    3000: "前端 dev server (Node / Next)",
    3001: "前端 dev server (备用)",
    3306: "MySQL",
    5000: "Flask / 备用 HTTP",
    5173: "Vite dev server",
    5432: "PostgreSQL",
    5601: "Kibana",
    6379: "Redis",
    8000: "Django / FastAPI / 备用 HTTP",
    8080: "HTTP 备用 / Tomcat",
    8888: "Jupyter / 备用 HTTP",
    9000: "PHP-FPM / 备用",
    9090: "Prometheus / 备用",
    9092: "Kafka",
    9200: "Elasticsearch",
    11211: "Memcached",
    27017: "MongoDB",
}


def is_admin() -> bool:
    """当前进程是否具备管理员权限（决定归因链路能拿到多少字段）。"""
    if sys.platform != "win32":
        try:
            import os

            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _safe(fn, default=None):
    """调用可能因权限/进程消失而失败的 psutil 接口，失败返回 default。"""
    try:
        return fn()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return default
    except OSError:
        return default
    except Exception:
        return default


def build_ppid_map() -> dict[int, int]:
    """一次性构建 {pid: ppid} 全量映射。

    Windows 上 psutil.Process.ppid() 每次调用都会重建一次全量进程快照
    (实测约 45ms)，逐进程调用 110 次即 5.4 秒，是本采集器最大的性能陷阱。
    改为一次快照 + 查表后，该部分开销降到 ~45ms。
    """
    if sys.platform == "win32":
        try:
            from psutil._pswindows import ppid_map  # 私有 API，不可用则走兜底

            return dict(ppid_map())
        except Exception:
            pass

    mapping: dict[int, int] = {}
    for proc in psutil.process_iter(["pid", "ppid"]):
        try:
            mapping[proc.info["pid"]] = proc.info["ppid"]
        except Exception:
            continue
    return mapping


def collect_process(pid: int, ppid_map: dict[int, int] | None = None) -> dict[str, Any]:
    """沿 PID 追溯归因链路，逐字段降级。"""
    info: dict[str, Any] = {
        "pid": pid,
        "name": None,
        "exe": None,
        "cmdline": None,
        "cwd": None,
        "parent_pid": None,
        "started_at": None,
        "access": "full",
    }

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        info["access"] = "gone"
        return info
    except psutil.AccessDenied:
        info["access"] = "denied"
        return info

    missing = False

    def grab(field: str, fn) -> None:
        nonlocal missing
        value = _safe(fn)
        if value is None:
            missing = True
        info[field] = value

    grab("name", proc.name)
    grab("exe", proc.exe)
    grab("cwd", proc.cwd)

    if ppid_map is not None:
        info["parent_pid"] = ppid_map.get(pid)

    argv = _safe(proc.cmdline)
    if argv:
        info["cmdline"] = " ".join(argv)
    else:
        missing = True

    created = _safe(proc.create_time)
    if created:
        info["started_at"] = datetime.fromtimestamp(created).isoformat(timespec="seconds")

    info["access"] = "partial" if missing else "full"
    return info


def collect(merge_dual_stack: bool = True, include_udp: bool = True) -> dict[str, Any]:
    """采集端口占用快照。"""
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return {
            "schema": SCHEMA,
            "error": "net_connections 被系统拒绝，请以管理员身份重试",
            "entries": [],
            "stats": {},
        }

    buckets: dict[tuple, dict[str, Any]] = {}
    no_pid = 0

    for conn in conns:
        is_udp = conn.type == socket.SOCK_DGRAM

        if is_udp:
            if not include_udp:
                continue
        elif conn.status != psutil.CONN_LISTEN:
            # TCP 只关心监听态（排除 ESTABLISHED / TIME_WAIT 等）
            continue

        pid = conn.pid
        if pid is None:
            no_pid += 1
            continue

        port = conn.laddr.port
        proto = PROTO_UDP if is_udp else PROTO_TCP
        key = (proto, port, pid) if merge_dual_stack else (proto, port, pid, conn.laddr.ip)

        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"proto": proto, "port": port, "pid": pid, "listen_addrs": []}
            buckets[key] = bucket

        addr = conn.laddr.ip
        if addr not in bucket["listen_addrs"]:
            bucket["listen_addrs"].append(addr)

    parent_map = build_ppid_map()
    proc_cache: dict[int, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []

    for bucket in buckets.values():
        pid = bucket["pid"]
        if pid not in proc_cache:
            proc_cache[pid] = collect_process(pid, parent_map)

        proc = proc_cache[pid]
        entries.append(
            {
                "port": bucket["port"],
                "proto": bucket["proto"],
                "hint": PORT_HINTS.get(bucket["port"]),
                "listen_addrs": sorted(bucket["listen_addrs"]),
                "pid": pid,
                "process": proc["name"],
                "exe": proc["exe"],
                "cmdline": proc["cmdline"],
                "cwd": proc["cwd"],
                "parent_pid": proc["parent_pid"],
                "started_at": proc["started_at"],
                "access": proc["access"],
            }
        )

    entries.sort(key=lambda e: (e["port"], e["proto"]))

    return {
        "schema": SCHEMA,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "host": platform.node(),
        "platform": platform.platform(),
        "is_admin": is_admin(),
        "stats": {
            "total": len(entries),
            "tcp": sum(1 for e in entries if e["proto"] == PROTO_TCP),
            "udp": sum(1 for e in entries if e["proto"] == PROTO_UDP),
            "access_full": sum(1 for e in entries if e["access"] == "full"),
            "access_partial": sum(1 for e in entries if e["access"] != "full"),
            "skipped_no_pid": no_pid,
        },
        "entries": entries,
    }


def render(snapshot: dict[str, Any]) -> str:
    """控制台摘要渲染。"""
    lines: list[str] = []
    stats = snapshot.get("stats", {})

    lines.append(
        f"portwatch @ {snapshot.get('collected_at')}  "
        f"admin={'yes' if snapshot.get('is_admin') else 'no'}"
    )
    lines.append(
        f"监听条目 {stats.get('total', 0)} 条 "
        f"(tcp {stats.get('tcp', 0)} / udp {stats.get('udp', 0)})  "
        f"归因完整 {stats.get('access_full', 0)} · 受限 {stats.get('access_partial', 0)}"
    )
    lines.append("")

    header = f"{'PORT':>6}  {'PROTO':<5} {'PID':>7}  {'PROCESS':<22} {'ACCESS':<8} CMDLINE"
    lines.append(header)
    lines.append("-" * len(header))

    for e in snapshot.get("entries", []):
        cmd = e.get("cmdline") or (e.get("exe") or "-")
        if len(cmd) > 72:
            cmd = cmd[:69] + "..."
        proc = (e.get("process") or "?")[:22]
        lines.append(
            f"{e['port']:>6}  {e['proto']:<5} {e['pid']:>7}  {proc:<22} {e['access']:<8} {cmd}"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="portwatch 端口占用采集器")
    parser.add_argument("--json", metavar="PATH", help="将快照写入 JSON 文件")
    parser.add_argument("--raw", action="store_true", help="不做 IPv4/IPv6 双栈合并")
    parser.add_argument("--tcp-only", action="store_true", help="仅采集 TCP 监听")
    args = parser.parse_args()

    snapshot = collect(merge_dual_stack=not args.raw, include_udp=not args.tcp_only)

    if snapshot.get("error"):
        print(snapshot["error"], file=sys.stderr)
        return 1

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        print(f"已写出快照: {args.json}\n", file=sys.stderr)

    print(render(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
