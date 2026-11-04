"""魂的加载、热更新、原子写与变更史。

写路径全部由人手触发（面板 / CLI）：edit()（改值）、migrate()（换形状）、
accept_proposal()（接受提案）、add_redline() / retire_redline()（红线逐条加 / 逐条撤）、
rollback()（回到某一版），以及它们共用的写入点 _atomic_write / append_history。
程序侧没有任何别的开门方式；形状变化只能走 migrate()。

验收探针不是第六只手：acceptance() 用 edit() 连写两次（value，然后 restore_value），两行都记
kind=probe。探针有自己的端点、自己的日限额，也不许碰红线——所以 edit() 的 probe 形参只留给
内部与测试，HTTP 与 CLI 都不暴露它（传了就 400，PROBE_VIA_EDIT_FORBIDDEN）。
红线段的整块替换与整表替换一律拒（BOUNDS_BLOCK_REPLACE_FORBIDDEN，吸收参考册 M3）。

一次写入是一个事务：写块 + 追一行账 + 刷内存快照三步同成同败；账追不上就整次作废。
读方每次取快照都比一次四张卡的 (mtime, size, inode)，变过就重载——改完下一回合生效，不重启。

写盘前有两道前置校验，顺序固定：
1. M1 红线断言（redline.check_bounds）：不过就不许落盘，拒因随返回值回给调用方；
2. 整档归档（archive_now）：把三张卡原文复制成同目录 previous.json（0600），revision 自增，
   环形保留最近 20 版。取不到归档就整次拒（GATE_SNAPSHOT）。
history 与整档归档分工不同、不许互相顶替：**history 解释「谁为什么改」，归档负责「回到哪一版」**。
history 只留摘要与指纹，复原不了原文；归档是整份原文，但答不出「为什么」。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import gates, model, redline
from .bounds import match as match_bounds
from .pin import take_pin as take_pin_text
from .render import render as render_snapshot

BLOCK_FILES = {
    "identity": "identity.json",
    "manner": "manner.json",
    "bounds": "bounds.json",
}
HISTORY_FILE = "history.jsonl"
# 整档归档（吸收参考册 M5）：写前把三张卡原文存一份，revision 自增，环形留最近 20 版
ARCHIVE_FILE = "previous.json"
RING_SLOTS = 20
RING_FILE = "previous-%02d.json"
ARCHIVE_MODE = 0o600
KIND_EDIT = "edit"            # 内容真改：改一个已存在的叶子值
KIND_MIGRATE = "migrate"      # 形状变化：换结构、加/删字段、整块替换（唯一入口 migrate()）
KIND_PROBE = "probe"          # 探测与验收：为验证链路而改，随后可能还原
KIND_MANUAL = "manual"        # 人手工补账：只许事故用，check 报 warn 要人复核
KINDS = (KIND_EDIT, KIND_MIGRATE, KIND_PROBE, KIND_MANUAL)
# 提案是第五种账行，不在这四类里：它记的不是"哪块哪个字段变了"，而是"人接受了哪份提案"，
# 由 accept_proposal() 写、由 checks._proposal_issues 单列校验（proposal_id / 三条例据 / 月限额）。
KIND_PROPOSAL = "proposal"
# 回滚是第六种账行：它记的不是「哪块哪个字段」，而是「回到哪一版」，所以单列一种；
# 四类（edit/migrate/probe/manual）一字不动，回滚不混进它们里。
KIND_ROLLBACK = "rollback"
KNOWN_KINDS = KINDS + (KIND_PROPOSAL, KIND_ROLLBACK)

# 红线加/撤与验收探针的拒因码：都在写盘之前当场判，拒因跟着返回值回给面板。
REDLINE_REASON_REQUIRED = "REDLINE_REASON_REQUIRED"        # 加/撤红线不写 reason
REDLINE_RERELEASE_COOLDOWN = "REDLINE_RERELEASE_COOLDOWN"  # 同一条线撤了又想 7 天内加回
PROBE_VIA_EDIT_FORBIDDEN = "PROBE_VIA_EDIT_FORBIDDEN"      # HTTP/CLI 的 edit 不许带 probe
PROBE_ON_BOUNDS_FORBIDDEN = "PROBE_ON_BOUNDS_FORBIDDEN"    # 探针不许碰红线块
PROBE_DAILY_LIMIT = "PROBE_DAILY_LIMIT"                    # 每天 ≤3 对探针
RERELEASE_HOURS = 168.0    # 撤了又加的冷却：7 天
PROBE_DAILY_PAIRS = 3      # 每天允许的探针对数（一对 = 两条 probe 账行）


@dataclass(frozen=True)
class Snapshot:
    identity: model.Identity
    manner: model.Manner
    bounds: model.Bounds
    digest: str
    loaded_at: str
    issues: Tuple[model.Issue, ...]


def errors_only(issues: Sequence[model.Issue]) -> List[model.Issue]:
    return [item for item in issues if item.level == model.ERROR]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def iso_to_epoch(text: str) -> float:
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def month_of(stamp: Any) -> Optional[str]:
    """ISO 时间戳 → "YYYY-MM"；给不出就返回 None（坏行由 check 报，不在这里猜）。"""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m")
    except ValueError:
        return None


def proposals_in_month(rows: Sequence[Dict[str, Any]], stamp: str) -> List[Dict[str, Any]]:
    """本月已记进账的提案：accept_proposal 与 check 用同一个口径。

    红线加/撤**不占**这份月限额：它们是 kind=edit 的窄口，不写 proposal 行，所以这里数不到。
    """
    month = month_of(stamp)
    return [row for row in rows
            if row.get("kind") == KIND_PROPOSAL and month_of(row.get("ts")) == month]


def day_of(stamp: Any) -> Optional[str]:
    """ISO 时间戳 → "YYYY-MM-DD"；给不出就返回 None（探针日限额按天算）。"""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m-%d")
    except ValueError:
        return None


def probe_pairs_in_day(rows: Sequence[Dict[str, Any]], stamp: str) -> int:
    """当天的探针对数 = 当天 probe 账行数 / 2（一次验收写两行：value 一行、还原一行）。"""
    day = day_of(stamp)
    count = len([row for row in rows
                 if row.get("kind") == KIND_PROBE and day_of(row.get("ts")) == day])
    return count // 2


def rerelease_issues(rows: Sequence[Dict[str, Any]], item_id: str, stamp: str,
                     hours: float = RERELEASE_HOURS) -> List[model.Issue]:
    """⑥ 同一条红线 7 天内不许「撤了又加」：按 id 查该 id 最近一次 retire 的时间。

    probe 行不计：探针是为验证链路而改，不是人撤线，不该锁住这条线的复燃冷却。
    """
    try:
        now_epoch = iso_to_epoch(stamp)
    except (TypeError, ValueError):
        return [model.Issue(REDLINE_RERELEASE_COOLDOWN, model.ERROR,
                            "算复燃冷却要一个 ISO 时间，拿到的是 %r" % (stamp,))]
    last_epoch = None
    for row in rows:
        if row.get("kind") == KIND_PROBE:
            continue
        if row.get("redline_op") != "retire" or row.get("redline_id") != item_id:
            continue
        try:
            epoch = iso_to_epoch(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if last_epoch is None or epoch > last_epoch:
            last_epoch = epoch
    if last_epoch is None:
        return []
    waited = now_epoch - last_epoch
    if waited >= hours * 3600:
        return []
    return [model.Issue(
        REDLINE_RERELEASE_COOLDOWN, model.ERROR,
        "红线 %s 在 %.1f 小时前刚撤下（冷却 %.0f 小时未满）：撤了又加等于把撤线的决定作废，"
        "7 天内不许加回同一条" % (item_id, waited / 3600.0, hours))]


def read_blocks(root: Path) -> Tuple[Dict[str, Any], List[model.Issue]]:
    """读三块卡并校验；不抛异常，坏卡的 Issue 全部返回。

    六轴卡不在其中：它已迁到 organs/heart/，目录里残留的 baseline.json 一律不读。
    """
    root = Path(root)
    issues: List[model.Issue] = []
    raw: Dict[str, Any] = {}
    for block in model.BLOCK_NAMES:
        name = BLOCK_FILES[block]
        path = root / name
        if not path.is_file():
            issues.append(model.Issue("SOUL_BLOCK_MISSING", model.ERROR, "%s 缺失：%s" % (block, name)))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(model.Issue("SOUL_JSON_UNREADABLE", model.ERROR, "%s 读不出来：%s" % (name, exc)))
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            issues.append(model.Issue("SOUL_JSON_UNREADABLE", model.ERROR, "%s 不是合法 JSON：%s" % (name, exc)))
            continue
        raw[block] = data
        issues.extend(model.validate(block, data))
    if errors_only(issues):
        return raw, issues
    blocks = dict((block, model.parse(block, raw[block])) for block in model.BLOCK_NAMES)
    total = model.body_chars(blocks["identity"], blocks["manner"], blocks["bounds"])
    if total > model.limits.body_total:
        issues.append(model.Issue("BUDGET_OVER", model.ERROR,
                                  "六件正文合计 %d 字 > %d 字（合计是硬线，超了拒载）"
                                  % (total, model.limits.body_total)))
    return raw, issues


def _stat_signature(path: Path) -> Optional[Tuple[int, int, int]]:
    """(st_mtime_ns, st_size, st_ino)：原子替换会换 inode，只看 mtime 会漏。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(root: Path) -> str:
    overall = hashlib.sha256()
    for block in model.BLOCK_NAMES:
        overall.update(block.encode("utf-8") + b"\0")
        try:
            overall.update((Path(root) / BLOCK_FILES[block]).read_bytes())
        except OSError:
            overall.update(b"")
    return "sha256:" + overall.hexdigest()


