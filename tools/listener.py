#!/usr/bin/env python3
"""起一个真实监听进程，用于端到端验证采集链路（见 HANDOVER.md 附录 C）。

用法：
    python tools/listener.py 8899            # 监听 127.0.0.1:8899，默认持续 60 秒
    python tools/listener.py 8899 300        # 持续 300 秒

验证步骤：
    1) 运行本脚本 → 页面应在 1~2 个采样周期内出现该端口，右侧时间线出现 added
    2) 按 Ctrl+C（或等待超时）结束 → 时间线出现 removed，历史库区间闭合
       （end_reason = observed_removed）
    3) 排障：python history.py --dump | findstr 8899

注意：Windows 上刻意不设 SO_REUSEADDR —— 它允许绑定已被占用的端口，会把
「端口冲突」伪装成「监听成功」，让验证结果失真。端口被占用时本脚本直接报错退出。
"""

from __future__ import annotations

import socket
import sys
import time

DEFAULT_SECONDS = 60


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    port = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SECONDS

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        print(f"无法监听 127.0.0.1:{port} —— {exc}（该端口可能已被占用）", file=sys.stderr)
        return 1
    sock.listen(1)
    print(f"listening on 127.0.0.1:{port} for {seconds:g}s (Ctrl+C 提前结束)", flush=True)

    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print("closed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
