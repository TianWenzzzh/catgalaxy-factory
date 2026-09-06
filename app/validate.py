"""F2 · 校验引擎：列完整性 / 照片匹配 / 编号连续性 / 置信度警告。"""
from __future__ import annotations

from collections import Counter

from .config import CONFIDENCE_LEVELS, ROSTER_COLUMNS
from .csv_loader import id_number, normalize_id
from .models import CatRow, Issue, Summary, ValidationReport
from .star_mapper import id_gaps

MAX_FEATURE_LEN = 4


def _stem(name: str) -> str:
    base = name.rsplit(".", 1)
    return (base[0] if len(base) == 2 and base[1] else name).lower()


def build_photo_index(photos) -> tuple[dict[str, str], dict[str, list[str]]]:
    """已上传照片 → (basename 索引, stem 索引)。stem 索引用于扩展名归一后的兜底匹配。"""
    by_name: dict[str, str] = {}
    by_stem: dict[str, list[str]] = {}
    for p in photos:
        by_name.setdefault(p.lower(), p)
        by_stem.setdefault(_stem(p), []).append(p)
    return by_name, by_stem


def resolve_photo(raw: str, by_name: dict[str, str],
                  by_stem: dict[str, list[str]]) -> tuple[str, str]:
    """名册里的照片写法 → (实际入库文件名, 去掉目录后的 basename)。找不到时首项为空。

    真实数据里既有 `xxx.jpg` 也有 `照片视频/xxx.jpg`，而上传照片会被安全化成裸文件名，
    所以必须按 basename 比对；压缩统一输出 .jpg，故再留一层 stem 兜底。
    """
    base = (raw or "").strip().replace("\\", "/").split("/")[-1].strip()
    if not base:
        return "", ""
    hit = by_name.get(base.lower())
    if hit:
        return hit, base
    cands = by_stem.get(_stem(base)) or []
    if len(cands) == 1:
        return cands[0], base
    return "", base


def validate(rows: list[CatRow], photos: set[str], *,
             missing_columns: list[str] | None = None,
             unknown_columns: list[str] | None = None,
             school: str = "", project_id: str = "") -> ValidationReport:
    """执行全部校验规则，返回报告。

    rows: 解析出的名册行；photos: 已上传照片文件名集合（安全化后的名字）。
    """
    issues: list[Issue] = []
    missing_columns = missing_columns or []
    unknown_columns = unknown_columns or []

    # --- 列完整性 ---
    if missing_columns:
        for col in missing_columns:
            issues.append(Issue(level="error", code="E_MISSING_COLUMN",
                                message=f"名册缺少必需列「{col}」", field=col))
    if unknown_columns:
        for col in unknown_columns:
            issues.append(Issue(level="info", code="I_UNKNOWN_COLUMN",
                                message=f"名册含未识别列「{col}」，已忽略", field=col))
    if not rows:
        issues.append(Issue(level="error", code="E_EMPTY_ROSTER",
                            message="名册为空（没有数据行）"))

    by_name, by_stem = build_photo_index(photos)
    id_counter: Counter[str] = Counter()
    photo_owner: dict[str, str] = {}
    referenced: set[str] = set()
    low_conf: list[str] = []
    valid_rows: list[CatRow] = []

    for row in rows:
        row_issues_before = len(issues)
        cid = normalize_id(row.id)

        # 编号
        if not row.id.strip():
            issues.append(Issue(level="error", code="E_BAD_ID", line=row.line,
                                message="编号为空", field="编号"))
        elif cid is None:
            issues.append(Issue(level="error", code="E_BAD_ID", line=row.line,
                                message=f"编号「{row.id}」不符合 CAT-NNN 格式", field="编号"))
        else:
            row.id = cid
            id_counter[cid] += 1
            if id_counter[cid] > 1:
                issues.append(Issue(level="error", code="E_DUP_ID", line=row.line,
                                    cat_id=cid,
                                    message=f"编号 {cid} 重复出现（第 {id_counter[cid]} 次）",
                                    field="编号"))

        # 必填字段
        for field, value in (("昵称", row.name), ("毛色", row.coat),
                             ("代表照片文件", row.photo_file)):
            if not (value or "").strip():
                issues.append(Issue(level="error", code="E_REQUIRED_FIELD",
                                    line=row.line, cat_id=row.id or None,
                                    message=f"「{field}」为空", field=field))

        # 照片匹配
        raw_photo = row.photo_file.strip()
        if raw_photo:
            resolved, _base = resolve_photo(raw_photo, by_name, by_stem)
            if resolved:
                if resolved != raw_photo:
                    issues.append(Issue(
                        level="info", code="I_PHOTO_PATH_NORMALIZED", line=row.line,
                        cat_id=row.id or None,
                        message=f"代表照片「{raw_photo}」已按上传文件名归一为「{resolved}」",
                        field="代表照片文件"))
                row.photo_file = resolved
                referenced.add(resolved)
                key = resolved.lower()
                owner = photo_owner.get(key)
                if owner and owner != (row.id or f"line{row.line}"):
                    issues.append(Issue(
                        level="warning", code="W_PHOTO_SHARED", line=row.line,
                        cat_id=row.id or None,
                        message=f"照片「{resolved}」同时被 {owner} 与 "
                                f"{row.id or '本行'} 引用，可能是重复建档",
                        field="代表照片文件"))
                else:
                    photo_owner.setdefault(key, row.id or f"line{row.line}")
            else:
                issues.append(Issue(
                    level="error", code="E_PHOTO_MISSING", line=row.line,
                    cat_id=row.id or None,
                    message=f"代表照片「{raw_photo}」未在已上传照片中找到",
                    field="代表照片文件"))

        # 照片数量
        if not row.photo_count_raw.strip():
            issues.append(Issue(level="warning", code="W_BAD_COUNT", line=row.line,
                                cat_id=row.id or None, message="「照片数量」为空，按 1 处理",
                                field="照片数量"))
        elif row.photo_count <= 0:
            issues.append(Issue(level="warning", code="W_BAD_COUNT", line=row.line,
                                cat_id=row.id or None,
                                message=f"「照片数量」值「{row.photo_count_raw}」不是正整数，按 1 处理",
                                field="照片数量"))

        # 置信度
        conf = (row.confidence or "").strip()
        if not conf:
            issues.append(Issue(level="warning", code="W_BAD_CONFIDENCE", line=row.line,
                                cat_id=row.id or None, message="「置信度」为空，建议标注 高/中/低",
                                field="置信度"))
        elif conf not in CONFIDENCE_LEVELS:
            issues.append(Issue(level="warning", code="W_BAD_CONFIDENCE", line=row.line,
                                cat_id=row.id or None,
                                message=f"「置信度」值「{conf}」不在 高/中/低 之内", field="置信度"))
        elif conf == "低":
            low_conf.append(row.id or f"line{row.line}")
            issues.append(Issue(level="warning", code="W_LOW_CONFIDENCE", line=row.line,
                                cat_id=row.id or None,
                                message=f"{row.name or row.id} 置信度为「低」，按数据红线建议弃用",
                                field="置信度"))

        # 特征描述过短
        if len((row.features or "").strip()) < MAX_FEATURE_LEN:
            issues.append(Issue(level="info", code="I_THIN_FEATURE", line=row.line,
                                cat_id=row.id or None,
                                message="「特征描述」过短，他人实地对照会困难", field="特征描述"))

        # 出没区域缺失
        if not (row.area or "").strip():
            issues.append(Issue(level="info", code="I_NO_AREA", line=row.line,
                                cat_id=row.id or None,
                                message="「出没区域」为空，星位将落入未归类区", field="出没区域"))

        if len(issues) == row_issues_before:
            valid_rows.append(row)
        elif not any(i.line == row.line and i.level == "error"
                     for i in issues[row_issues_before:]):
            valid_rows.append(row)

    # --- 未使用照片 ---
    unused = sorted(p for p in photos if p not in referenced)
    for p in unused:
        issues.append(Issue(level="info", code="I_PHOTO_UNUSED",
                            message=f"照片「{p}」已上传但名册未引用"))

    # --- 编号连续性 ---
    gaps, lo, hi = id_gaps(rows)
    for g in gaps:
        issues.append(Issue(level="info", code="I_ID_GAP", cat_id=g,
                            message=f"编号 {g} 空缺（弃用编号允许空缺，不拦截）"))

    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    infos = [i for i in issues if i.level == "info"]

    summary = Summary(
        total_rows=len(rows),
        valid_rows=len(valid_rows),
        error_count=len(errors),
        warning_count=len(warnings),
        info_count=len(infos),
        photos_uploaded=len(photos),
        photos_referenced=len(referenced),
        photos_unused=len(unused),
        id_min=f"CAT-{lo:03d}" if lo else None,
        id_max=f"CAT-{hi:03d}" if hi else None,
        id_gaps=gaps,
        low_confidence=low_conf,
        ok=(len(errors) == 0 and len(rows) > 0),
    )

    return ValidationReport(project_id=project_id, school=school,
                            summary=summary, issues=issues, rows=rows)


