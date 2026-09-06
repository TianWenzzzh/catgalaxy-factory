# 喵星图工厂 · CatGalaxy Factory

把「校园猫咪星图」的手工复刻流程产品化：**上传名册 CSV + 照片 → 校验 → 注入星图模板 → 在线预览 → 一键打包出可离线运行的专属星图 HTML**。

任何学校的学生只要按《跨校复制包》填好 12 列名册、备好照片，就能在几分钟内得到一份属于自己学校的星图，无需再手改一行代码。

> 第一次在自己学校做？先读 [docs/跨校落地手册.md](docs/跨校落地手册.md)：从普查记录格式、名册列名别名、逐条问题码处置，到发布前自查清单，一条链路走完。

- 后端：Python 3.10+ / FastAPI（无数据库，文件系统 + 内存）
- 前端：原生 HTML/CSS/JS（Canvas 2D 星图渲染器）
- 依赖：fastapi、uvicorn、python-multipart、pillow（全部 < 100MB）

---

## 一、安装

```bash
cd D:\QoderProjects\catgalaxy-factory

# 方式 A：uv（推荐，本机已有）
uv venv
uv pip install -e ".[dev]"

# 方式 B：pip
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

## 二、启动

```bash
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

浏览器打开 <http://127.0.0.1:8010/> 即为控制台。健康检查：`GET /healthz`。

> 端口可自选。生成的星图通过 `/bundle/{项目}/out/{形态}/{HTML}` 提供，iframe 预览依赖该挂载点。

## 三、使用（网页流程）

| 步骤 | 操作 | 对应功能 |
|---|---|---|
| 1 | 填校名/副标题，点「新建项目」（也可从下拉切回历史项目） | — |
| 2 | 拖入或点选 **名册 CSV**；再拖入 **照片**（多选或 zip）；底图可选，不传会自动生成夜空底图 | F1 |
| 3 | 右栏自动出现 **校验报告**：错误/警告/提示分色，可按级别筛选，可下载 `校验报告.md` | F2 |
| 4 | 选打包形态（相对路径版 / 内嵌 base64 版），可勾选「排除低置信度」，点「生成星图」；下方 iframe 即时预览，可拖动缩放、点星看档案 | F3 + F4 |
| 5 | 点「下载 zip」得到 `{校名}-校园猫咪星图-{日期}.zip`；点「归并决策摘要」下载 F7 文档骨架 | F5 + F7 |

主干之外还有四张按需卡片，插在哪一步都行，改完**都要重新点「生成星图」**才会进产物：

| 卡片 | 干什么 | 对应功能 |
|---|---|---|
| 名册在线编辑 | 按「行号 + 列名」直接改单元格，回写 CSV 并自动重跑校验，不用再下载-改-上传 | F10 |
| 归并工作台 | 列出「同毛色 + 同区域 + 特征相似」的疑似重复组，人工判定同一只/不是/存疑并写理由；**不自动改名册**，判定理由进 F7 摘要 | F9 |
| 底图标定 | 上传校园平面图后拖星星到真实位置，坐标以 0~1 归一化存储，可一键退回算法推导；页脚会注明人工标定了几颗 | F8 |
| 星图主题 | 5 套深色预设、8 个可覆盖颜色、5 种系统字体栈、页脚署名（追加在出处之后）、校徽（自动缩到 256px 并保留透明区） | F11 |

另有独立入口 **F6 普查解析**：把 `文件名 | 猫数 | 毛色 | 特征 | 场景 | 画质 | 正脸 | 人脸` 格式的普查 batch 拖进去，直接得到名册草稿 CSV + 疑似重复归并建议。

### 两种打包形态怎么选

- **相对路径版**：`HTML + assets/photos/*.jpg + assets/map.jpg`，体积小、便于二次编辑，但必须整包解压后打开。
- **内嵌 base64 版**：照片与底图切成 `assets/photo-data-NN.js`（每片 ≤1.5MB）内嵌，HTML 双击即开、可直接上传比赛平台，体积约为照片总量的 1.37 倍。

