# LLS

AI 增强的 shell 终端包装器：PTY 代理 + VT100 内核 + LLM 命令生成。

> 当前处于重构中。旧实现见 [`legacy/`](legacy/)，新实现见 [`src/lls/`](src/lls/)。

## 状态

- [x] P0 骨架（pyproject + src 包 + 工具链）
- [x] P1 相机（快照 / 回放 / golden 对拍测试）
- [ ] P2 VT 内核（pyte 适配器已就绪，自研状态机待做）
- [ ] P3 运行时（asyncio 事件循环 + PTY 适配器）
- [ ] P4 渲染与编辑（底部临时覆盖行 + 差量渲染）
- [ ] P5 命令与配置（Effect 协议 + TOML 配置）
- [ ] P6 AI 层（Context/Role/Session/Transport）
- [ ] P7 收尾（文档 / doctor / replay）

## 开发

```bash
# 运行测试（需先 pip install -e ".[dev]"）
python -m pytest

# 语料回放
python tools/replay.py replay <corpus.raw>
python tools/replay.py compare <corpus.raw>
```

详见 [docs/](docs/)（待补）与 [tests/corpora/README.md](tests/corpora/README.md)。