def build_snapshot(root: Path, raw: Dict[str, Any], issues: Sequence[model.Issue]) -> Snapshot:
    return Snapshot(
        identity=model.parse("identity", raw["identity"]),
        manner=model.parse("manner", raw["manner"]),
        bounds=model.parse("bounds", raw["bounds"]),
        digest=_digest(root),
        loaded_at=now_iso(),
        issues=tuple(issues),
    )


def read_history(path: Path) -> List[Dict[str, Any]]:
    """只读账：文件不存在 = 空账；有坏行即整账不可读。"""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise model.SoulLoadError([model.Issue("HISTORY_UNREADABLE", model.ERROR,
                                                "history 读不出来：%s" % exc)])
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.SoulLoadError([model.Issue("HISTORY_UNREADABLE", model.ERROR,
                                                    "history 第 %d 行不是合法 JSON：%s" % (number, exc))])
        if not isinstance(row, dict):
            raise model.SoulLoadError([model.Issue("HISTORY_UNREADABLE", model.ERROR,
                                                    "history 第 %d 行不是对象" % number)])
        rows.append(row)
    return rows


def append_history(path: Path, record: Dict[str, Any]) -> None:
    """追加一行；独立成函数，事务回滚靠它可被替换成失败，也只增不改。"""
    with open(str(path), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, data: bytes) -> None:
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# —— 整档归档（M5）：previous.json + 环形 20 版 —————————————————————————————
def _archive_path(root: Path) -> Path:
    return Path(root) / ARCHIVE_FILE


def _ring_path(root: Path, revision: int) -> Path:
    """环形槽：槽号 = revision % 20。写第 N 版就覆盖同槽里的第 N-20 版，最旧的自动淘汰。

    用「固定 20 个槽 + 覆盖」而不是「删最旧的文件」：魂的程序侧只有两个写点
    （_atomic_write / append_history），淘汰最旧不该再开第三个写点。
    """
    return Path(root) / (RING_FILE % (revision % RING_SLOTS))


