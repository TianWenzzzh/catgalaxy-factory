# CatGalaxy Factory · 开发约定（给编码 agent 与新会话）

把「校园猫咪星图」手工流程产品化的**批量生成器**：FastAPI + Pillow，**无数据库**，
`workspace/{pid}/` 即状态（"文件系统即数据库"）。产物是**自包含单文件 HTML 星图**。
现役渲染引擎 = **v29**（env `STARMAP_ENGINE`，默认 v29；单次请求可用 `GenerateRequest.engine` 覆盖）。
`app/templates/starmap.html` 是 v1 旧模板，`app/config.py` 标了 **deprecated，保留一个版本周期后移除** ——
新代码不要接它，也不要"顺手统一"。

## 跑起来 / 自证（全部仓内相对路径）

```bash
uv venv && uv pip install -e ".[dev]"          # 或既有的本机 venv；不要用可移动盘上的 venv
python -m pytest -q                            # 基线 747 条（`[tool.pytest] addopts` 已是 -q）
python scripts/acceptance.py --case demo2      # 冒烟级验收（另有 --case full76 / all）
uv run --python 3.12 --with playwright --with pillow python scripts/smoke_v29.py [--port 8152]
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010   # 端口自选；健康检查 GET /healthz
```

- **pytest 用例数 = 747**（25 个测试文件，`--collect-only -q` 实测）。README 里"726/730 条、23 个文件"
  和 `第三轮冲刺报告.md` 的数字都是**历史快照，已过期**，别拿它们当基线，也别为"对齐文档"去改测试。
- **真实数据用例不需要 marker**：`conftest.py` 靠"路径不存在自动跳过"。根目录由 env `STARMAP_HOME`
  决定（缺省回落到 conftest 里写死的那台机器的总库路径）。**换机 / 盘不在位时这些用例优雅跳过，
  不是失败 —— 别去改 conftest 的缺省路径来"修好"它。**
- `pyproject.toml` 的 `version` 与 `app/main.py` 里 FastAPI 的 `version=` **都落后于 git tag**
  （实测 1.1.0 / 1.0.0，而 HEAD 已有 tag v1.3.2）。**别顺手"统一版本号"** —— 版本真相是 tag，
  要改就一次性把三处一起改并在 CHANGELOG 补条目（v1.2.0→v1.3.2 目前**没有** CHANGELOG 条目）。

## 🔴 渲染真相单源（这个仓最容易毁掉的东西）

`app/starmap_render.py` 是星图渲染的**唯一上游**。另外两个仓围绕它形成一条链：

- 开源模板仓（`TianWenzzzh/meow-starmap`）里的 `tools/starmap_render.py` 是本仓渲染器的
  **vendor 快照**，由**那个仓里的** `tests/test_vendor_snapshot.py` 用**钉死的 sha256** 锁住；
- 产物仓（`TianWenzzzh/nuc-cat-starmap`）的 HTML 由本仓 `scripts/export_v29_product.py`
  从模板仓的数据包**直出**（所以产物与模板托管逐字节一致不是巧合，是这条链的性质）。

**发版顺序不可颠倒：工厂（改上游）→ 模板仓（同步快照 + 更新其 PINNED_SHA256 与文件头基线）→ 产物仓（直出）。**

- ⚠️ **绝不允许为了让 sha256 测试变绿而放宽/改钉值**。那个测试失败意味着"渲染真相被改动了"。
  正解只有两条：要么这次改动本就该升上游 → 按上面顺序走完整链；要么改动不该动渲染器 → 回退改动。
- 本仓内的交叉校验工具：`python scripts/parity_nuc_package.py`（退出码 0 = 工厂渲染器与模板仓
  构建器对同一数据包**逐字节一致**）。改过渲染器或模板后跑它，比跑单测更能证明没破契约。
- `app/templates/starmap_v29.html`（约 214 KB）与模板仓的 `template/starmap.html` **靠 sha256 手工同步**
  （本仓没有本地生成器；代际生成脚本在模板仓那边）。标签 v1.2.0→v1.3.0 的说明就是"模板同步 16 仓"。
  它文件头那段带 `__PRODUCT__`/`__VERSION__` token 的是**产品签名块，不是"自动生成请勿手改"标记** ——
  别把它当生成物删掉重建。

## 隐性契约与红线（改之前先看这条）

- **`/bundle` 挂载点**：`app/main.py` 里 `app.mount("/bundle", StaticFiles(directory=workspace))`，
  预览 URL 是 `/bundle/{pid}/out/{form}/{html}`。**iframe 预览依赖这个挂载点**，改路径=线上预览全黑。
- **`scripts/cleanup.py` 的红线**：只认项目内的 `workspace/`，路径不在项目目录下**直接拒绝执行**；
  `--before` 默认 dry-run，`--apply` 才动手；`--exclude` 名单不匹配会中止 `--apply`。**别放宽这些守卫。**
- **阈值不许"调松让测试过"**：`app/phash.py` 的 `BLUR_RADIUS = 2.0`、`MIN_CONTRAST`（无结构返回 `None`）、
  `app/merge.py` 的 `MAX_GROUP_SIZE = 5` / `MIN_SIMILARITY = 0.12` —— 注释里写着实测取舍依据。
  `scripts/compare_full76_v29.py` 的 CALIB 逐键坐标 **≤0.002 是硬门禁**。
- **`GENERATE_STAGES = 6`**（`app/main.py`）与前端进度条**强耦合**，"对不上就会条子走到 83% 就停"，
  `tests/test_progress.py` 盯这个数。
- **CI 有元测试**：`tests/test_ci.py` 会断言 CI 里**不出现** `full76` / `--case all`（那是本机重活，
  不进 CI），并断言存在 Linux 浏览器冒烟 job。**改 CI 前先跑这个文件**，别删断言。
- **数据红线**：`workspace/`、照片、名册、`dist/*.zip` 都在 `.gitignore` 里，`git ls-files` 现在**不含**
  任何真实照片/名册（只有 `docs/screenshots-f3/` 的证据 PNG）。**永远不要把真实普查数据、照片、
  学校名册提交进这个仓 —— 它是公开的。** 上传限额（200MB / zip 解压 1GB / 2 万条 / 深度 3）在
  `app/config.py`，`LOGO_EXTS` 故意不含 `.svg`（"SVG 能带 `<script>`"），别"顺手支持一下"。

## 已知过期/待办（发现它们别当成 bug 去"顺手修"）

- README §一「730 条」、§五「23 个测试文件、726 条」、§末「当前仓库还没有配置 git 远端」三处**已失效**
  （远端 `origin` 存在且两条分支都已推送）。CHANGELOG 缺 v1.2.0→v1.3.2。
- `SPEC.md` 头部还写着「版本 v1.0 · 2026-09-06」，正文 F1–F7 仍是有效契约。
- 模板仓那份快照的基线标注停在比本仓 HEAD 低一个 tag 的位置 —— **下次升上游时把它一并推进**。

## 文档地图

`README.md`（§六 目录树、§八 API 速查、§九 数据红线）· `SPEC.md`（F1–F7 细则与红线）·
`docs/跨校落地手册.md`（换一所学校怎么做，每条都对应代码真实行为）·
`docs/验收证据-*.md`（F3 双形态冒烟 / F5 full76 逐字段比对 / F5 数据包字节对齐 / 浏览器实测）·
`第三轮冲刺报告.md`（README 指定的"当前状态"出处，但**数字是历史快照**）。
