# 喵星图工厂 CatGalaxy Factory · 规格说明书（SPEC）

版本 v1.0 · 2026-09-06 · spec-driven 首版（无人值守，写完即实现）

## 0. 一句话定义

学生上传【猫咪名册 CSV + 照片】→ 自动校验 → 注入星图模板 → 浏览器预览 →
一键打包出**可离线运行**的专属校园猫咪星图（两种形态：相对路径版 / base64 纯文本版）。

## 1. 参考实现分析（只读，来自 E 盘总库）

从 `05_git仓库_最新v2.6/中北喵星图.html`（3336 行 / 230KB）中提取出的**数据注入点**：

| 注入点 | 形态 | 说明 |
|---|---|---|
| `const CATS = [...]` | JS 对象数组 | 每只猫一条，星图的全部数据来源 |
| `const CALIB = {...}` | `{"CAT-001":{x,y}}` | 归一化坐标（0-1），语义聚簇落位 |
| `const MAP_SRC = "..."` | 字符串 | 底图文件名 |
| `<title>` / `.sub` / `.stats` | HTML 文本 | 校名相关文案 |
| `window.__PHOTOS[文件名]="data:image/jpeg;base64,..."` | 分片 JS | 纯文本版的照片内嵌载体 |
| `function PH(p)` | JS | 运行时把相对路径解析成 base64 |

**CATS 单条契约（12 个键）**：

```js
{ id, name, rank, title, coat, coatGroup, features, area, bio, photo, photoCount, brightness }
```

**CSV 只有 12 列，CATS 需要 12 键，映射关系**：

| CATS 键 | 来源 | 推导规则 |
|---|---|---|
| id | 编号 | 直取 |
| name | 昵称 | 直取 |
| rank | 军衔 | 直取 |
| title | 工位 | 直取 |
| coat | 毛色 | 直取 |
| coatGroup | 毛色 | **推导**：关键词匹配 → 橘/狸花/三花/纯白/纯黑/奶牛/玳瑁/重点色/其他 |
| features | 特征描述 | 空格分隔 → `、` 连接 |
| area | 出没区域 | 直取 |
| bio | 特征描述+工位+军衔+出没区域 | **合成**：模板化小传（照片里看得到的事实，不虚构行为故事） |
| photo | 代表照片文件 | `assets/photos/{文件名}` |
| photoCount | 照片数量 | int |
| brightness | 照片数量 | **推导**：`min(0.5 + 0.11*(n-1), 1.05)`，与中北版实测值逐项对齐 |

> 实测对齐验证（中北版）：n=1→0.50, 2→0.61, 3→0.72, 4→0.83, 5→0.94, 8→1.05 ✔

CSV 中的 `关联照片编号 / 置信度 / 备注` 不进星图，进**校验报告**与**数据包溯源清单**。

## 2. 数据流图

```
┌──────────┐   POST /api/projects/{pid}/roster     ┌─────────────┐
│ 名册 CSV │──────────────────────────────────────▶│  csv_loader │
└──────────┘                                       └──────┬──────┘
                                                          │ 12 列 → RowModel[]
┌──────────┐   POST /api/projects/{pid}/photos            ▼
│ 照片(多选)│──────────────────────────────▶┌──────────────────────┐
│ 照片(zip)│                               │  validate_roster     │ F2
└──────────┘                               │  · 列完整性          │
┌──────────┐   POST /api/projects/{pid}/map│  · 照片名 ↔ 名册匹配 │
│ 底图(可选)│──────────────────────┐       │  · 编号连续性(允许空缺)│
└──────────┘                      │       │  · 置信度=低 警告清单 │
                                   │       │  · 重复编号/重复照片名 │
                                   │       └──────┬──────────┬────┘
                                   │              │报告 JSON │通过行
                                   ▼              ▼          ▼
                            ┌────────────┐  ┌──────────┐ ┌──────────────┐
                            │ image_proc │  │ 前端报告 │ │ star_mapper  │ F3
                            │ 压缩 ≤1200 │  │   页面   │ │ coat→色      │
                            │ ≤200KB     │  └──────────┘ │ count→星等   │
                            └─────┬──────┘                │ area→星区分区│
                                  │                       │ id→CALIB坐标 │
                                  ▼                       │ →bio 合成    │
                          workspace/{pid}/assets/photos/  └──────┬───────┘
                                                                 ▼
                                              ┌────────────────────────────┐
                                              │  template_injector         │
                                              │  starmap_template.html     │
                                              │   + CATS + CALIB + 校名    │
                                              │   + __PHOTOS(base64 分片)  │
                                              └──────────┬─────────────────┘
                                                         ▼
                              GET /api/projects/{pid}/preview  ── F4 iframe 在线预览
                                                         ▼
                                              ┌────────────────────────┐
                                              │  packager              │ F5
                                              │  {校名}-校园猫咪星图-  │
                                              │  {YYYY-MM-DD}.zip      │
                                              └────────────────────────┘

旁路（富余功能）：
  普查 batch txt ──▶ census_parser (F6) ──▶ 名册草稿 CSV
  项目操作记录  ──▶ summary_writer (F7) ──▶ 归并决策摘要.md
```

