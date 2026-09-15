"""让测试能直接 `import collector / server / history`（三个模块都在仓库根）。

pytest 默认只把 tests/ 目录加进 sys.path，这里显式把仓库根插到最前面。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
