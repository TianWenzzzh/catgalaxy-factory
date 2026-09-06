"""F7 · 归并决策摘要生成器：按项目操作记录自动产出摘要文档骨架。"""
from __future__ import annotations

from datetime import date

from .models import MergeBook, ProjectMeta, ValidationReport
from .star_mapper import coat_stats, id_gaps, zone_stats

VERDICT_LABEL = {"same": "同一只猫", "different": "不是同一只", "unsure": "存疑待复核"}


def build_summary(meta: ProjectMeta, report: ValidationReport | None = None,
                  generated: dict | None = None,
                  merge: MergeBook | None = None) -> str:
    """输出 Markdown 摘要骨架（对齐 12_跨校复制包 的《归并决策摘要》体例）。"""
    today = date.today().isoformat()
    lines: list[str] = [
        f"# {meta.school} · 归并决策摘要",
        "",
        f"> 由「喵星图工厂 CatGalaxy Factory」自动生成骨架 · {today}",
        "> 填写原则：**宁缺毋滥**——疑似重复、画质不够、身份存疑的一律不收，每条取舍都要写下理由。",
        "",
        "## 一、数据总览",
        "",
    ]

    if report:
        s = report.summary
        lines += [
            f"- 名册数据行：**{s.total_rows}** 行",
            f"- 可入星图：**{s.valid_rows}** 行",
            f"- 错误 / 警告 / 提示：**{s.error_count} / {s.warning_count} / {s.info_count}**",
            f"- 校验结论：{'通过' if s.ok else '未通过（存在错误）'}",
            f"- 照片：上传 {s.photos_uploaded} 张，引用 {s.photos_referenced} 张，"
            f"未使用 {s.photos_unused} 张",
            f"- 编号范围：{s.id_min or '—'} ~ {s.id_max or '—'}",
            "",
        ]
        rows = report.rows
        if rows:
            lines += ["### 毛色分组分布", ""]
            for k, v in coat_stats(rows).items():
                lines.append(f"- {k}：{v} 只")
            lines += ["", "### 出没分区分布", ""]
            for k, v in zone_stats(rows).items():
                lines.append(f"- {k}：{v} 只")
            lines.append("")
    else:
        lines += ["- 尚未运行校验。", ""]

    lines += ["## 二、编号空缺说明（弃用编号留空不复用）", ""]
    gaps = report.summary.id_gaps if report else []
    if gaps:
        for g in gaps:
            lines.append(f"- `{g}`：__________（请写明弃用理由，如重复建档/画质不足/身份存疑）")
    else:
        lines.append("- 无空缺，编号连续。")
    lines.append("")

    lines += ["## 三、低置信度待确认清单", ""]
    low = []
    if report:
        low = [r for r in report.rows if (r.confidence or "").strip() == "低"]
    if low:
        lines += ["| 编号 | 昵称 | 毛色 | 出没区域 | 关联照片 | 处置决定（收/弃） | 理由 |",
                  "|---|---|---|---|---|---|---|"]
        for r in low:
            lines.append(f"| {r.id} | {r.name} | {r.coat} | {r.area} | {r.related} |  |  |")
    else:
        lines.append("- 无低置信度行。")
    lines.append("")

    mid = [r for r in (report.rows if report else []) if (r.confidence or "").strip() == "中"]
    lines += ["## 四、中置信度合并推断（需写明依据）", ""]
    if mid:
        lines += ["| 编号 | 昵称 | 关联照片编号 | 推断依据 | 是否已实地复核 |",
                  "|---|---|---|---|---|"]
        for r in mid:
            lines.append(f"| {r.id} | {r.name} | {r.related} | {r.note or ''} |  |")
    else:
        lines.append("- 无中置信度行。")
    lines.append("")

    shared = []
    if report:
        shared = [i for i in report.issues if i.code == "W_PHOTO_SHARED"]
    lines += ["## 五、共用照片的疑似重复建档", ""]
    if shared:
        for i in shared:
            lines.append(f"- 行 {i.line}（{i.cat_id or '—'}）：{i.message}")
    else:
        lines.append("- 未发现同一张代表照片被多只猫引用。")
    lines.append("")

    lines += ["## 六、归并判定记录（人工留痕）", ""]
    decisions = sorted(merge.decisions.values(), key=lambda d: d.updated_at) if merge else []
    if decisions:
        lines += ["| 候选组 | 类型 | 判定 | 保留 | 弃用 | 理由 |",
                  "|---|---|---|---|---|---|"]
        for d in decisions:
            lines.append(
                f"| `{d.gid}` | {'共用照片' if d.kind == 'same-photo' else '同色同区'} | "
                f"{VERDICT_LABEL.get(d.verdict, d.verdict)} | {d.keep or '—'} | "
                f"{', '.join(d.drop) or '—'} | {d.reason or '—'} |")
        lines += ["", f"> 共 {len(decisions)} 条判定。判定只记录取舍与理由，"
                      f"编号的实际合并/弃用请在名册 CSV 里落笔，弃用编号留空不复用。"]
    else:
        lines.append("- 暂无判定。归并工作台（F9）里对每个候选组的判定都会自动记录在此。")
    lines.append("")

    lines += ["## 七、操作记录（自动采集）", ""]
    if meta.log:
        lines += ["| 时间 | 动作 | 详情 |", "|---|---|---|"]
        for e in meta.log[-60:]:
            lines.append(f"| {e.get('at','')} | {e.get('action','')} | {e.get('detail','')} |")
    else:
        lines.append("- 暂无记录。")
    lines.append("")

    if generated:
        lines += ["## 八、本次星图产出", "",
                  f"- 入图猫咪：**{generated.get('cats', 0)}** 只",
                  f"- 收录照片：**{generated.get('photos', 0)}** 张",
                  f"- 出没分区：**{len(generated.get('zones', {}))}** 个",
                  f"- 毛色分组：**{len(generated.get('coats', {}))}** 类",
                  ""]

    lines += [
        "## 数据红线自检",
        "",
        "- [ ] 每只猫都有照片证据链（关联照片编号可溯源）",
        "- [ ] 置信度「中」的合并写明了推断依据",
        "- [ ] 置信度「低」的已弃用并记录理由",
        "- [ ] 未虚构行为故事，小传只写照片可见 + 蹲点核实内容",
        "- [ ] 未收录任何含人脸的照片，未公布投喂人信息",
        "",
    ]
    return "\n".join(lines)
