"""``python -m lls.recording``：不经过 LLS 入口也能列出/回放/导出。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
