"""终端语料库。

录制方法（POSIX）：:

    script -q -c 'vim /tmp/x' tests/corpora/vim.raw

``script`` 会把程序的完整输出（含所有 ANSI 转义序列与退出序列）落成原始字节流，
这正是屏幕模型需要吃的东西。交互程序（vim/less/top）优先，因为它们的转义序列最丰富。

语料用途：

* ``tests/golden/test_parity.py`` 用它们做 pyte / legacy 对拍与分块不变性测试；
* ``python tools/replay.py replay <file>.raw`` 手动查看某语料的最终屏幕；
* 自研 VT 内核落地后，用同一批语料做差分测试。

目前语料尚未录制（需在 POSIX 环境跑 ``script``）。本文件只定义清单。
"""

# 计划录制的语料（供 CI 或手工补充）：
CORPORA = [
    ("basic", "printf 与简单文本输出"),
    ("colors", "彩色输出（git log --color、ls --color）"),
    ("cursor", "光标移动与绝对定位"),
    ("wide", "宽字符/中文/emoji（光标宽度）"),
    ("altbuf", "vim / less 的备用缓冲区进出"),
    ("scroll", "大量输出触发回滚"),
    ("clear", "清屏与 keep_logs 语义"),
]
