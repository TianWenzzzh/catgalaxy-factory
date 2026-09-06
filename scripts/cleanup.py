"""工作区清理 · 按「多久没动过」回收项目目录。

    python scripts/cleanup.py --before 7d              # 干跑：只列出会被清掉的
    python scripts/cleanup.py --before 7d --apply      # 真删
    python scripts/cleanup.py --before 24h --keep-latest 3 --apply
    python scripts/cleanup.py --stats                  # 只看占用，不筛时间
    python scripts/cleanup.py --before 7d --json       # 机器可读输出

默认干跑，必须显式 --apply 才动手。判定依据是项目目录的 mtime（比 meta 里的
updated_at 更可靠：照片被重压过、产物被重新生成过，目录时间都会变）。

红线：只认项目内的 workspace/，路径不在项目目录下就直接拒绝执行；E 盘、
D:\\code、D:\\校园基米skill 一律不碰。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402

UNITS = {"h": 3600, "d": 86400, "w": 604800}


def parse_before(text: str) -> int:
    """把 '7d' / '24h' / '2w' 换算成秒。写法不对就报错，不猜。"""
    s = (text or "").strip().lower()
    if len(s) < 2 or s[-1] not in UNITS or not s[:-1].isdigit():
        raise ValueError(f"--before 要写成 7d / 24h / 2w 这样的形式，收到「{text}」")
    n = int(s[:-1])
    if n <= 0:
        raise ValueError("--before 的数字必须大于 0")
    return n * UNITS[s[-1]]


def dir_size(path: Path) -> int:
    """目录占用字节数。读不到的文件按 0 计，不为一个坏文件中断整轮统计。"""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def fmt_bytes(n: int) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def scan(with_size: bool = True) -> list[dict]:
    """工作区里每个项目的实况：最后活动时间、占用、有没有生成过产物。"""
    ws = config.WORKSPACE
    if not ws.exists():
        return []
    rows: list[dict] = []
    for d in sorted(ws.iterdir()):
        if not d.is_dir():
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        meta = store.load_meta(d.name)
        rows.append({
            "id": d.name,
            "school": meta.school if meta else "",
            "is_project": meta is not None,
            "photo_count": meta.photo_count if meta else 0,
            "mtime": mtime,
            "modified_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "bytes": dir_size(d) if with_size else 0,
            "has_output": (d / "dist").exists() and any((d / "dist").iterdir()),
        })
    rows.sort(key=lambda r: r["mtime"])
    return rows


def select(rows: list[dict], before_s: int, keep_latest: int, now: float) -> list[dict]:
    """挑出「超过 before_s 没动过」的项目，并保住最近 keep_latest 个。"""
    cutoff = now - before_s
    stale = [r for r in rows if r["mtime"] < cutoff]
    if keep_latest > 0:
        recent = {r["id"] for r in sorted(rows, key=lambda r: -r["mtime"])[:keep_latest]}
        stale = [r for r in stale if r["id"] not in recent]
    return stale


def delete(pid: str) -> list[str]:
    """删一个项目目录，返回失败原因列表（空表示删干净了）。"""
    errs: list[str] = []

    def onexc(func, path, exc):
        errs.append(f"{func.__name__} {path}: {exc}")

    shutil.rmtree(store.project_dir(pid), onexc=onexc)
    return errs


def is_safe_workspace(ws: Path, root: Path) -> bool:
    """工作区必须在项目目录之内——防止配置被改歪后删到别处去。"""
    ws = ws.resolve()
    root = root.resolve()
    return ws == root or root in ws.parents


def guard_workspace() -> Path | None:
    """确认工作区确实在项目目录内，否则拒绝执行。"""
    ws = config.WORKSPACE
    if not is_safe_workspace(ws, config.ROOT):
        print(f"拒绝执行：工作区 {ws.resolve()} 不在项目目录 {config.ROOT} 之下。",
              file=sys.stderr)
        print("本脚本只清理喵星图工厂自己的 workspace/，不会碰任何其它路径。", file=sys.stderr)
        return None
    return ws


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按「多久没动过」清理工作区里的项目目录")
    ap.add_argument("--before", help="时间窗，如 7d / 24h / 2w；不填则只出统计")
    ap.add_argument("--keep-latest", type=int, default=0,
                    help="无论多久没动，都保住最近的 N 个项目")
    ap.add_argument("--apply", action="store_true", help="真删（默认只干跑）")
    ap.add_argument("--no-size", action="store_true", help="跳过体积统计，扫得更快")
    ap.add_argument("--stats", action="store_true", help="只看工作区占用概况")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.keep_latest < 0:
        print("--keep-latest 不能为负", file=sys.stderr)
        return 2
    try:
        before_s = parse_before(args.before) if args.before else 0
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.apply and not args.before:
        print("--apply 必须配 --before，否则等于「清空工作区」，本脚本不干这事。",
              file=sys.stderr)
        return 2
    if guard_workspace() is None:
        return 2

    now = time.time()
    rows = scan(with_size=not args.no_size)
    orphans = [r for r in rows if not r["is_project"]]
    projects = [r for r in rows if r["is_project"]]

    if args.stats or not args.before:
        total = sum(r["bytes"] for r in rows)
        out = {"projects": len(projects), "orphans": len(orphans),
               "total_bytes": total, "total_human": fmt_bytes(total),
               "oldest": projects[0]["modified_at"] if projects else "",
               "newest": projects[-1]["modified_at"] if projects else "",
               "rows": rows}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"工作区 {config.WORKSPACE}")
            print(f"  项目 {len(projects)} 个 · 非项目目录 {len(orphans)} 个 · "
                  f"合计 {fmt_bytes(total)}")
            if projects:
                print(f"  最久没动：{projects[0]['id']}（{projects[0]['modified_at']}）")
                print(f"  最近动过：{projects[-1]['id']}（{projects[-1]['modified_at']}）")
            for r in sorted(rows, key=lambda r: -r["bytes"])[:10]:
                mark = "" if r["is_project"] else "  ← 不是项目目录"
                print(f"    {fmt_bytes(r['bytes']):>9}  {r['modified_at']}  "
                      f"{r['id']}{mark}")
            if not args.before:
                print("\n加 --before 7d 看哪些该清了；加 --apply 才真删。")
        return 0

    doomed = select(rows, before_s, args.keep_latest, now)
    reclaim = sum(r["bytes"] for r in doomed)
    failures: list[dict] = []
    deleted_ids: set[str] = set()

    if args.apply:
        for r in doomed:
            errs = delete(r["id"])
            if errs:
                failures.append({"id": r["id"], "errors": errs[:5]})
            else:
                deleted_ids.add(r["id"])

    result = {"before": args.before, "before_seconds": before_s,
              "keep_latest": args.keep_latest, "applied": args.apply,
              "candidates": len(doomed), "bytes": reclaim,
              "bytes_human": fmt_bytes(reclaim),
              "deleted": len(deleted_ids),
              "failures": failures,
              "remaining_projects": len([r for r in projects if r["id"] not in deleted_ids]),
              "rows": doomed}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verb = "已删除" if args.apply else "将被删除（干跑）"
        print(f"--before {args.before}（{before_s // 3600} 小时没动过）"
              + (f" · 保住最近 {args.keep_latest} 个" if args.keep_latest else ""))
        if not doomed:
            print(f"  没有符合条件的项目。工作区现有 {len(projects)} 个项目。")
        else:
            print(f"  {verb} {len(doomed)} 个项目，回收 {fmt_bytes(reclaim)}：")
            for r in doomed:
                flag = "有产物" if r["has_output"] else "无产物"
                print(f"    {r['modified_at']}  {fmt_bytes(r['bytes']):>9}  "
                      f"{r['id']}  {r['school']}  {r['photo_count']}照 {flag}")
            if failures:
                print(f"  ⚠ {len(failures)} 个没删干净：")
                for f in failures:
                    print(f"    {f['id']}: {f['errors'][0]}")
            if not args.apply:
                print("\n确认无误后加 --apply 执行删除。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
