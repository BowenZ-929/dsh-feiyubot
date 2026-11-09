"""日子的测试小工具：临时数据根、可注入的时刻、候选计划。

两条规矩：测试一律用临时目录（绝不写 organs/day/data/ 的真实文件），
时刻一律用注入的 now_ts（绝不依赖真实时钟）。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from organs.brain import BrainStore
from organs.day import model, night
from organs.home import rooms


def temp_root(case: Any) -> Path:
    """一次测试一个临时根，用完删掉。"""
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    return Path(holder.name)


def at(text: str) -> str:
    """把"墙上时间"钉到本机时区，再归一成本地绝对时刻串。

    这样断言与机器时区无关：换台机器跑，结论还是同一个（换日只看本地钟点）。
    """
    return model.iso(datetime.fromisoformat(text).replace(tzinfo=model.local_tz()))


def candidates(*pairs: Tuple[str, str]) -> List[Dict[str, str]]:
    """(kind, 墙上时间) → 候选计划列表。"""
    return [{"kind": kind, "at": at(stamp)} for kind, stamp in pairs]


def brain(root: Path) -> BrainStore:
    """临时脑：夜的第 1 步要一个真 BrainStore，落在临时根下。"""
    return BrainStore(Path(root) / "brain")


def make_home(case: Any) -> Path:
    """临时家：十间建好 + access.json。

    白名单里**没有**抽屉——她的日记与梦走的是 access 里那条按 kind 的例外
    （DRAWER_WRITE_KINDS），不是门二点名的房间。
    """
    root = temp_root(case)
    for name in rooms.ROOM_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "access.json").write_text(
        json.dumps({"version": 1, "writable": ["玄关", "名片盒", "工作台", "仓库", "信箱"]},
                   ensure_ascii=False),
        encoding="utf-8")
    return root


def article(chars: int = 0, head: str = "今天") -> str:
    """恰好 chars 字的一段话：默认取 writing.json 字数区间里的中间值，测试里不抄 80/150。"""
    if chars <= 0:
        span = night.load_writing()["word_range"]
        chars = (span["min"] + span["max"]) // 2
    filler = "把今天的事一件一件摆回原位，摆完就坐着，什么也不做。"
    text = head
    while len(text) < chars:
        text += filler
    return text[:chars]


def fake_mind(text: str = "", retries: int = 0) -> Dict[str, Any]:
    """假核：config/env/transport 三件一起注入，一个真网络请求都不发。

    config 照核那份数据文件的形状给全（缺字段核会报 config 错，那不是我们要测的东西）。
    """
    content = text or article()
    body = json.dumps({"choices": [{"message": {"content": content}}]},
                      ensure_ascii=False).encode("utf-8")

    def transport(url, headers, payload, timeout):
        if payload['messages'][0]['content'].startswith(night.load_writing()['entries']['home_diary']['review_prompt']):
            return 200, json.dumps({'choices': [{'message': {'content': json.dumps({
                'text': content, 'changes': []}, ensure_ascii=False)}}]}).encode()
        return 200, body

    config = {"provider": "test", "model": "test-model",
              "base_url": "http://127.0.0.1:9/v1", "key_env": "TEST_KEY",
              "timeout_ms": 1000, "max_tokens": 64, "retries": int(retries),
              "temperature": 0.7,
              "temperature_tiers": {"factual": 0.5,
                                    "trigger": {"recalled_nonempty": False,
                                                "reply_budget_at_most": 100}}}
    return {"config": config, "env": {"TEST_KEY": "test-key"}, "transport": transport}


def offline_night(case: Any, text: str = "") -> Path:
    """把夜里的核换成假的、把家的根换到临时目录：rhythm 与 HTTP 层那两层测试用。

    器官层自己的测试走 night.run 的 mind= 注入（见 test_night / test_writing）；这两层
    进程内直接调 night.run，没有注入点，所以只能 patch——核一次真请求都不发，
    实盘（家的抽屉）一个字节都不动。
    """
    from unittest import mock
    home = make_home(case)
    previous = os.environ.get("HOME_ROOT")
    os.environ["HOME_ROOT"] = str(home)

    def restore() -> None:
        if previous is None:
            os.environ.pop("HOME_ROOT", None)
        else:
            os.environ["HOME_ROOT"] = previous

    case.addCleanup(restore)
    content = text or article()
    for name, replacement in (("complete", lambda messages, **kwargs:
                              json.dumps({'text': content, 'changes': []}, ensure_ascii=False)
                              if kwargs.get('purpose') == '日记事实复核' else content),
                              ("load_config", lambda *a, **k: {"temperature_tiers":
                                                               {"factual": 0.5}})):
        patcher = mock.patch.object(night, name, replacement)
        patcher.start()
        case.addCleanup(patcher.stop)
    return home


def seed_today(brain_store: BrainStore, now_ts: str,
               text: str = "今天下午把书架整个挪了一次，挪完手上全是灰。") -> Dict[str, Any]:
    """给临时脑塞一条今天的真条目：日记/梦的素材得真有个来源，不然那两步会如实报"没素材"。"""
    return brain_store.ingest(who="人", text=text, ts=now_ts)


def drawer_cards(root: Path, home: Path) -> List[Dict[str, Any]]:
    """抽屉里现在有几张卡（走家的公开读，不翻文件）：日记/梦各几条一眼看得出。"""
    from organs.home import cards
    listing = cards.list_room(str(home), room="抽屉", limit=500)
    return list(listing.get("items") or [])


def log_lines(root: Path) -> List[Dict[str, Any]]:
    """夜日志每一行解出来的对象（一行一次）。"""
    target = Path(root) / "night_log.jsonl"
    if not target.exists():
        return []
    return [json.loads(text) for text in target.read_text(encoding="utf-8").splitlines() if text.strip()]