## 3. 模块划分

```
catgalaxy-factory/
├── SPEC.md                    # 本文件
├── README.md                  # 安装/使用/目录说明
├── 交付报告.md                 # 完成清单+验收证据+限制+待办
├── pyproject.toml             # uv 管理依赖
├── app/
│   ├── main.py                # FastAPI 入口 + 路由 + 静态托管
│   ├── config.py              # 路径常量、WORKSPACE 根
│   ├── models.py              # Pydantic：CatRow / Issue / ValidationReport / ProjectMeta
│   ├── store.py               # 文件系统"数据库"：项目 CRUD（无 DB）
│   ├── csv_loader.py          # F1 CSV 解析（BOM/GBK/UTF-8 自动识别、12 列）
│   ├── validate.py            # F2 校验引擎 → ValidationReport
│   ├── star_mapper.py         # F3 coat→星色、count→星等、area→分区、bio 合成
│   ├── image_proc.py          # 照片压缩：长边≤1200px、≤200KB；zip 解包
│   ├── census_parser.py       # F6 普查 batch txt → 名册草稿
│   ├── summary_writer.py      # F7 归并决策摘要骨架
│   ├── injector.py            # F3 模板注入（CATS/CALIB/文案/__PHOTOS）
│   ├── packager.py            # F5 打包 zip（relative / inline 两形态）
│   └── templates/
│       └── starmap.html       # 自研星图渲染模板（含注入占位符）
├── static/
│   ├── index.html             # 工厂控制台（上传/报告/预览/打包）
│   ├── app.js
│   └── style.css
├── tests/
│   ├── conftest.py
│   ├── test_csv_loader.py
│   ├── test_validate.py
│   ├── test_star_mapper.py
│   ├── test_injector.py
│   ├── test_packager.py
│   ├── test_image_proc.py
│   ├── test_census_parser.py
│   └── test_api_e2e.py        # F1-F5 全链路
├── scripts/
│   └── acceptance.py          # 验收脚本：2行示例 + 76只全量回归
└── workspace/                 # 运行时产物（.gitignore）
    └── {project_id}/
        ├── project.json
        ├── roster.csv
        ├── assets/photos/*.jpg
        ├── assets/map.jpg
        ├── report.json
        ├── out/{校名}喵星图.html
        └── dist/*.zip
```

## 4. 功能规格细则

### F1 数据导入
- 名册 CSV：12 列，表头必须完全匹配（顺序无关，按列名取）。编码自动探测 `utf-8-sig / utf-8 / gbk`。
- 照片：`multipart` 多文件上传，或单个 `.zip`（自动解包，忽略 `__MACOSX/`、目录项、非图片）。
- 底图：可选上传（jpg/png）。未上传时**自动生成**程序化夜空底图（Pillow 绘制星野 + 校园区块网格），保证零素材也能出图。
- 幂等：同名文件覆盖；重复调用不产生脏数据。

### F2 校验与报告
`ValidationReport = { summary:{total,passed,errors,warnings}, issues:[{level,code,row,field,message}], rows:[...] }`