## 四、命令行验收

```bash
.venv\Scripts\python scripts\acceptance.py --case demo2    # 空骨架 + 2 行示例，走通 F1-F5
.venv\Scripts\python scripts\acceptance.py --case full76   # 真实名册 76 只全量回归
.venv\Scripts\python scripts\acceptance.py --case all
```

脚本走真实 HTTP 接口，逐步打印实测结果，并把证据写到 `docs\验收证据-*.md`。任一场景失败退出码为 1。
`--case full76` 需要 E 盘（`E:\猫咪星图_总库`）挂载；缺失时会明确报出而不会静默跳过。

## 五、测试

```bash
.venv\Scripts\python -m pytest -q
```

8 个测试文件覆盖 CSV 解析、校验规则、星位映射、模板注入、打包、图片处理、普查解析与端到端 API。核心模块（校验 / 注入 / 打包）均有单测。

## 六、目录结构

```
catgalaxy-factory/
├─ SPEC.md                     规格：数据注入点契约、14 条校验规则、API、数据流图
├─ README.md                   本文件
├─ 交付报告.md                  完成清单 / 验收证据 / 已知限制 / 次日待办
├─ pyproject.toml              依赖与 pytest 配置
├─ conftest.py                 测试夹具：临时 workspace、造图、造 CSV
├─ app/
│  ├─ main.py                  FastAPI 路由 + generate_bundle 编排
│  ├─ config.py                路径与红线常量（长边 1200px / 单张 200KB / 分片 1.5MB）
│  ├─ models.py                Pydantic：CatRow / Issue / Summary / ValidationReport / ProjectMeta
│  ├─ csv_loader.py        F1  编码探测（utf-8-sig/gb18030/…）、列名别名、12 列解析
│  ├─ validate.py          F2  14 条校验规则 + 照片引用归一
│  ├─ star_mapper.py       F3  毛色→星色、照片数→星等、出没区→星位分区
│  ├─ injector.py          F3  模板 token 注入 + base64 分片
│  ├─ image_proc.py            照片压缩 / zip 解包（含中文文件名修复）/ 默认夜空底图
│  ├─ packager.py          F5  bundle 落盘 + zip
│  ├─ store.py                 文件系统即数据库：project.json + 操作日志
│  ├─ census_parser.py     F6  普查 batch → 名册草稿 + 归并建议
│  ├─ summary_writer.py    F7  归并决策摘要骨架
│  └─ templates/starmap.html   星图模板（Canvas 渲染器 + 注入 token）
├─ static/                     控制台前端：index.html / app.js / style.css
├─ scripts/acceptance.py       验收脚本
├─ tests/                      单元测试 + 端到端测试
├─ docs/                       验收证据（脚本产出）
└─ workspace/                  运行期数据（已 gitignore）
   └─ {project_id}/
      ├─ project.json          项目元数据 + 操作日志（F7 的数据源）
      ├─ roster.csv            名册原件（不改一字，保证可溯源）
      ├─ 校验报告.md  归并决策摘要.md
      ├─ assets/photos/*.jpg   压缩后的照片（长边≤1200px，单张≤200KB）
      ├─ assets/map.jpg        底图（上传的或自动生成的）
      ├─ out/{relative|inline}/  可直接预览的 bundle
      ├─ dist/*.zip            交付物
      └─ data/猫咪名册.csv      随包数据
```

## 七、数据契约

### 名册 CSV（12 列，列序不限，支持别名表头）

`编号,昵称,军衔,工位,毛色,特征描述,代表照片文件,照片数量,出没区域,关联照片编号,置信度,备注`

- `编号` 接受 `CAT-001 / CAT001 / cat_1` 等写法，统一归一为 `CAT-NNN`；弃用编号允许空缺（只提示，不拦截）。
- `代表照片文件` 允许写裸文件名或带目录前缀（如 `照片视频/xxx.jpg`），校验时按 basename 归一到实际入库文件名。
- 编码自动探测：utf-8-sig / utf-8 / gb18030 / gbk / big5 / latin-1。

