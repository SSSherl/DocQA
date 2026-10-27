# 贡献指南

感谢你愿意花时间。这个项目面向个人本地使用，代码风格偏保守：**先能解释清楚，再谈聪明**。

## 开发环境

```bash
git clone https://github.com/SSSherl/DocQA.git
cd DocQA

python3 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
cp config.example.json config.json

.venv/bin/python -m pytest -q
.venv/bin/python -m docqa serve
```

Windows 用 `.\.venv\Scripts\python.exe` 代替 `.venv/bin/python`。

## 提交前请自查

**不要提交这些内容**——它们已经在 `.gitignore` 里，但请再确认一次：

- `config.json`（你的真实配置）
- `data/`（原文、索引、解析产物、会话记录）
- 任何 `.env`、`*.dpapi`、`*.sqlite3`、密钥或截图中的个人信息
- 虚拟环境、缓存、打包产物

提交前可以跑一次：

```bash
git status --short
git diff --cached --stat
```

如果发现自己误提交了密钥，**先到服务商控制台吊销**，再改写历史；只删文件是不够的。

## 代码约定

- **不引入不必要的依赖**。切词、BM25、RRF 都是手写的，原因就是少一个依赖少一个部署障碍。加依赖请在 PR 里说明为什么现有方案不够。
- **失败要说清楚**。检索不到、模型不可用、引用越界，都要有明确的用户可见提示，不允许静默降级成"看起来成功"。
- **不要伪造定位**。匹配不到原文行就只定位到页，并写明原因。
- **注释解释「为什么」**，不复述「做了什么」。
- 中文注释和中文界面文案是刻意选择，保持即可。

## 测试

```bash
python -m pytest -q                    # 全量
python -m pytest tests/test_http.py -q # 单文件
```

新增行为请补测试。涉及界面的改动，可以额外跑 `scripts/verify_*_ui.py`（需要 Chrome）。

## PR

1. 一个 PR 解决一件事，标题写结论（例：`修复：Word 文档引用定位退化到标题级`）。
2. 描述里写清：改了什么、为什么、怎么验证的。
3. 涉及行为变更的，同步更新 `README.md` 或 `docs/` 下对应文档。

## Issue

提问请附上：系统与 Python 版本、复现步骤、报错原文（**记得脱敏**）、以及 `python -m docqa doctor` 的输出。

## License

提交即表示同意以 [MIT](LICENSE) 协议发布你的贡献。