def error_codes(report: ValidationReport) -> list[str]:
    return [i.code for i in report.issues if i.level == "error"]


def required_columns() -> list[str]:
    return list(ROSTER_COLUMNS)


def passing_rows(report: ValidationReport, *, exclude_low_confidence: bool = False) -> list[CatRow]:
    """挑出可进星图的行：无 error 的行；可选排除置信度=低。"""
    bad_lines = {i.line for i in report.issues if i.level == "error" and i.line}
    out = [r for r in report.rows if r.line not in bad_lines]
    if exclude_low_confidence:
        out = [r for r in out if (r.confidence or "").strip() != "低"]
    return out


def report_markdown(report: ValidationReport) -> str:
    """把报告渲染成 Markdown，随 zip 一起交付（数据溯源）。"""
    s = report.summary
    lines = [
        f"# {report.school or '校园'}猫咪名册 · 校验报告",
        "",
        f"- 数据行总数：**{s.total_rows}**",
        f"- 可入图行数：**{s.valid_rows}**",
        f"- 错误 / 警告 / 提示：**{s.error_count} / {s.warning_count} / {s.info_count}**",
        f"- 结论：{'✅ 通过，可生成星图' if s.ok else '❌ 存在错误，需先修复'}",
        f"- 照片：已上传 {s.photos_uploaded} 张，名册引用 {s.photos_referenced} 张，"
        f"未使用 {s.photos_unused} 张",
        f"- 编号范围：{s.id_min or '—'} ~ {s.id_max or '—'}",
    ]
    if s.id_gaps:
        lines.append(f"- 编号空缺（弃用不复用）：{', '.join(s.id_gaps)}")
    if s.low_confidence:
        lines.append(f"- 低置信度警告清单：{', '.join(s.low_confidence)}")

    lines += ["", "## 明细", "", "| 级别 | 代码 | 行 | 编号 | 字段 | 说明 |",
              "|---|---|---|---|---|---|"]
    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(report.issues, key=lambda x: (order[x.level], x.line or 0)):
        lines.append(f"| {i.level} | {i.code} | {i.line or ''} | {i.cat_id or ''} | "
                     f"{i.field or ''} | {i.message} |")
    return "\n".join(lines) + "\n"
