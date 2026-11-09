"""魂的命令行入口：从实盘（<仓库>/home/organs/soul）读卡，不碰任何插件层。

根三级：--root > SOUL_ROOT > 实盘默认。出厂（organs/soul/data）不再是默认读取对象——
只读它会让 check 永远绿，盖住"实盘少一条线"这类事；两边的对照见 checks.cross_check_issues。

改魂只有人手的这几条路：edit（改七件里的字段，--structural 换形状）、
migrate（形状变化的唯一入口，坏卡状态下也能跑）、accept-proposal（改待人姿态）、
acceptance（验收探针：写值再写回原值，两行都记 probe；红线一律不探）。
edit 不再有 --probe：探针只有 acceptance 一个入口，两件事不能再挤在一条命令里。
删字段、删条目、越界 target 一律拒绝。六轴卡归心，不在这个目录里。
喜欢什么（identity.likes）也走 edit：它是七件之一，但没有任何程序侧的写路径。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from .. import paths
from . import checks, model, render
from . import pin as pin_module
from . import store as store_module
from .store import SoulStore, load

#: 实盘根的覆盖变量。层级与器官层一致：显式参数 > 环境变量 > 默认根。
ENV_ROOT = "SOUL_ROOT"


def resolve_root(explicit: Optional[str] = None) -> Path:
    """魂的根：--root > SOUL_ROOT > 实盘默认。

    实盘默认的"仓库在哪"不在这里推：口径只有 organs/paths.py 一处（api/roots.py 的
    organ_root("soul") 读的也是它），这里只负责优先级，不另造一套。
    空的 --root / 空的环境变量按"没给"处理：CLI 是人的手，手没给值就落到默认根，
    免得一个空变量把整条路堵死。
    """
    for raw in (explicit, os.environ.get(ENV_ROOT)):
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser()
    return paths.organ_dir("soul")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    # 每个子命令都收同样的两个根开关：读哪一侧、比不比对出厂，都不能靠"用哪个子命令"猜
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None,
                        help="魂的根（不给就看 %s，再不给就是实盘默认）" % ENV_ROOT)
    parser = argparse.ArgumentParser(prog="python3 -m organs.soul.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    checker = sub.add_parser("check", parents=[common],
                             help="打印全部 Issue 与人读摘要，有 error 级则退出码 1")
    checker.add_argument("--no-cross-check", dest="no_cross_check", action="store_true",
                         help="只考一侧：不比对出厂与实盘的红线 id / identity 键集合（单测用）")
    sub.add_parser("show", parents=[common], help="打印四块现状与 digest")
    renderer = sub.add_parser("render", parents=[common], help="打印注入文本与七件预算")
    renderer.add_argument("--audience", default="")
    editor = sub.add_parser("edit", parents=[common], help="人手改七件里的一个字段，写后同一事务追一行账")
    editor.add_argument("--block", required=True)
    editor.add_argument("--path", required=True)
    editor.add_argument("--value", required=True)
    editor.add_argument("--actor", required=True)
    editor.add_argument("--reason", required=True)
    editor.add_argument("--structural", action="store_true",
                        help="结构变更（新建字段/整块替换/搬走字段）：不显式声明一律拒；账记 migrate")
    accepter_probe = sub.add_parser("acceptance", parents=[common],
                                    help="验收探针：一次写值再写回原值，两行都记 probe（每天 ≤3 对）")
    accepter_probe.add_argument("--block", required=True)
    accepter_probe.add_argument("--path", required=True)
    accepter_probe.add_argument("--value", required=True)
    accepter_probe.add_argument("--restore-value", required=True)
    accepter_probe.add_argument("--actor", required=True)
    accepter_probe.add_argument("--reason", required=True)
    migrator = sub.add_parser("migrate", parents=[common],
                              help="形状变化的唯一入口：宽容读旧卡、校验新形状、对齐 digest 后写盘")
    migrator.add_argument("--block", required=True)
    migrator.add_argument("--value", required=True, help="迁移后的整块 JSON")
    migrator.add_argument("--actor", required=True)
    migrator.add_argument("--reason", required=True)
    migrator.add_argument("--expect-digest", required=True,
                          help="当前块文件的 sha256（带不带 sha256: 前缀都认）")
    accepter = sub.add_parser("accept-proposal", parents=[common],
                             help="人手接受一份提案（改待人姿态的唯一提案路径）")
    accepter.add_argument("--proposal", required=True, help="提案 JSON：id/target/value/evidence/reason")
    accepter.add_argument("--actor", required=True)
    accepter.add_argument("--monthly-limit", type=int, default=model.PROPOSAL_MONTHLY_LIMIT)
    accepter.add_argument("--now", default=None, help="注入当前时间（自测用，默认真实时间）")
    sub.add_parser("pin", parents=[common],
                   help="打印压缩前要 pin 的那一份（条目 id + 禁令首句，≤1200 字节）")
    sub.add_parser("revisions", parents=[common], help="列出整档归档里还留着的版本（最新在前）")
    rollbacker = sub.add_parser("rollback", parents=[common],
                                help="回到某一版：回滚也记账、回滚前也先存整档")
    rollbacker.add_argument("--revision", required=True, type=int)
    rollbacker.add_argument("--actor", required=True)
    rollbacker.add_argument("--reason", required=True)
    return parser.parse_args(argv)


def _print_issues(issues: Sequence[model.Issue]) -> None:
    for issue in issues:
        print("[%s] %s %s" % (issue.level, issue.code, issue.message))


def _value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _show(store: SoulStore) -> int:
    snapshot = store.snapshot()
    for line in checks.summary(store.root):
        print(line)
    print("digest %s" % snapshot.digest)
    print("loaded_at %s" % snapshot.loaded_at)
    _print_issues(snapshot.issues)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    root = resolve_root(args.root)
    if args.cmd == "check":
        issues = checks.collect(root, cross_check=not args.no_cross_check)
        # 先说清楚这一次 check 读的是哪一侧：读错根还一片绿，就是"check 骗人"本身
        print("魂根 %s" % root)
        print("出厂 %s" % checks.FACTORY_ROOT)
        for line in checks.summary(root):
            print(line)
        print()
        if not issues:
            print("自检：无 Issue")
        _print_issues(issues)
        return 1 if [item for item in issues if item.level == model.ERROR] else 0
    if args.cmd == "rollback":
        # 与 migrate 同理：坏卡状态下也要回得去，所以走模块级 rollback()，不过 load()
        result = store_module.rollback(root, actor=args.actor, reason=args.reason,
                                       revision=args.revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.cmd == "migrate":
        # 在 load() 之前：旧形状本身就是坏卡，这条路正是给坏卡状态的
        result = store_module.migrate(root, actor=args.actor, reason=args.reason,
                                      block=args.block, new_value=_value(args.value),
                                      expect_old_digest=args.expect_digest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    try:
        store = load(root)
    except model.SoulLoadError as exc:
        print("载入失败：")
        _print_issues(exc.issues)
        return 1
    if args.cmd == "show":
        return _show(store)
    if args.cmd == "render":
        try:
            text = store.render(args.audience)
        except ValueError as exc:
            print("渲染失败：%s" % exc)
            return 1
        print(text)
        print()
        print("预算（七件）%d/%d 字（合计是硬线）"
              % (render.budget_used(store.snapshot()), model.limits.body_total))
        return 0
    if args.cmd == "revisions":
        print(json.dumps({"current_revision": store.revision(),
                          "revisions": store.revisions()}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "pin":
        text = store.pin()
        print(text)
        print()
        print("pin %d/%d 字节" % (len(text.encode("utf-8")), pin_module.PIN_LIMIT))
        return 0
    if args.cmd == "acceptance":
        result = store.acceptance(actor=args.actor, reason=args.reason, block=args.block,
                                  path=args.path, value=_value(args.value),
                                  restore_value=_value(args.restore_value))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.cmd == "accept-proposal":
        result = store.accept_proposal(actor=args.actor, proposal=_value(args.proposal),
                                       monthly_limit=args.monthly_limit, now=args.now)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    result = store.edit(actor=args.actor, reason=args.reason, block=args.block,
                        path=args.path, value=_value(args.value),
                        structural=args.structural)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

