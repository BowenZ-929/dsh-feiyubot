"""手的落点、时钟与字节：实盘在哪、一行怎么追加、本地时区怎么算。

只做「路径与字节」——不判许可（permit 判）、不做动作（actions 做）。手不认识心与脑，
所以本文件不 import 它们，时间的算法自己写一遍（口径照抄心：本地时区、秒精度、带偏移）。

三个目录各只有一个算法，不许在别处再拼一次：
- 手的实盘 `organ_dir("hand")`（= <实盘器官根>/hand），手账与 limits.json 落这里；
- 画出来的图 `<手实盘>/draw`（见 `draw_dir`），只落手自己的盘、再登记进家；
- 家的实盘 `organ_dir("home")`（= 同一个父目录下的 home），任务卡落那里。
显式传进来的 root 是**手自己那个目录**，家按兄弟目录换算——同一个 root 喂两边，
盘的落点就不会分叉。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs import paths

LEDGER_NAME = "ledger.jsonl"
LIMITS_NAME = "limits.json"
DRAW_NAME = "draw"
DIR_MODE = 0o700
FILE_MODE = 0o600
#: 落图时一次读多少字节：图可能不小，不整份进内存两遍。
READ_CHUNK = 1 << 16
#: 落图的名字取指纹前这么多位：够区分，也不至于把文件名撑爆。
LAND_NAME_CHARS = 16
#: 猜不出格式时的后缀：宁可当 png，也不落一个没后缀的文件让下游去猜。
DEFAULT_IMAGE_EXT = ".png"


def hand_root(root=None) -> Path:
    """手的实盘目录：显式 root > `organ_dir("hand")`。显式 root 照样过仓库门禁。

    门禁本身只在 `organs/paths.py` 一处（插件目录是出厂默认参数、只读），这里不重写。
    """
    if root is not None:
        return Path(paths.guard_root(root, "构造参数", must_exist=False))
    return paths.organ_dir("hand")


def home_root(root=None) -> Path:
    """家的实盘目录：手给的 root 是手自己的目录，家是它的**兄弟**目录。

    这么换算的好处是「一个 --root 同时指到临时实盘里」：家的卡与手的账落在同一个
    临时根下，测试与冒烟不会有一边漏到实盘去。
    """
    if root is not None:
        return Path(hand_root(root)).parent / "home"
    return paths.organ_dir("home")


def ledger_path(root=None) -> Path:
    return hand_root(root) / LEDGER_NAME


def live_limits_path(root=None) -> Path:
    return hand_root(root) / LIMITS_NAME


def factory_dir() -> Path:
    """出厂默认（随包发、只读参数）：plugin/organs/hand/data。"""
    return paths.factory_dir("hand")


def factory_limits_path() -> Path:
    return factory_dir() / LIMITS_NAME


# ---- 时钟：本地时区、秒精度、带偏移 --------------------------------------
# 与家写进卡里的 due 同一种形状（2026-11-06T12:00:00+08:00），所以两边能直接比先后。


def parse_ts(value: Any) -> datetime:
    """带偏移的 ISO8601 → datetime。**naive 串不补时区**，直接抛：猜错的时刻比没有更坏。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("没有时区的时间：%r" % (value,))
        return value
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("没有时区的时间串：%r" % (value,))
    return stamp


def now_dt(now: Any = None) -> datetime:
    """「现在」：不给就用本机此刻（带本地偏移），给了就按给的那一个算（演示与测试用）。"""
    if now is None:
        return datetime.now().astimezone()
    if isinstance(now, datetime) and now.tzinfo is None:
        return now.astimezone()
    return parse_ts(now)


def now_iso(now: Any = None) -> str:
    return now_dt(now).isoformat(timespec="seconds")


def local_day(value: Any) -> str:
    """这一刻落在本地日历的哪一天（YYYY-MM-DD）：「当日」按本地算，不按 UTC 算。"""
    return parse_ts(value).astimezone().date().isoformat()


# ---- 字节 ----------------------------------------------------------------


def ensure_dir(path: Path) -> Path:
    """建目录并钉 0700：umask 会把 mkdir 的 mode 打折，所以建完再 chmod 一次。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), DIR_MODE)
    return path


def append_line(path: Path, obj: Dict[str, Any]) -> int:
    """追加一行 JSON 并 fsync。**只增不改**：本函数没有截断模式，也不许有人给它加。"""
    path = Path(path)
    ensure_dir(path.parent)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(str(path), FILE_MODE)
    except OSError:
        pass
    return len(line.encode("utf-8"))


def read_lines(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """读回每一行 JSON。坏行**跳过并计数**，不替它编一行，也不整份作废。"""
    target = Path(path)
    if not target.is_file():
        return [], 0
    rows: List[Dict[str, Any]] = []
    bad = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def read_text(path: Path) -> Optional[str]:
    """整份读回文本；文件不在就 None（「没有这份文件」与「文件是空的」不折叠）。"""
    target = Path(path)
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    """读回一个 JSON 对象；不在／坏了都 None——坏配置由调用方按「这级没给」处理。"""
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None

# ---- 图：读一份已有的字节、把一张画好的图落到自己的盘 ----------------------


def draw_dir(root=None) -> Path:
    """画出来的图落这里：<手实盘>/draw。图的落点只有这一个算法。"""
    return hand_root(root) / DRAW_NAME


def read_blob(path: Any) -> bytes:
    """整份读回字节（图这一类不按文本读）。文件不在就抛：缺件由调用方判成缺件。"""
    return Path(path).read_bytes()


def sha256_file(path: Any) -> str:
    """一份文件的 sha256（边读边算）。**路径与字节的判断都不在这里**，这里只管指纹。"""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def land_draw(data: Any, *, root=None, ext: str = DEFAULT_IMAGE_EXT) -> Path:
    """把画好的字节落到 <手实盘>/draw/<sha256 前 16 位><ext>，返回落点。

    名字由**内容**决定（同一张图落两次得同名，不会越落越多），不拿响应里的 url 或文件名
    拼路径——名字是从外面来的，路径拼法只长在这一处。空字节不落：半成品与占位图不进盘，
    「产物型动作只认产物」这条（docs/modules/hand.md §2 hand-paint）在这里兑现。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("没有字节可落：图是空的")
    suffix = ext.lower() if isinstance(ext, str) and ext.startswith(".") else DEFAULT_IMAGE_EXT
    target = draw_dir(root) / (hashlib.sha256(bytes(data)).hexdigest()[:LAND_NAME_CHARS] + suffix)
    ensure_dir(target.parent)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(bytes(data))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(str(target), FILE_MODE)
    except OSError:
        pass
    return target
