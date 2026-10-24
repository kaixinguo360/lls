# LLS

基于 PTY 的终端包装器：VT100 屏幕模型 + 覆盖层界面。

> 当前处于重构中。旧实现见 [`legacy/`](legacy/)，新实现见 [`src/lls/`](src/lls/)。

## 结构

```
src/lls/session/    终端引擎与它对外的能力接口
src/lls/handlers.py 输入处理器（透传 / 命令分发）
src/lls/commands/   命令层，按类别分模块
src/lls/term/       VT100 屏幕模型（pyte 适配）
src/lls/ui/         显示原语（换行、覆盖层定位、按模型渲染）
src/lls/edit/       行编辑缓冲
src/lls/tty/        PTY 与平台终端后端
```

引擎不认识任何命令：主循环只反复调用当前处理器的 `step()`。新增一种输入
方式 = 新增一个处理器；新增一条命令 = 加一个带 `@command` 的函数。

## 开发

```bash
# 运行测试（需先 pip install -e ".[dev]"）
python -m pytest

# 语料回放
python tools/replay.py replay <corpus.raw>
python tools/replay.py compare <corpus.raw>
```

详见 [docs/](docs/)（待补）与 [tests/corpora/README.md](tests/corpora/README.md)。
