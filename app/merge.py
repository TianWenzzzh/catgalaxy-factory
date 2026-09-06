"""F9 · 归并工作台：把「疑似同一只猫」的候选组摆上台面，等人工判定并留痕。

判定不自动改名册——数据红线要求每条取舍都有人签字，所以这里只做三件事：
找候选、记判定、把理由送进 F7 摘要。真正的合并由人回到 CSV 里落笔。
"""
from __future__ import annotations

from collections import defaultdict

from .csv_loader import normalize_id
from .models import CatRow

VERDICTS = ("same", "different", "unsure")
VERDICT_LABEL = {"same": "同一只猫", "different": "不是同一只", "unsure": "存疑待复核"}

MAX_GROUP_SIZE = 5          # 再大就不是「疑似重复」而是「同色猫扎堆」，没有比对价值
MIN_SIMILARITY = 0.12       # 特征描述太不像的同色同区猫，不进候选
_PUNCT = "，,。、；;：: 　\t（）()【】[]「」\"'“”‘’…—-·|/\\."


def _key(row: CatRow) -> str:
    return normalize_id(row.id) or (row.id or "").strip() or f"line{row.line}"


def _bigrams(text: str) -> set[str]:
    """中文按二字切片——不引分词依赖也够判断特征描述像不像。"""
    s = "".join(ch for ch in (text or "") if ch not in _PUNCT)
    if len(s) > 1:
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {s} if s else set()


def similarity(a: str, b: str) -> float:
    """特征描述的 Jaccard 相似度，0~1。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _avg_similarity(members: list[CatRow]) -> float:
    sims = [similarity(a.features, b.features)
            for i, a in enumerate(members) for b in members[i + 1:]]
    return sum(sims) / len(sims) if sims else 0.0


def _member(r: CatRow) -> dict:
    return {"id": _key(r), "line": r.line, "name": r.name, "coat": r.coat,
            "area": r.area, "features": r.features, "photo": r.photo_file,
            "photo_count": r.photo_count, "confidence": r.confidence,
            "related": r.related, "note": r.note}


def _gid(kind: str, keys: list[str]) -> str:
    """候选组 id 取内容指纹：名册一改，旧判定自然失效而不是错挂到新组上。"""
    h = 2166136261
    for ch in kind + "|" + ",".join(sorted(keys)):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"M-{h:08x}"


def _group(kind: str, rows: list[CatRow], score: float, reason: str) -> dict:
    return {"gid": _gid(kind, [_key(r) for r in rows]), "kind": kind,
            "score": round(score, 3), "reason": reason,
            "members": [_member(r) for r in rows]}


def candidate_groups(rows: list[CatRow]) -> list[dict]:
    """疑似重复建档的候选组，按可疑度降序。"""
    groups: list[dict] = []
    claimed: set[frozenset] = set()

    by_photo: dict[str, list[CatRow]] = defaultdict(list)
    for r in rows:
        p = (r.photo_file or "").strip().lower()
        if p:
            by_photo[p].append(r)
    for photo, members in sorted(by_photo.items()):
        if len(members) < 2:
            continue
        claimed.add(frozenset(_key(m) for m in members))
        groups.append(_group("same-photo", members, 0.95,
                             f"共用同一张代表照片「{photo}」——最可能是重复建档"))

    by_zone: dict[tuple[str, str], list[CatRow]] = defaultdict(list)
    for r in rows:
        coat = (r.coat or "").strip()
        if coat:
            by_zone[(coat, (r.area or "").strip())].append(r)
    for (coat, area), members in sorted(by_zone.items(),
                                        key=lambda kv: (-len(kv[1]), kv[0])):
        if not 2 <= len(members) <= MAX_GROUP_SIZE:
            continue
        if frozenset(_key(m) for m in members) in claimed:
            continue
        sim = _avg_similarity(members)
        if sim < MIN_SIMILARITY:
            continue
        groups.append(_group(
            "coat-area", members, 0.35 + 0.55 * sim,
            f"毛色「{coat}」+ 出没区域「{area or '未填'}」相同，"
            f"特征描述相似度 {sim:.0%}"))

    return sorted(groups, key=lambda g: (-g["score"], g["gid"]))


def check_decision(group: dict, verdict: str, keep: str, drop: list[str],
                   reason: str = "") -> list[str]:
    """判定入库前的体检。返回错误列表，空列表表示可以入库。"""
    errs: list[str] = []
    if verdict not in VERDICTS:
        errs.append(f"判定只能是 {'/'.join(VERDICTS)}，收到「{verdict}」")
    ids = {m["id"] for m in group["members"]}
    if keep and keep not in ids:
        errs.append(f"保留编号 {keep} 不在这个候选组里")
    bad = sorted(d for d in drop if d not in ids)
    if bad:
        errs.append(f"弃用编号不在这个候选组里：{', '.join(bad[:5])}")
    if keep and keep in drop:
        errs.append(f"{keep} 不能既保留又弃用")
    if verdict == "same":
        if not keep:
            errs.append("判定「同一只猫」必须写明保留哪个编号")
        if not drop:
            errs.append("判定「同一只猫」必须写明弃用哪些编号")
    if verdict != "unsure" and len((reason or "").strip()) < 2:
        errs.append("判定同猫/不同猫必须写理由（数据红线：每条取舍都要留痕）")
    return errs
