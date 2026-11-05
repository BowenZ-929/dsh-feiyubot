#!/usr/bin/env python3
"""把她"这一段时间的经历"清干净，只留下"她是谁"。

用途：测试期的消息、记忆、状态都是假的；正式迁入旧家之前要先清场，
否则真记忆会和测试数据混在一起，谁也分不清哪条是真的。

留下的（她的人设，人定的）：
  · 魂（home/organs/soul/**）——她的本体，程序永不改
  · 六轴卡（home/organs/heart/baseline-card.json）
  · 台词卡（home/organs/home/仓库/anchors.jsonl）
  · 家的决定与骨架（access.json / rooms.json / .gitkeep）
  · 参数（retention/tiers/topics/engage_weights/limits/delivery 等出厂默认的镜像）

清掉的（测试期攒下的经历）：
  · 脑：today/hot/longterm/people/edges/proposals/selfnarration/usage/evictions/候选话题
  · 心：energy/mood_traj/pending/refusals
  · 家：物件与审计（各屋 index.jsonl、audit.jsonl、audit.seen.json、指针卡）
  · 日：日程与送达账
  · 嘴：运行时账本（trace/ 下的弃言、拒绝、超长）

安全：默认只**打印计划**；加 --yes 才动，且不是删除而是整包移到 home/.reset-<时间戳>/，随时可拿回。

用法：
    python3 tools/reset_experience.py            # 看看会清什么
    python3 tools/reset_experience.py --yes      # 真的清（先备份到 .reset-<时间戳>）
"""
from __future__ import annotations

import datetime
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "home" / "organs"

#: 她的人设：这些永远不动
KEEP = (
    "soul/",                       # 魂：她的本体
    "heart/baseline-card.json",    # 六轴卡
    "home/仓库/anchors.jsonl",      # 台词卡（人定的口头禅）
    "home/access.json",            # 她能写哪几间（决定）
    "home/rooms.json",             # 十间房骨架
    "heart/.gitkeep",
)

#: 测试期攒下的经历：清
CLEAR = (
    "brain/today.json", "brain/hot.json", "brain/longterm.jsonl", "brain/people.json",
    "brain/people_card_history.jsonl", "brain/edges.json", "brain/proposals.json",
    "brain/proposal_log.jsonl", "brain/selfnarration.jsonl", "brain/usage.jsonl",
    "brain/evictions.jsonl", "brain/topic_candidates.json",
    "heart/energy.json", "heart/mood_traj.jsonl", "heart/pending.json", "heart/refusals.json",
    "day/schedule.json",
    "mind/recent_turns.jsonl",  # 最近 12 轮：也是经历，清场要清
    "home/audit.jsonl", "home/audit.seen.json", "home/仓库/index.jsonl", "home/工作台/index.jsonl",
)


def kept(rel: str) -> bool:
    return any(rel == k or rel.startswith(k) for k in KEEP)


def collect() -> list[Path]:
    out: list[Path] = []
    for rel in CLEAR:
        p = HOME / rel
        if p.exists():
            out.append(p)
    # 各屋里的物件与其索引（只留 .gitkeep 与台词卡）
    for room in sorted((HOME / "home").iterdir()) if (HOME / "home").exists() else []:
        if not room.is_dir():
            continue
        for child in sorted(room.iterdir()):
            rel = str(child.relative_to(HOME))
            if kept(rel) or child.name == ".gitkeep":
                continue
            out.append(child)
    for ledger in (ROOT / "plugin" / "organs" / "mouth" / "trace").glob("*"):
        out.append(ledger)
    # 去重：CLEAR 里点过名的文件，可能又被「各屋扫描」收一遍
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            unique.append(p)
    return unique


def main(argv: list[str]) -> int:
    apply = "--yes" in argv
    targets = collect()
    if not targets:
        print("没有要清的：她现在的经历是空的。")
        return 0
    print("将清掉 %d 项（留下的：魂、六轴卡、台词卡、家的决定与骨架、参数）：" % len(targets))
    for p in targets:
        size = p.stat().st_size if p.is_file() else -1
        print("  %-52s %s" % (str(p.relative_to(ROOT)), "目录" if size < 0 else "%d 字节" % size))
    if not apply:
        print("\n这是预演。确认无误后加 --yes 执行（会整体移到 home/.reset-<时间戳>/，可拿回）。")
        return 0
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    vault = ROOT / "home" / (".reset-" + stamp)
    for p in targets:
        dest = vault / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
    print("\n已清场，原件在 %s（随时可拿回）。" % vault)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