| 规则 | 级别 | code |
|---|---|---|
| 缺列 / 多列 | error | `E_MISSING_COLUMN` |
| 编号为空或格式非 `CAT-\d+` | error | `E_BAD_ID` |
| 编号重复 | error | `E_DUP_ID` |
| 昵称/毛色/代表照片为空 | error | `E_REQUIRED_FIELD` |
| 代表照片在已上传集合中不存在 | error | `E_PHOTO_MISSING` |
| 同一张照片被多只猫引用 | warning | `W_PHOTO_SHARED` |
| 上传了但名册未引用的照片 | info | `I_PHOTO_UNUSED` |
| 编号连续性：`CAT-001..CAT-max` 中的空缺 | info | `I_ID_GAP`（弃用编号允许空缺，只提示不拦截） |
| 照片数量非正整数 | warning | `W_BAD_COUNT` |
| 照片数量与实际上传数不符 | info | `I_COUNT_MISMATCH` |
| 置信度 = 低 | warning | `W_LOW_CONFIDENCE`（进警告清单，**默认仍生成**，可在 UI 勾选排除） |
| 置信度非 高/中/低 | warning | `W_BAD_CONFIDENCE` |
| 特征描述过短（<4 字） | info | `I_THIN_FEATURE` |

判定：`errors == 0` → 可生成；否则拒绝并返回报告。

### F3 星图生成
- **毛色 → 星色**（`coatGroup` → RGB，与中北版色系一致）：
  橘 `255,190,110` / 狸花 `168,190,220` / 三花 `255,170,200` / 纯白 `235,245,255` /
  纯黑 `150,160,200` / 奶牛 `225,235,250` / 玳瑁 `210,160,120` / 重点色 `190,200,235` / 其他 `200,200,220`
- **照片数 → 星等**：`brightness = min(0.5 + 0.11*(n-1), 1.05)`；渲染半径 `r = 2.2 + 3.4*brightness`。
- **出没区 → 星位分区**：按 `area` 文本分组（稳定排序）→ 每组分配一个**扇区/网格区块**，
  组内用 `hash(id)` 种子随机散开并做最小间距松弛（避免重叠），全部落在核心区 `x∈[0.10,0.90], y∈[0.12,0.88]`。
  分区锚点由 area 关键词优先命中（宿舍/教学/食堂/道路/草地/车库/围墙…），未命中则均匀环形分配。
- **bio 合成**：只用 CSV 里的事实字段拼装（军衔+工位+出没区域+特征），不虚构行为故事。
  例：`{name}，{rank}，{title}。常驻{area}。{features}。`
- **两种打包形态**：
  - `relative`：HTML + `assets/photos/*.jpg`（+ `assets/map.jpg`），体积小，需保持目录结构。
  - `inline`：照片全部 base64 内嵌进 `assets/photo-data-NN.js` 分片（每片 ≤1.5MB），
    HTML 里保留 `PH()` 解析函数，双击即开、可单文件传播。

### F4 在线预览
- `GET /api/projects/{pid}/preview?form=relative|inline` 返回渲染后的 HTML（`text/html`）。
- 前端 `<iframe>` 直接嵌入；相对路径版的图片由 `/api/projects/{pid}/file/{path}` 提供。
- 预览不写盘（inline 版会写 `out/`，因为分片 JS 需要落盘才能被引用）。

### F5 一键打包
- `GET /api/projects/{pid}/download?form=relative|inline`
- 文件名：`{校名}-校园猫咪星图-{YYYY-MM-DD}.zip`（校名做文件名安全化）。
- zip 内结构（relative）：`{校名}喵星图.html` + `assets/photos/…` + `assets/map.jpg` + `data/猫咪名册.csv` + `校验报告.md`
- zip 内结构（inline）：`{校名}喵星图.html` + `assets/photo-data-NN.js` + `data/…` + `校验报告.md`

