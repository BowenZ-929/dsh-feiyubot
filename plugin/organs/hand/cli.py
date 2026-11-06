"""手的人可读自检：每条命令打**一行 JSON**，里面两层都有——`say` 给人，`code` 给机器。

    python3 -m organs.hand.cli add --title "交房租" --when "明天中午" --root /tmp/hand
    python3 -m organs.hand.cli complete --id 工作台-1 --root /tmp/hand
    python3 -m organs.hand.cli due --root /tmp/hand
    python3 -m organs.hand.cli ledger --root /tmp/hand
    python3 -m organs.hand.cli permit --action add_task --root /tmp/hand
    python3 -m organs.hand.cli status --root /tmp/hand
    python3 -m organs.hand.cli send-image --scene 客厅 --target 阿禾 --name 参考图.png --root /tmp/hand
    python3 -m organs.hand.cli send-image --scene 客厅 --target 阿禾 --utterance "给我看你的自画像" --root /tmp/hand
    python3 -m organs.hand.cli draw --description "一只在窗台上打盹的猫" --scene 客厅 --target 阿禾 --root /tmp/hand
    python3 -m organs.hand.cli set-style --utterance "一句话说完，别分开发" --root /tmp/hand
    python3 -m organs.hand.cli set-style --key max_chars --value 30 --root /tmp/hand

`--root` 指到**手自己的实盘目录**（家的目录按兄弟目录换算），所以冒烟永远指临时根，
仓里的 home/ 一个字节都不动。`--at` 指定「现在」（带偏移的 ISO），让演示不看本机时钟。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional, Sequence

from . import actions, ledger, permit, quota, store, whitelist

COMMANDS = ("add", "complete", "due", "ledger", "permit", "status", "send-image", "draw",
            "set-style")


def _base(state: str, action: str, code: str, say: str, who: str,
          detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """与 actions 同一个信封：命令不同，读法相同。"""
    return {"state": state, "action": action, "code": code, "say": say, "who": who,
            "card_id": None, "detail": dict(detail or {}), "ledger_id": None}


def _cmd_add(args: argparse.Namespace) -> Dict[str, Any]:
    return actions.add_task(who=args.who, title=args.title, due=args.due, when=args.when,
                            note=args.note, now=args.at, root=args.root)


def _cmd_complete(args: argparse.Namespace) -> Dict[str, Any]:
    return actions.complete_task(who=args.who, card_id=args.card_id, now=args.at, root=args.root)


def _cmd_send_image(args: argparse.Namespace) -> Dict[str, Any]:
    return actions.send_image(who=args.who, scene=args.scene, target=args.target,
                              path=args.path, name=args.name, utterance=args.utterance,
                              now=args.at, root=args.root)


def _cmd_draw(args: argparse.Namespace) -> Dict[str, Any]:
    return actions.draw(who=args.who, description=args.description, scene=args.scene,
                        target=args.target, now=args.at, root=args.root)


def _cmd_set_style(args: argparse.Namespace) -> Dict[str, Any]:
    return actions.set_style(who=args.who, key=args.key, value=args.value,
                             utterance=args.utterance, now=args.at, root=args.root)


def _cmd_due(args: argparse.Namespace) -> Dict[str, Any]:
    ready = actions.due(now=args.at, root=args.root)
    return _base("done", "due", "OK", "到点还没做的有 %d 件" % len(ready), args.who,
                 {"now": store.now_iso(args.at), "tasks": ready})


def _cmd_ledger(args: argparse.Namespace) -> Dict[str, Any]:
    rows = ledger.read_tail(limit=args.rows, root=args.root)
    return _base("done", "ledger", "OK", "手账最后 %d 个动作" % len(rows), args.who,
                 {"rows": rows, "path": str(store.ledger_path(args.root))})


def _cmd_permit(args: argparse.Namespace) -> Dict[str, Any]:
    checked = permit.check(args.action, who=args.who, now=args.at, root=args.root)
    # 这里的额度是「判闸时看到的那一份」：--limit 是诊断用的，不许拿它改判定。
    left = quota.remaining(args.who, now=args.at, root=args.root)
    detail = dict(checked["detail"])
    detail["quota"] = left
    detail["needs_grant"] = permit.needs_grant(args.action)
    return _base("done" if checked["allowed"] else "refused", str(args.action),
                 str(checked["code"] or "OK"), checked["say"], args.who, detail)


def _cmd_status(args: argparse.Namespace) -> Dict[str, Any]:
    left = quota.remaining(args.who, now=args.at, root=args.root, explicit=args.limit)
    rows = ledger.read_all(root=args.root)
    return _base("done", "status", "OK",
                 "%s 今天还能 %d 次（上限 %d，已用 %d，额度来自 %s）"
                 % (args.who, left["remaining"], left["limit"], left["used"], left["source"]),
                 args.who,
                 {"quota": left, "actions": list(whitelist.ACTIONS),
                  "risk": {name: permit.risk_of(name) for name in whitelist.ACTIONS},
                  "now": store.now_iso(args.at), "ledger_actions": len(rows),
                  "hand_root": str(store.hand_root(args.root)),
                  "home_root": str(store.home_root(args.root)),
                  "live_limits": str(store.live_limits_path(args.root)),
                  "factory_limits": str(store.factory_limits_path())})


HANDLERS = {"add": _cmd_add, "complete": _cmd_complete, "due": _cmd_due,
            "ledger": _cmd_ledger, "permit": _cmd_permit, "status": _cmd_status,
            "send-image": _cmd_send_image, "draw": _cmd_draw, "set-style": _cmd_set_style}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m organs.hand.cli",
        description="手：五个动作 + 三层许可 + 一本只增的手账。每条命令打一行 JSON。")
    parser.add_argument("cmd", choices=COMMANDS)
    parser.add_argument("--who", default="1000xxxx", help="谁在动（回执里的 who）")
    parser.add_argument("--title", default=None, help="add：任务名（家的路径形状要过）")
    parser.add_argument("--when", default=None, help="add：人话时间（明天中午 / 下午3点半）")
    parser.add_argument("--due", default=None, help="add：直接给带时区的 ISO8601")
    parser.add_argument("--note", default="", help="add：一句备注")
    parser.add_argument("--id", dest="card_id", default=None, help="complete：卡 id")
    parser.add_argument("--scene", default=None,
                        help="send-image／draw：group（群）或 private（私聊），通道只认这两个")
    parser.add_argument("--target", default=None,
                        help="send-image／draw：群号或 QQ 号（数字；出站时是 JSON 数字）")
    parser.add_argument("--path", default=None, help="send-image：图的绝对路径（与 --name 二选一）")
    parser.add_argument("--name", default=None,
                        help="send-image：图在家的名字（与 --path 二选一；也可以只给 --utterance）")
    parser.add_argument("--description", default=None, help="draw：画什么")
    parser.add_argument("--key", default=None,
                        help="set-style：只认 chunks_per_reply（1–3）或 max_chars（10–80）")
    parser.add_argument("--value", default=None, help="set-style：那一个旋钮要改成几（整数）")
    parser.add_argument("--utterance", default=None,
                        help="send-image：她那句话（给我看你的自画像）；"
                             "set-style：她那句话（一句话说完 / 说短点 / 一次别超20字）")
    parser.add_argument("--at", default=None, help="指定「现在」（带偏移的 ISO），演示用")
    parser.add_argument("--root", default=None, help="临时实盘根（手自己的目录），默认走 organ_dir")
    parser.add_argument("--limit", type=int, default=None,
                        help="status：诊断用，把上限按这个数看（不改闸的判定）")
    parser.add_argument("--action", default="add_task", help="permit：要看哪个动作")
    parser.add_argument("--rows", type=int, default=20, help="ledger：读最后几个动作")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "add" and not args.title:
        parser.error("add 要 --title：没有名字就不知道记什么")
    if args.cmd == "complete" and not args.card_id:
        parser.error("complete 要 --id：要指明是哪张卡")
    if args.cmd == "draw" and not (args.description or "").strip():
        parser.error("draw 要 --description：没有描述就不知道画什么")
    # send-image 的「两个都给／都不给」不算用法错误：那是动作层的 BAD_ARGUMENT，回来的是同一套码。
    # 时间缺了不算用法错误：那是「她拒绝」，交给动作层回 MISSING_DUE，好让外面看到同一套码。

    try:
        payload = HANDLERS[args.cmd](args)
    except (ValueError, TypeError) as exc:
        # 时间串坏到解析不了这类，打一行带码的 JSON 而不是抛栈：命令行也要两层回执。
        payload = _base("failed", args.cmd, "BAD_ARGUMENT", "这条参数我看不懂：%s" % exc,
                        args.who, {"error": str(exc)})
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
