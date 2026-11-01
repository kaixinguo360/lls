"""离线学习：只读已落盘的 v2 会话，产出可训练的数据集。

这里不连接 provider、不执行工具、不改会话；``lls-learn export`` 是唯一入口，
训练器留在仓库外。
"""

from __future__ import annotations

__all__ = ["cli", "dataset"]