### F6 普查 batch 解析器
输入行：`文件名 | 猫数 | 毛色 | 特征 | 场景 | 画质 | 正脸 | 人脸`（首行标题与空行跳过）。
输出：名册草稿 CSV（12 列）——
- 编号：`CAT-NNN` 顺序分配（每张照片一行，`猫数>1` 记入备注待人工归并）
- 昵称：留空（待人工命名）；军衔/工位：留空
- 毛色：取 `|` 第 3 段括号前的主色词；特征描述：第 4 段
- 代表照片文件：第 1 段；照片数量：第 2 段
- 出没区域：第 5 段截取场景关键词；关联照片编号：`batch{N}:{文件名前4位}`
- 置信度：画质 A→高 / B→中 / C→低；备注：`正脸={是/否} 人脸={无/有} 待归并`
- 提供"疑似同猫归并建议"：毛色主词 + 场景关键词相同的行聚成候选组。

### F7 归并决策摘要生成器
读取 `project.json` 的操作日志（上传时间、校验轮次、排除的低置信度行、共享照片的猫、编号空缺），
输出 `归并决策摘要.md` 骨架：待确认清单 / 已排除清单 / 编号空缺说明 / 数据红线自检。

## 5. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 创建项目 `{school, subtitle?, motto?}` → `{id}` |
| GET | `/api/projects` | 项目列表 |
| GET | `/api/projects/{pid}` | 项目详情 |
| POST | `/api/projects/{pid}/roster` | 上传名册 CSV（`file`） |
| POST | `/api/projects/{pid}/photos` | 上传照片（`files[]` 或单个 zip） |
| POST | `/api/projects/{pid}/map` | 上传底图（可选） |
| POST | `/api/projects/{pid}/validate` | 运行校验 → `ValidationReport` |
| POST | `/api/projects/{pid}/generate` | 生成星图 `{form, exclude_low_confidence}` |
| GET | `/api/projects/{pid}/preview` | 预览 HTML（F4） |
| GET | `/api/projects/{pid}/file/{path}` | 预览用素材 |
| GET | `/api/projects/{pid}/download` | 下载 zip（F5） |
| POST | `/api/census/parse` | F6 batch txt → 草稿 CSV |
| POST | `/api/projects/{pid}/summary` | F7 生成归并决策摘要 |
| GET | `/api/template` | 下载空白名册模板 CSV |

## 6. 技术选型与决策记录

| 项 | 选择 | 理由 |
|---|---|---|
| 后端 | Python 3.12 + FastAPI + uvicorn（uv 管理） | 本机已有 uv/Pillow；async 上传方便 |
| 前端 | 原生 HTML/CSS/JS，无构建 | 零依赖、离线可用、改起来快 |
| 存储 | 文件系统 `workspace/{pid}/`，无 DB | 规格要求 |
| 星图模板 | **自研** `app/templates/starmap.html`（Canvas 渲染） | 规格明确"不要复制整文件"；只对齐数据契约与视觉语言（深空底 + 金色主调 + 星等/星色/环绕光点） |
| UI 风格 | 深空蓝黑 `#070c1c` + 星金 `#ffd76a` + 青绿 `#7de8d8` | 与参考实现同一视觉家族 |
| 底图缺省 | 程序化生成星野底图 | 保证"只上传 CSV+照片"也能出完整成品 |
| 照片压缩 | Pillow：长边 1200、JPEG 质量二分逼近 200KB | 规格硬指标 |

## 7. 验收标准映射

| 验收项 | 执行方式 |
|---|---|
| 1. 空模板 + 2 行示例走通 F1-F5 | `scripts/acceptance.py --case demo2` |
| 2. 76 只全量回归，数据完整、照片全可显示 | `scripts/acceptance.py --case full76`（用 E 盘真实名册 + 合成照片桩，只读 E 盘） |
| 3. 单测全绿 + README | `pytest -q` |
| 4. 交付报告.md | 人工撰写 + 自动嵌入测试输出 |

## 8. 红线

- E 盘（U 盘）全程只读，绝不写入。
- 不碰 `D:\code`、`D:\校园基米skill`。
- 所有产出只写 `D:\QoderProjects\catgalaxy-factory\`。
- 不联网下载 >100MB 依赖（实际依赖约 30MB）。
- 不部署公网。