def read_archive(path: Path) -> Optional[Dict[str, Any]]:
    """读一份归档；读不出来或形状不对就返回 None（调用方据此报「没有这一版」）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), dict):
        return None
    if not isinstance(data.get("revision"), int) or isinstance(data.get("revision"), bool):
        return None
    return data


def current_revision(root: Path) -> int:
    """活着这一版的号：最新归档的 revision + 1；没有归档就是 0（装机时的原样）。"""
    archive = read_archive(_archive_path(Path(root)))
    if archive is None:
        return 0
    return int(archive["revision"]) + 1


def archive_now(root: Path) -> int:
    """写盘前取整档：三张卡的**原文**复制成 previous.json + 环形槽，返回被存下的版本号。

    存原文而不是解析结果：归档的用途是「回到哪一版」，回滚要逐字还原；而且 migrate 是在坏卡
    状态下跑的，那时原文根本解析不出来，解析结果也就存不下来。
    """
    root = Path(root)
    revision = current_revision(root)
    blocks: Dict[str, str] = {}
    for block, name in BLOCK_FILES.items():
        try:
            blocks[block] = (root / name).read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError("%s 读不出来，取不到整档归档：%s" % (name, exc))
    envelope = {"revision": revision, "ts": now_iso(), "blocks": blocks}
    data = (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for target in (_archive_path(root), _ring_path(root, revision)):
        _atomic_write(target, data)
        os.chmod(str(target), ARCHIVE_MODE)   # mkstemp 建的就是 0600，这里写明，不靠默认值
    return revision


def list_revisions(root: Path) -> List[Dict[str, Any]]:
    """环形里还留着的版本，最新在前：面板「回滚」读它。

    history 解释「谁为什么改」，这里回答「回到哪一版」——两者分工不同，不许互相顶替。
    """
    root = Path(root)
    paths = [_archive_path(root)] + [_ring_path(root, slot) for slot in range(RING_SLOTS)]
    found: Dict[int, Dict[str, Any]] = {}
    for path in paths:
        archive = read_archive(path)
        if archive is None:
            continue
        revision = int(archive["revision"])
        if revision in found:
            continue
        blocks = archive.get("blocks") or {}
        found[revision] = {
            "revision": revision,
            "ts": archive.get("ts"),
            "bytes": sum(len(text.encode("utf-8")) for text in blocks.values()
                         if isinstance(text, str)),
            "blocks": sorted(str(name) for name in blocks),
            "file": path.name,
        }
    return [found[key] for key in sorted(found, reverse=True)][:RING_SLOTS]


def archive_of(root: Path, revision: int) -> Optional[Dict[str, Any]]:
    """按版本号取一份归档：先看 previous.json，再看环形槽。"""
    root = Path(root)
    for path in (_archive_path(root), _ring_path(root, revision)):
        archive = read_archive(path)
        if archive is not None and int(archive["revision"]) == revision:
            return archive
    return None


def redline_issues(raw: Dict[str, Any], block: str, new_block: Dict[str, Any]
                   ) -> List[model.Issue]:
    """M1：把「改完之后的红线段」拿去跑断言（对象是 render() 的红线段，不是文件行）。

    写盘前调；不过就不许落盘。三张卡都已过 model.validate，这里 parse 不会抛。
    """
    candidate = dict(raw)
    candidate[block] = new_block
    return redline.check_bounds(model.parse("bounds", candidate["bounds"]))


def rollback(root: Path, actor: str, reason: str, revision: int) -> Dict[str, Any]:
    """人的手：让魂回到 revision 那一版。

    回滚本身也是一次写入：先存当前整档（所以回滚可以再回滚回去），校验目标整档能载入、
    红线段过 M1 断言，才原子落盘三张卡，再追一行 kind=rollback 的账——宁可没改成，
    也不能改了没账。
    """
    root = Path(root)
    if not isinstance(actor, str) or not actor.strip():
        return _reject("actor 不能为空：账本要记清是谁回滚的")
    if not isinstance(reason, str) or not reason.strip():
        return _reject("reason 不能为空：账本要记清为什么回滚")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return _reject("revision 必须是整数版本号")
    archive = archive_of(root, revision)
    if archive is None:
        return _reject("环形里没有 revision %r 这一版（只留最近 %d 版）"
                       % (revision, RING_SLOTS))
    blocks = archive.get("blocks") or {}
    texts: Dict[str, str] = {}
    raw: Dict[str, Any] = {}
    issues: List[model.Issue] = []
    for block, name in BLOCK_FILES.items():
        text = blocks.get(block)
        if not isinstance(text, str) or not text.strip():
            return _reject("revision %d 缺 %s：归档不完整，不能拿它回滚" % (revision, name))
        texts[block] = text
        try:
            raw[block] = json.loads(text)
        except json.JSONDecodeError as exc:
            issues.append(model.Issue("SOUL_JSON_UNREADABLE", model.ERROR,
                                      "revision %d 的 %s 不是合法 JSON：%s"
                                      % (revision, name, exc)))
            continue
        issues.extend(model.validate(block, raw[block]))
    if errors_only(issues):
        return _reject("这一版载不进去（回滚目标必须是能载入的整档），未写入", issues)
    text_issues = redline.check_bounds(model.parse("bounds", raw["bounds"]))
    if errors_only(text_issues):
        return _reject("这一版的红线段过不了断言，未写入", text_issues)
    before: Dict[str, bytes] = {}
    for name in BLOCK_FILES.values():
        try:
            before[name] = (root / name).read_bytes()
        except OSError:
            before[name] = b""
    old_digest = _digest(root)
    try:
        archived = archive_now(root)
    except OSError as exc:
        return _reject_issue(gates.SNAPSHOT, "取整档归档失败，本次回滚作废：%s" % exc)
    for block, name in BLOCK_FILES.items():
        _atomic_write(root / name, texts[block].encode("utf-8"))
    line = {
        "kind": KIND_ROLLBACK,
        "actor": actor.strip(),
        "block": "",
        "path": "",
        "target_revision": revision,
        "old_summary": "回滚前这一版",
        "old_digest": old_digest,
        "new_summary": "回到 revision %d（%s）" % (revision, archive.get("ts")),
        "new_digest": _digest(root),
        "reason": reason.strip(),
        "revision": archived + 1,
    }
    line["ts"] = now_iso()
    try:
        append_history(root / HISTORY_FILE, line)
    except Exception as exc:  # noqa: BLE001 - 追账失败就是整次作废，原因要原样带出去
        for name, data in before.items():
            _atomic_write(root / name, data)
        return _reject("history 追加失败，已回滚，本次回滚作废：%s" % exc)
    _, post = read_blocks(root)
    if errors_only(post):
        for name, data in before.items():
            _atomic_write(root / name, data)
        return _reject("回滚后整张魂载不进去，已还原", post)
    return {"ok": True, "history_line": line, "revision": current_revision(root),
            "restored_from": revision,
            "note": "history 解释谁为什么改；快照负责回到哪一版，两者分工不混"}


def _parse_path(path: str) -> List[Any]:
    tokens: List[Any] = []
    buffer = ""
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            if buffer:
                tokens.append(buffer)
                buffer = ""
        elif char == "[":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            close = path.find("]", index)
            if close < 0:
                raise ValueError("缺少 ]")
            body = path[index + 1:close]
            if not body.isdigit():
                raise ValueError("[%s] 不是下标" % body)
            tokens.append(int(body))
            index = close
        elif char == "]":
            raise ValueError("多余的 ]")
        else:
            buffer += char
        index += 1
    if buffer:
        tokens.append(buffer)
    if not tokens:
        raise ValueError("path 不能为空")
    return tokens


def _get_path(data: Any, tokens: Sequence[Any]) -> Any:
    node = data
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(node, list):
                raise TypeError("路径 %r 处不是数组" % (token,))
        elif not isinstance(node, dict):
            raise TypeError("路径 %r 处不是对象" % (token,))
        node = node[token]
    return node


def _set_path(data: Any, tokens: Sequence[Any], value: Any, create: bool = False) -> None:
    """按 path 落值。create=True 才许新建缺的字段——那是结构变更，要人显式点头。"""
    node = data
    for token in tokens[:-1]:
        if isinstance(token, int):
            if not isinstance(node, list):
                raise TypeError("路径 %r 处不是数组" % (token,))
        else:
            if not isinstance(node, dict) or token not in node:
                raise KeyError(token)
        node = node[token]
    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(node, list):
            raise TypeError("末段 %r 处不是数组" % (last,))
        if not 0 <= last < len(node):
            raise IndexError(last)
        node[last] = value
        return
    if not isinstance(node, dict) or last not in node:
        if create and isinstance(node, dict):
            node[last] = value
            return
        raise KeyError(last)
    node[last] = value


def _revived_id(bounds_raw: Any, path: str) -> Optional[str]:
    """path 落在 items[N].active 且这一条现在是 active=false → 返回要「加回」的条目 id。

    撤了又加有两条路：再走一次 add_redline（同 id），或者把 active 改回 true。后者更隐蔽，
    所以复燃冷却两处都查——不然「同一条线 7 天内不许撤了又加」换个写法就绕过了。
    """
    try:
        tokens = _parse_path(path)
    except ValueError:
        return None
    if (len(tokens) != 3 or tokens[0] != "items" or not isinstance(tokens[1], int)
            or tokens[2] != "active"):
        return None
    items = bounds_raw.get("items") if isinstance(bounds_raw, dict) else None
    if not isinstance(items, list) or not 0 <= tokens[1] < len(items):
        return None
    item = items[tokens[1]]
    if not isinstance(item, dict) or item.get("active") is not False:
        return None
    item_id = item.get("id")
    return item_id.strip() if isinstance(item_id, str) and item_id.strip() else None


def _deletion(old: Any, new: Any, where: str = "") -> Optional[str]:
    """删字段、删条目、把已有值置空一律算删除：魂只有增与改，没有删。"""
    where = where or "根"
    if new is None and old is not None:
        return "%s 被置空：魂不许删字段，红线只许 active=false" % where
    if isinstance(old, list):
        if not isinstance(new, list):
            return "%s 不是数组了：魂不许删条目" % where
        if len(new) < len(old):
            return "%s 少了 %d 条：魂不许删条目" % (where, len(old) - len(new))
        for index, (before, after) in enumerate(zip(old, new)):
            found = _deletion(before, after, "%s[%d]" % (where, index))
            if found:
                return found
        return None
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return "%s 不是对象了：魂不许删字段" % where
        missing = [str(key) for key in old if key not in new]
        if missing:
            return "%s 的字段被删：%s" % (where, "/".join(missing))
        for key in new:
            if key not in old:
                continue      # 新增字段不是删除（结构变更会添字段）
            found = _deletion(old[key], new[key], "%s.%s" % (where, key))
            if found:
                return found
        return None
    return None


def _bounds_deletion(old_block: Any, new_block: Any, value: Any) -> Optional[str]:
    if value is None:
        return "红线不能删除：撤线只能把 active 改成 false"
    old_items = old_block.get("items") if isinstance(old_block, dict) else None
    new_items = new_block.get("items") if isinstance(new_block, dict) else None
    if not isinstance(old_items, list) or not isinstance(new_items, list):
        return None
    old_ids = [item.get("id") for item in old_items if isinstance(item, dict)]
    new_ids = [item.get("id") for item in new_items if isinstance(item, dict)]
    missing = [item_id for item_id in old_ids if item_id not in new_ids]
    if missing:
        return "红线不能删除也不能改 id：%s 不见了（撤线只能 active=false）" % "/".join(
            str(item_id) for item_id in missing)
    return None


def _summary(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:60] + "…" if len(text) > 60 else text


def _reject(message: str, issues: Sequence[model.Issue] = ()) -> Dict[str, Any]:
    """拒绝结果必须 JSON 安全：HTTP 层拿到就能直接序列化。"""
    return {"ok": False, "message": message, "issues": [item.to_dict() for item in issues]}


def _reject_issue(code: str, message: str) -> Dict[str, Any]:
    return _reject(message, [model.Issue(code, model.ERROR, message)])


class SoulStore:
    def __init__(self, root: Path, raw: Dict[str, Any], issues: Sequence[model.Issue]) -> None:
        self._root = Path(root)
        self._raw = raw
        self._snapshot = build_snapshot(self._root, raw, issues)
        self._signatures = self._read_signatures()

    @property
    def root(self) -> Path:
        """魂数据目录：checks.summary / store.read_blocks 只吃路径。"""
        return self._root

    def _read_signatures(self) -> Dict[str, Optional[Tuple[int, int, int]]]:
        return dict((name, _stat_signature(self._root / name)) for name in BLOCK_FILES.values())

    def _reload(self) -> Snapshot:
        raw, issues = read_blocks(self._root)
        if not errors_only(issues):
            # M1：热更新之后同样跑一次红线断言；不过就照旧拒载（磁盘已经变了，拒的是这次重载）
            issues = list(issues) + redline.check_bounds(model.parse("bounds", raw["bounds"]))
        bad = errors_only(issues)
        if bad:
            raise model.SoulLoadError(issues)
        self._raw = raw
        self._snapshot = build_snapshot(self._root, raw, issues)
        self._signatures = self._read_signatures()
        return self._snapshot

    def snapshot(self) -> Snapshot:
        """每次取都给最新：盘上四张卡变过就重载，改完下一回合生效、不重启。"""
        if self._read_signatures() != self._signatures:
            return self._reload()
        return self._snapshot

    def raw(self, block: str) -> Dict[str, Any]:
        """该块在磁盘上的原始结构（深拷贝，调用方改它不影响 store）。

        edit() 的 path 按这个形状定位：bounds 条目是 match.type / match.pattern；
        snapshot() 给的是归一化形状 match_type / pattern，拿快照字段名当 path 会定位失败。
        """
        if block not in BLOCK_FILES:
            raise ValueError("block 只能是 %s" % "/".join(model.BLOCK_NAMES))
        self.snapshot()  # 磁盘先变过就先重载，raw 才不会落后于 snapshot
        return copy.deepcopy(self._raw[block])

    def render(self, audience: str = "") -> str:
        return render_snapshot(self.snapshot(), audience)

    def check(self) -> List[model.Issue]:
        from . import checks  # 局部导入：checks 反过来用本模块读卡，顶层导入会成环

        return checks.collect(self._root)

    def match_bounds(self, text: str) -> List[str]:
        return match_bounds(self.snapshot().bounds.items, text)

    def history(self) -> List[Dict[str, Any]]:
        return read_history(self._root / HISTORY_FILE)

    def pin(self) -> str:
        """M4：压缩前要 pin 的那一份——每条红线「条目 id ｜ 禁令首句」，≤1200 字节。

        只取生效条目：撤下的线（active=false）不进注入，也就不该占 pin 的预算。
        """
        return take_pin_text(self.snapshot().bounds)

    def revision(self) -> int:
        """活着这一版的号（面板与 GET /soul 都读它）。"""
        return current_revision(self._root)

    def revisions(self) -> List[Dict[str, Any]]:
        """环形里还留着的版本，最新在前。「回滚」读它；history 负责解释，这里负责回到。"""
        return list_revisions(self._root)

    def rollback(self, actor: str, reason: str, revision: int) -> Dict[str, Any]:
        """人的手：回到某一版。成功后重载内存快照，改完下一回合生效。"""
        # 这里调的是模块级 rollback()：坏卡状态下也能回滚，所以它不吃 store 实例
        result = rollback(self._root, actor=actor, reason=reason, revision=revision)
        if result.get("ok"):
            self._reload()
        return result

    def add_redline(self, actor: str, reason: str, item: Dict[str, Any],
                    now: Optional[str] = None) -> Dict[str, Any]:
        """逐条加一条红线——⑤ 的专用动作名（旧名 add_bounds_item 已删，不留别名）。

        加线不能走 edit(path="items")：那是整表替换，一律拒。所以另开这一条窄口：
        只追加一条、id 不许撞、其余条目逐字不动；账上 path 记成 items[N]，并写明
        redline_id / redline_op（7 天复燃冷却就按这两格查）。
        加线写的是 kind=edit 的窄口、不写 proposal 行，所以**不占**那 2 次月限额；
        但 reason 一定要有——空/空白即拒（REDLINE_REASON_REQUIRED）。
        now 只有自测注入时间时才会给：冷却与账行 ts 用同一个钟。
        """
        if not isinstance(actor, str) or not actor.strip():
            return _reject("actor 不能为空：账本要记清是谁加的这一条")
        if not isinstance(reason, str) or not reason.strip():
            return _reject_issue(REDLINE_REASON_REQUIRED,
                                 "reason 不能为空：加一条红线必须写清为什么（空白也算没写）")
        if not isinstance(item, dict):
            return _reject("item 必须是 JSON 对象（id/rule/match/action）")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            return _reject("item.id 不能为空：加一条红线要给条目 id")
        stamp = now or now_iso()
        try:
            rows = read_history(self._root / HISTORY_FILE)
        except model.SoulLoadError as exc:
            return _reject("history 读不出来，先修账", exc.issues)
        rerelease = rerelease_issues(rows, item_id, stamp)
        if rerelease:
            return _reject("这条红线刚撤下，复燃冷却未满，未写入", rerelease)
        prepared = self._loaded_for_write("bounds")
        if not prepared.get("ok"):
            return prepared
        raw = prepared["raw"]
        new_block = prepared["new_block"]
        items = new_block.get("items")
        if not isinstance(items, list):
            return _reject("bounds.items 不是数组，先修卡")
        new_block["items"] = items + [copy.deepcopy(item)]
        block_issues = model.validate("bounds", new_block)
        if errors_only(block_issues):
            return _reject("加完这一条线过不了校验，未写入", block_issues)
        index = len(items)
        line = {
            "kind": KIND_EDIT,
            "redline_op": "add",
            "redline_id": item_id.strip(),
            "actor": actor.strip(),
            "block": "bounds",
            "path": "items[%d]" % index,
            "old_summary": "（新增，无旧值）",
            "old_digest": _digest_bytes(prepared["old_bytes"]),
            "new_summary": _summary(item.get("rule") or item),
            "new_digest": _digest_bytes((json.dumps(new_block, ensure_ascii=False, indent=2)
                                        + "\n").encode("utf-8")),
            "reason": reason.strip(),
        }
        return self._commit("bounds", new_block, prepared["old_bytes"],
                            prepared["signature"], line, raw, stamp=now)

    def retire_redline(self, actor: str, reason: str, item_id: str) -> Dict[str, Any]:
        """逐条撤一条红线——⑤ 的专用动作名（旧名 retire_bounds_item 已删，不留别名）。

        只把 active 改成 false，不物理删除：撤下的线仍占六件预算、仍留在卡里、账上留有摘要；
        这也是「账本只增不改」的同一个道理。账上记 redline_id / redline_op=retire，
        加线的 7 天复燃冷却就查它。撤线同样不占那 2 次月限额，但必须写 reason。
        """
        if not isinstance(actor, str) or not actor.strip():
            return _reject("actor 不能为空：账本要记清是谁撤的这一条")
        if not isinstance(reason, str) or not reason.strip():
            return _reject_issue(REDLINE_REASON_REQUIRED,
                                 "reason 不能为空：撤一条红线必须写清为什么（空白也算没写）")
        if not isinstance(item_id, str) or not item_id.strip():
            return _reject("item_id 不能为空：撤哪一条要说出来")
        prepared = self._loaded_for_write("bounds")
        if not prepared.get("ok"):
            return prepared
        raw = prepared["raw"]
        new_block = prepared["new_block"]
        items = new_block.get("items")
        if not isinstance(items, list):
            return _reject("bounds.items 不是数组，先修卡")
        index = None
        for position, item in enumerate(items):
            if isinstance(item, dict) and item.get("id") == item_id:
                index = position
                break
        if index is None:
            return _reject("没有 id 为 %r 的红线" % (item_id,))
        if items[index].get("active") is False:
            return _reject("红线 %s 已经是撤下状态：撤线只有一次" % item_id)
        previous_rule = items[index].get("rule")
        items[index]["active"] = False
        block_issues = model.validate("bounds", new_block)
        if errors_only(block_issues):
            return _reject("撤完这一条线过不了校验，未写入", block_issues)
        line = {
            "kind": KIND_EDIT,
            "redline_op": "retire",
            "redline_id": item_id.strip(),
            "actor": actor.strip(),
            "block": "bounds",
            "path": "items[%d].active" % index,
            "old_summary": _summary(previous_rule),
            "old_digest": _digest_bytes(prepared["old_bytes"]),
            "new_summary": "active=false（撤线，不物理删除）",
            "new_digest": _digest_bytes((json.dumps(new_block, ensure_ascii=False, indent=2)
                                        + "\n").encode("utf-8")),
            "reason": reason.strip(),
        }
        return self._commit("bounds", new_block, prepared["old_bytes"],
                            prepared["signature"], line, raw)

    def acceptance(self, actor: str, reason: str, block: str, path: str, value: Any,
                   restore_value: Any) -> Dict[str, Any]:
        """验收探针：一次调用连写两次——先写 value，再写回 restore_value，两行都记 probe。

        为什么单开这一条：探测原来借 edit(probe=True) 的手，那等于给「为验证而写」留了
        一个和真改值一样宽的入口（还能碰红线、还能不还原）。这里把它收成一次完整往返：
        两行 probe 账行成对出现，history 一眼看得出「探了一次，又原样写回了」。
        红线不许探（block="bounds" 一律拒）：探针是为验证链路，红线只有人逐条加/撤这一条路。
        每天 ≤3 对，按当天 probe 账行数 / 2 计数，超了拒（探针多了就不是验收，是在拿魂当靶场）。
        """
        if not isinstance(actor, str) or not actor.strip():
            return _reject("actor 不能为空：账本要记清是谁探的这一次")
        if not isinstance(reason, str) or not reason.strip():
            return _reject("reason 不能为空：账本要记清为什么探这一次")
        if block not in BLOCK_FILES:
            return _reject("block 只能是 %s" % "/".join(model.BLOCK_NAMES))
        if block == "bounds":
            return _reject_issue(
                PROBE_ON_BOUNDS_FORBIDDEN,
                "probe 不许碰红线：红线段没有探针路径——加线走 add_redline、撤线走 retire_redline，"
                "两条都由人逐条写")
        if not isinstance(path, str) or not path.strip():
            return _reject("path 不能为空：探哪一格要说出来")
        stamp = now_iso()
        try:
            rows = read_history(self._root / HISTORY_FILE)
        except model.SoulLoadError as exc:
            return _reject("history 读不出来，先修账", exc.issues)
        used = probe_pairs_in_day(rows, stamp)
        if used >= PROBE_DAILY_PAIRS:
            return _reject_issue(
                PROBE_DAILY_LIMIT,
                "今天已经探了 %d 对（一对 = 两条 probe 行），上限 %d 对："
                "探针多了就不是验收，是在拿魂当靶场" % (used, PROBE_DAILY_PAIRS))
        first = self.edit(actor=actor, reason=reason, block=block, path=path,
                          value=value, probe=True)
        if not first.get("ok"):
            return first
        second = self.edit(actor=actor, reason=reason, block=block, path=path,
                           value=restore_value, probe=True)
        if not second.get("ok"):
            return {"ok": False,
                    "message": "写回原值失败，盘上现在是探针值（第一行 probe 已落账），"
                               "请人工改正：%s" % second.get("message"),
                    "issues": second.get("issues", []),
                    "history_lines": [first["history_line"]]}
        return {"ok": True,
                "history_lines": [first["history_line"], second["history_line"]],
                "note": "两行都是 kind=probe：一次写入、一次还原；魂回到探之前的值"}

    def _loaded_for_write(self, block: str) -> Dict[str, Any]:
        """写前的共同门：块在不在、当前卡坏不坏、文件是否在载入后被别人改过。

        返回 {"ok": True, raw, old_bytes, new_block} 或拒绝结果。
        """
        target_name = BLOCK_FILES[block]
        target = self._root / target_name
        if _stat_signature(target) != self._signatures.get(target_name):
            return _reject("文件在你编辑期间被改过，请重读后再改：%s" % target_name)
        raw, issues = read_blocks(self._root)
        if errors_only(issues):
            return _reject("当前魂是坏卡，先修好再改", issues)
        if block not in raw:
            return _reject("%s 没读到" % block)
        try:
            old_bytes = target.read_bytes()
        except OSError as exc:
            return _reject("%s 读不出来：%s" % (target_name, exc))
        return {"ok": True, "raw": raw, "old_bytes": old_bytes,
                "new_block": copy.deepcopy(raw[block]), "signature": _stat_signature(target)}

    def _commit(self, block: str, new_block: Dict[str, Any], old_bytes: bytes,
                signature: Any, line: Dict[str, Any], raw: Dict[str, Any],
                stamp: Optional[str] = None) -> Dict[str, Any]:
        """事务：M1 断言 → 整档归档 → 写块 → 追账 → 刷内存快照；账追不上就把块写回原样。

        写盘前的两道前置校验在这里，顺序固定（吸收参考册 M1 + M5）：
        1. 红线段断言不过就不许落盘，拒因随返回值回给调用方（面板直接显示）；
        2. 先取整档归档（previous.json + 环形槽，revision 自增），取不到就整次作废
           （GATE_SNAPSHOT）——归档是「回到哪一版」的唯一凭据，没有它就不许写。

        账的时间戳在写完块之后才取：账不许早于它记的那次改动，否则自检会把
        这次改动读成「改了没记账」。stamp 只有自测注入月份时才会给。
        """
        target = self._root / BLOCK_FILES[block]
        new_bytes = (json.dumps(new_block, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if _stat_signature(target) != signature:
            return _reject("文件在你编辑期间被改过，请重读后再改：%s" % BLOCK_FILES[block])
        text_issues = redline_issues(raw, block, new_block)
        if errors_only(text_issues):
            # 拒因原样带回调用方：面板把 message 与 issues 一起显示，人才知道改哪一行
            return _reject("改完的红线段过不了断言（%s）：%s，未写入"
                           % ("、".join(sorted({item.code for item in text_issues})),
                              text_issues[0].message),
                           text_issues)
        try:
            archived = archive_now(self._root)
        except OSError as exc:
            return _reject_issue(gates.SNAPSHOT, "取整档归档失败，本次改写作废：%s" % exc)
        _atomic_write(target, new_bytes)
        line["ts"] = stamp or now_iso()
        line["revision"] = archived + 1
        try:
            append_history(self._root / HISTORY_FILE, line)
        except Exception as exc:  # noqa: BLE001 - 追账失败就是整次作废，原因要原样带出去
            _atomic_write(target, old_bytes)
            self._reload()
            return _reject("history 追加失败，已回滚，本次改写作废：%s" % exc)
        self._reload()
        return {"ok": True, "history_line": line, "digest": self._snapshot.digest,
                "revision": archived + 1}

    def edit(self, actor: str, reason: str, block: str, path: str, value: Any,
             structural: bool = False, probe: bool = False,
             now: Optional[str] = None) -> Dict[str, Any]:
        """人的手：改 identity / manner / bounds 的一个字段，同事务追一行账。

        只有这三块：六轴卡与基线归心（organs/heart/），不在这张白名单里。

        structural=True 是「这次是经裁决的结构变更」的显式声明：许新建缺的字段、许把被
        搬走的字段/条目拿掉，path 为空还许整块替换。删掉的东西记进账行的 removed，
        红线的删线保护不变（仍只许 active=false）。不声明就照旧一律拒。
        structural 的账行记 kind="migrate"：换形状不是改值。
        bounds 例外（吸收 M3）：红线块不许整块替换，structural 也不许——加线走
        add_redline()，撤线走 retire_redline()，两条都是逐条写入。

        probe=True 是「这次只为验证链路」的显式声明，其余行为一字不变，账行记
        kind="probe"（随后还原时再记一条 probe，两条都留着，不改旧行）。
        两个都为真时 kind="probe"，账行里照样保留 structural 字段。
        probe 这个形参只留给内部与测试：HTTP 与 CLI 都不暴露它（传了就 400，
        PROBE_VIA_EDIT_FORBIDDEN）。人要走验收就走 acceptance()——一次写值 + 一次还原。
        now 同样只给自测用：复燃冷却与账行 ts 取同一个钟。
        """
        if not isinstance(actor, str) or not actor.strip():
            return _reject("actor 不能为空：账本要记清是谁改的")
        if not isinstance(reason, str) or not reason.strip():
            return _reject("reason 不能为空：账本要记清为什么改")
        if block not in BLOCK_FILES:
            return _reject("block 只能是 %s" % "/".join(model.BLOCK_NAMES))
        prepared = self._loaded_for_write(block)
        if not prepared.get("ok"):
            return prepared
        raw = prepared["raw"]
        new_block = prepared["new_block"]
        if not path:
            # 空 path = 整块替换：这就是结构变更本身，不声明就拒
            if not structural:
                return _reject_issue("SOUL_DELETE_ATTEMPT",
                                     "空 path 是整块替换（结构变更），要显式 structural=true")
            old_value = copy.deepcopy(raw[block])
            new_block = copy.deepcopy(value)
        else:
            try:
                tokens = _parse_path(path)
                try:
                    old_value = _get_path(raw[block], tokens)
                except KeyError:
                    if not structural:
                        raise
                    old_value = ""          # 这次新建的字段：账上 old_summary 记空
                _set_path(new_block, tokens, value, create=structural)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                return _reject("path 无法定位：%s（%s）" % (path, exc))
        if block == "bounds":
            attempt = _bounds_deletion(raw["bounds"], new_block, value)
            if attempt:
                # 删线先报：删的拒因比「整块替换」更接近人要干的事
                return _reject_issue("BOUNDS_DELETE_ATTEMPT", attempt)
            if not path or path.strip() == "items":
                # M3：红线块只许逐条加、逐条撤（active=false）。整块替换与整表替换一律拒——
                # 包括压缩产物、迁移结果、「一次性重写整段红线」这类看起来更方便的写法。
                return _reject_issue(
                    "BOUNDS_BLOCK_REPLACE_FORBIDDEN",
                    "红线块不许整块替换（path=%r）：只能逐条加（add_redline）或逐条撤"
                    "（retire_redline），压缩产物也不许覆盖红线块" % (path or "（空 path，整块）"))
            if value is True:
                # 复燃冷却的另一条路：把撤下的线 active 改回 true，和 add_redline 同一把尺子
                revived = _revived_id(raw["bounds"], path)
                if revived:
                    try:
                        rows = read_history(self._root / HISTORY_FILE)
                    except model.SoulLoadError as exc:
                        return _reject("history 读不出来，先修账", exc.issues)
                    rerelease = rerelease_issues(rows, revived, now or now_iso())
                    if rerelease:
                        return _reject("这条红线刚撤下，复燃冷却未满，未写入", rerelease)
        removed = _deletion(raw[block], new_block)
        if removed and not structural:
            return _reject_issue("SOUL_DELETE_ATTEMPT", removed)
        block_issues = model.validate(block, new_block)
        if errors_only(block_issues):
            return _reject("改完这一块过不了校验，未写入", block_issues)
        new_bytes = (json.dumps(new_block, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if probe:
            kind = KIND_PROBE
        elif structural:
            kind = KIND_MIGRATE
        else:
            kind = KIND_EDIT
        line = {
            "kind": kind,
            "actor": actor.strip(),
            "block": block,
            "path": path,
            "old_summary": _summary(old_value),
            "old_digest": _digest_bytes(prepared["old_bytes"]),
            "new_summary": _summary(value),
            "new_digest": _digest_bytes(new_bytes),
            "reason": reason.strip(),
        }
        if structural:
            line["structural"] = True          # 账上写明这次动了结构，不是普通改值
            if removed:
                line["removed"] = removed       # 删掉了什么，一个字都不许凭空消失
        return self._commit(block, new_block, prepared["old_bytes"], prepared["signature"],
                            line, raw, stamp=now)

    def accept_proposal(self, actor: str, proposal: Dict[str, Any],
                        monthly_limit: int = model.PROPOSAL_MONTHLY_LIMIT,
                        now: Optional[str] = None) -> Dict[str, Any]:
        """人的手：接受一份提案。提案只许动 manner.stance（唯一的可写 target）。

        校验顺序：依据记忆恰好三条非空 → target 合法且不落在红线上 →
        本月已接受的提案数未达上限 → 写入 + 追账（账行带 proposal_id 与 evidence）。
        任何一条不过就整次拒绝，不写盘。
        """
        if not isinstance(actor, str) or not actor.strip():
            return _reject("actor 不能为空：接受提案的是人的手，账本要记清是谁")
        if not isinstance(proposal, dict):
            return _reject("proposal 必须是 JSON 对象")
        proposal_id = proposal.get("id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            return _reject("proposal.id 不能为空：账行要能对上这份提案")
        reason = proposal.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return _reject("proposal.reason 不能为空：账本要记清为什么要改基线")
        evidence = proposal.get("evidence")
        if not (isinstance(evidence, list) and len(evidence) == model.limits.proposal_evidence
                and all(isinstance(item, str) and item.strip() for item in evidence)):
            return _reject("提案必须附 %d 条依据记忆 id（非空字符串），否则不可审计"
                           % model.limits.proposal_evidence)
        target = proposal.get("target")
        if isinstance(target, str) and ("bounds" in target or "红线" in target):
            return _reject_issue("PROPOSAL_FROZEN_TARGET",
                                 "红线永久冻结：不可提案、不可软化（target=%s）" % target)
        if "value" not in proposal:
            return _reject("提案缺 value：不知道要改成什么")
        resolved = model.proposal_target(target)
        if resolved is None:
            return _reject("target 只许 manner.stance，实际 %r"
                           "（红线不可提案；六轴卡归心，baseline.* 不是魂的 target）" % (target,))
        block, path = resolved
        try:
            rows = read_history(self._root / HISTORY_FILE)
        except model.SoulLoadError as exc:
            return _reject("history 读不出来，先修账", exc.issues)
        prepared = self._loaded_for_write(block)
        if not prepared.get("ok"):
            return prepared
        block_raw = prepared["raw"]
        new_block = prepared["new_block"]
        try:
            tokens = _parse_path(path)
            old_value = _get_path(block_raw[block], tokens)
            _set_path(new_block, tokens, proposal["value"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return _reject("path 无法定位：%s（%s）" % (path, exc))
        # —— M6 演化五闸（接在「人接受提案」之前的前置机器校验；§5 第 13 条一字不改）——
        # ⑤ 落盘前先存快照由 _commit 兜（取不到归档即 GATE_SNAPSHOT），这里管另外四闸。
        stamp = now or now_iso()
        gate_issues = gates.evidence_issues(evidence)
        gate_issues.extend(gates.step_issues(block, path, old_value, proposal["value"]))
        gate_issues.extend(gates.cooldown_issues(rows, block, path, stamp, iso_to_epoch))
        candidate = dict(block_raw)
        candidate[block] = new_block
        gate_issues.extend(gates.guard_issues(model.parse("bounds", block_raw["bounds"]),
                                             model.parse("bounds", candidate["bounds"])))
        if gate_issues:
            return _reject("演化五闸没过（%s），未写入"
                           % "、".join(sorted({item.code for item in gate_issues})),
                           gate_issues)
        used = len(proposals_in_month(rows, stamp))
        if used >= monthly_limit:
            return _reject_issue("PROPOSAL_MONTHLY_LIMIT",
                                 "本月已接受 %d 份提案，上限 %d 份：改基线不是天天能改的事"
                                 % (used, monthly_limit))
        removed = _deletion(block_raw[block], new_block)
        if removed:
            return _reject_issue("SOUL_DELETE_ATTEMPT", removed)
        block_issues = model.validate(block, new_block)
        if errors_only(block_issues):
            return _reject("提案改完这一块过不了校验，未写入", block_issues)
        new_bytes = (json.dumps(new_block, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        line = {
            "kind": KIND_PROPOSAL,
            "actor": actor.strip(),
            "block": block,
            "path": path,
            "target": target,
            "proposal_id": proposal_id.strip(),
            "evidence": [item.strip() for item in evidence],
            "old_summary": _summary(old_value),
            "old_digest": _digest_bytes(prepared["old_bytes"]),
            "new_summary": _summary(proposal["value"]),
            "new_digest": _digest_bytes(new_bytes),
            "reason": reason.strip(),
        }
        return self._commit(block, new_block, prepared["old_bytes"], prepared["signature"], line,
                            block_raw, stamp=now)


def _digest_hex(text: Any) -> str:
    """人抄过来的 digest：带不带 "sha256:" 前缀都认，比较用十六进制本体。"""
    value = text.strip() if isinstance(text, str) else ""
    return value[len("sha256:"):] if value.lower().startswith("sha256:") else value


def migrate(root: Path, actor: str, reason: str, block: str, new_value: Any,
            expect_old_digest: str) -> Dict[str, Any]:
    """形状变化的唯一入口：宽容读旧卡 → 校验新形状 → 对齐 digest → 写块 + 追账。

    判据：任何形状变化都不许绕开唯一写入口；手工改盘只能当事故补账。

    这里刻意不走 load()/_loaded_for_write()：旧形状在新 model 下就是坏卡，过去三次手工
    改盘全是这么来的。所以旧卡只按磁盘原文读（不要求它通过新校验），新值必须过新 model，
    且换完之后的整张魂要能载入（另外两张卡不许是坏的、六件合计不许超硬线）。
    这里只能是模块级函数：坏卡状态下根本没有能构造出来的 SoulStore。
    """
    root = Path(root)
    if not isinstance(actor, str) or not actor.strip():
        return _reject("actor 不能为空：账本要记清是谁换的形状")
    if not isinstance(reason, str) or not reason.strip():
        return _reject("reason 不能为空：账本要记清为什么换形状")
    if block not in BLOCK_FILES:
        return _reject("block 只能是 %s" % "/".join(model.BLOCK_NAMES))
    if block == "bounds":
        # M3：红线块不许被整块替换，迁移也不例外（它是最容易被当成「顺手清理」的那条路）。
        # 红线只能逐条加 / 逐条撤：加走 add_redline，撤走 retire_redline。
        return _reject_issue(
            "BOUNDS_BLOCK_REPLACE_FORBIDDEN",
            "红线块不许整块替换：migrate 的目标块不得是 bounds——红线只能逐条加/逐条撤，"
            "压缩产物也不许覆盖红线块")
    target = root / BLOCK_FILES[block]
    try:
        old_bytes = target.read_bytes()
    except OSError as exc:
        return _reject("%s 读不出来：%s" % (BLOCK_FILES[block], exc))
    current = _digest_bytes(old_bytes)
    if _digest_hex(expect_old_digest).lower() != current[len("sha256:"):]:
        return _reject_issue("MIGRATE_DIGEST_MISMATCH",
                             "expect_old_digest 对不上 %s 的当前 sha256：要改成别人的东西之前"
                             "先重读（当前 %s）" % (BLOCK_FILES[block], current))
    block_issues = model.validate(block, new_value)
    if errors_only(block_issues):
        return _reject("迁移后的卡过不了新 model，未写入", block_issues)
    others: Dict[str, Any] = {}
    issues: List[model.Issue] = []
    for name in model.BLOCK_NAMES:
        if name == block:
            continue
        path = root / BLOCK_FILES[name]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            issues.append(model.Issue("SOUL_BLOCK_MISSING", model.ERROR,
                                      "%s 读不出来：%s" % (BLOCK_FILES[name], exc)))
            continue
        except json.JSONDecodeError as exc:
            issues.append(model.Issue("SOUL_JSON_UNREADABLE", model.ERROR,
                                      "%s 不是合法 JSON：%s" % (BLOCK_FILES[name], exc)))
            continue
        issues.extend(model.validate(name, data))
        others[name] = data
    if errors_only(issues):
        return _reject("另外两张卡过不了校验，先修它们再迁移 %s" % block, issues)
    final = dict((name, new_value if name == block else others[name])
                 for name in model.BLOCK_NAMES)
    parsed = dict((name, model.parse(name, final[name])) for name in model.BLOCK_NAMES)
    total = model.body_chars(parsed["identity"], parsed["manner"], parsed["bounds"])
    if total > model.limits.body_total:
        return _reject_issue("BUDGET_OVER",
                             "迁移后六件正文合计 %d 字 > %d 字（合计是硬线，超了拒载）"
                             % (total, model.limits.body_total))
    text_issues = redline.check_bounds(parsed["bounds"])
    if errors_only(text_issues):
        return _reject("迁移后的红线段过不了断言（%s）：%s，未写入"
                       % ("、".join(sorted({item.code for item in text_issues})),
                          text_issues[0].message),
                       text_issues)
    try:
        old_value = json.loads(old_bytes.decode("utf-8"))
        old_summary = _summary(old_value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        old_summary = "（旧卡不是合法 JSON，摘要不可给）"
    new_bytes = (json.dumps(new_value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    line = {
        "kind": KIND_MIGRATE,
        "actor": actor.strip(),
        "block": block,
        "path": "",
        "old_summary": old_summary,
        "old_digest": current,
        "new_summary": _summary(new_value),
        "new_digest": _digest_bytes(new_bytes),
        "reason": reason.strip(),
    }
    history_path = root / HISTORY_FILE
    try:
        ledger_before = history_path.read_bytes()
    except OSError:
        ledger_before = b""
    try:
        archived = archive_now(root)      # M5：迁移也是写，写前先存整档
    except OSError as exc:
        return _reject_issue(gates.SNAPSHOT, "取整档归档失败，本次迁移作废：%s" % exc)
    _atomic_write(target, new_bytes)
    line["ts"] = now_iso()
    line["revision"] = archived + 1
    try:
        append_history(history_path, line)
    except Exception as exc:  # noqa: BLE001 - 追账失败就是整次作废，原因要原样带出去
        _atomic_write(target, old_bytes)
        return _reject("history 追加失败，已回滚，本次迁移作废：%s" % exc)
    _, post = read_blocks(root)
    if errors_only(post):
        _atomic_write(target, old_bytes)
        _atomic_write(history_path, ledger_before)   # 账退回追加前：只删掉刚写的那一行
        return _reject("迁移后整张魂载不进去，已回滚（含账行）", post)
    return {"ok": True, "history_line": line, "digest": _digest(root)}


def load(root: Path) -> SoulStore:
    """载入：M1 红线断言在启动时跑一次（每次人改魂后再跑一次，见 _commit）。

    不过就不许起来——断言只覆盖红线段，它过不了，说明有人把禁令写成了建议或写重了。
    """
    root = Path(root)
    raw, issues = read_blocks(root)
    if not errors_only(issues):
        issues = list(issues) + redline.check_bounds(model.parse("bounds", raw["bounds"]))
    if errors_only(issues):
        raise model.SoulLoadError(issues)
    return SoulStore(root, raw, issues)