### 注入星图的 CATS 条目（12 键，与参考实现一致）

`id, name, rank, title, coat, coatGroup, features, area, bio, photo, photoCount, brightness`

CSV 没有的字段由工具推导：

| 目标键 | 推导规则 |
|---|---|
| `coatGroup` / 星色 | 毛色关键词分类（三花 > 玳瑁 > 奶牛 > 纯白 > 纯黑 > 重点色 > 狸花 > 橘 > 其他） |
| `brightness` / 星等 | `min(0.5 + 0.11 × (照片数 − 1), 1.05)`，与中北版实测数值逐点对齐 |
| `area` / 星位 | 出没区域关键词归入 10 大分区，分区内黄金角螺旋布点 + 最小间距松弛，坐标确定性可复现 |
| `bio` | 只由军衔/职务/区域/特征/照片数拼装，**不虚构行为故事**（数据红线） |

## 八、API 速查

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 新建项目 `{school, subtitle}` |
| GET / DELETE | `/api/projects` · `/api/projects/{pid}` | 列表 / 详情 / 删除 |
| POST | `/api/projects/{pid}/roster` | 上传名册 CSV（自动跑一次校验） |
| POST | `/api/projects/{pid}/photos` | 上传照片（多选或 zip，自动压缩） |
| POST | `/api/projects/{pid}/map` | 上传底图（可选） |
| POST | `/api/projects/{pid}/validate` | 重跑校验，返回完整报告 |
| GET | `/api/projects/{pid}/report.md` | 校验报告 Markdown |
| POST | `/api/projects/{pid}/generate` | 生成 `{form, exclude_low_confidence}` |
| GET | `/api/projects/{pid}/preview?form=` | 预览地址 |
| GET | `/api/projects/{pid}/download?form=` | 下载 zip |
| GET | `/api/projects/{pid}/artifacts` | 工作区产物清单 |
| GET | `/api/projects/{pid}/progress` | 长任务进度（上传压缩 / 生成打包） |
| GET / PATCH | `/api/projects/{pid}/roster` | F10 读原始行与列映射 / 按「行号 + 列名」改单元格 |
| GET / PUT | `/api/projects/{pid}/merge` | F9 疑似重复候选组 / 记人工判定（判同猫或不同猫必须写理由） |
| DELETE | `/api/projects/{pid}/merge/{gid}` | F9 撤销某一组的判定 |
| GET / PUT / DELETE | `/api/projects/{pid}/calib` | F8 读星位标定 / 存人工坐标 / 退回算法推导 |
| GET | `/api/theme/options` | F11 预设、字体栈、可调颜色菜单 |
| GET / PUT / DELETE | `/api/projects/{pid}/theme` | F11 读主题（含服务端算好的 `effective_colors`）/ 局部改 / 恢复默认 |
| GET / POST / DELETE | `/api/projects/{pid}/logo` | F11 校徽原字节 / 上传（重编码为 ≤256px PNG）/ 删除 |
| POST | `/api/projects/{pid}/summary` | F7 归并决策摘要 |
| POST | `/api/census/parse` | F6 普查 batch 解析（`as_csv=true` 返回草稿 CSV） |
| GET | `/api/template` | 下载 12 列空名册模板 |
| GET | `/healthz` | 健康检查 |

## 九、数据红线（沿用中北版实测经验）

- 每只猫必须有照片证据链（`关联照片编号` 可溯源），置信度「低」建议直接不收。
- 不虚构行为故事；小传只写名册里能看到的内容。
- 不收录含人脸的照片（F6 解析时会自动告警）；不公布投喂人信息。
- 名册落在 `workspace/{pid}/roster.csv`。F10 在线编辑**会改写它**（原子替换，每处改动带旧值/新值进操作日志）；要留一份完全未动的原件，请自己另存，或改用「本地改 CSV → 重新上传」。

已知限制与次日待办见 [交付报告.md](交付报告.md)。
