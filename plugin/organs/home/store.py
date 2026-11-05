"""家的落盘底座：根目录只派生一次，写一律同目录 .tmp + os.replace，读一律不静默吞坏行。

根目录三级：构造参数 > 环境变量 HOME_ROOT > <仓库>/home/organs/home。测试只走前两级——
实盘在仓库根的 home/ 底下（见 organs/paths.py），会天天变，不入库。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import paths

ENV_ROOT = "HOME_ROOT"
TMP_PREFIX = ".tmp-"
FILE_MODE = 0o600
DIR_MODE = 0o700
# encodeURIComponent 不转义的字符集，照 JS 规范一字不动（少了任何一个都会与语言自带的实现分叉）
URI_SAFE = frozenset(string.ascii_letters + string.digits + "-_.!~*'()")
_CHUNK = 1 << 20


class StoreError(Exception):
    """落盘失败一律带码：调用方要分得清「缺件」「形状不对」和「写不进去」。"""

    def __init__(self, code: str, detail: Any = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.detail)


def home_root(root=None) -> Path:
    """家的根：构造参数 > HOME_ROOT > <仓库>/home/organs/home。**没有回退到出厂默认的路。**

    三条硬规矩，缺一条「唯一真源」就立不住：

    1. **不回落**。解析不出来就抛，绝不退回 `plugin/organs/home/data`。回落就是第二条真源：
       干净检出时实盘与出厂那份会指到同一个文件，那时「改出厂等于改实盘」，
       `anchors` / `access` / `rooms` 三份「改出厂读不变」的断言会全部失效。
    2. **门禁**。落在 <仓库> 里却不在 <仓库>/home 下的路径一律拒——那是出厂默认参数目录（只读），
       不是活人的实盘。与 `api/roots.py` 第 1 条同向；这里不额外要求「必须放在仓库里」，
       因为检验与部署都把实盘放在仓库外。
    3. **要存在**。`HOME_ROOT` 与默认根都必须是一个已存在的目录，否则拒绝启动，不替人建一个。
       显式构造参数不查存在（调用方自己点过名），但门禁照样过。
    """
    if root is not None:
        return _guard(root, "构造参数", must_exist=False)
    return _guard_env(os.environ.get(ENV_ROOT), ENV_ROOT, paths.organ_dir("home"), must_exist=True)


def _guard(path, origin: str, *, must_exist: bool = False) -> Path:
    """把 `paths.guard_root` 的报错翻成本器官的 StoreError。

    **判断与错误码都不在这里**——它们只长在 `organs/paths.py` 一处；这里只是把
    `paths.PathError` 翻成 `StoreError`，别顺手把规矩再写一遍（那是分叉的起点）。
    """
    try:
        return paths.guard_root(path, origin, must_exist=must_exist)
    except paths.PathError as exc:
        raise StoreError(exc.code, exc.message) from exc


def _guard_env(raw, origin: str, default, *, must_exist: bool = False) -> Path:
    """同上，只是多一层环境变量覆盖：空串规则也在 paths 里，这里同样只做翻译。"""
    try:
        return paths.guard_root_env(raw, origin, default, must_exist=must_exist)
    except paths.PathError as exc:
        raise StoreError(exc.code, exc.message) from exc


def factory_path(relative) -> Path:
    """**出厂默认**（随包发、只读参数）：plugin/organs/home/data/<relative>。

    它与实盘的区别是「可重算／可重装」，不是「另一份真源」。读「她现在是什么样」的路
    一律不许走这里——谁想读它，得先说明自己在补件。
    """
    return paths.factory_dir("home") / Path(str(relative))


def ensure_dir(path: Path) -> None:
    """建目录并钉 0700：umask 会把 mkdir 的 mode 打折，所以建完再 chmod 一次。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), DIR_MODE)


def _tmp_path(path: Path) -> Path:
    return path.parent / ("%s%d-%s" % (TMP_PREFIX, os.getpid(), secrets.token_hex(6)))


def atomic_write_text(path: Path, text: str) -> None:
    """整份换掉一个文件：先写同目录临时件再 os.replace，中途崩了也不会留下半个文件。"""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = _tmp_path(path)
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(tmp), FILE_MODE)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def append_line(path: Path, obj: dict) -> int:
    """追加一行 JSON，返回追加后的总行数。这不是原子覆盖：审计与卡都要能只增。"""
    path = Path(path)
    ensure_dir(path.parent)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return line_count(path)


def read_lines(path: Path) -> Tuple[List[dict], int]:
    """读成 (好行, 坏行数)。坏行只计数不丢——外面手改过的账要能被指出来，不许静默跳过。"""
    path = Path(path)
    if not path.exists():
        return [], 0
    good: List[dict] = []
    bad = 0
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            text = raw.strip()
            if not text:
                # 追加永远以换行收尾；空行是外面手改留下的疤，不算一行记录。
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            good.append(obj)
    return good, bad


def line_count(path: Path) -> int:
    """数的是记录行，不是字节里的换行数——空行不算，与 read_lines 同一把尺。"""
    path = Path(path)
    if not path.exists():
        return 0
    total = 0
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if raw.strip():
                total += 1
    return total


def encode_name(raw: str, limit: int = 160) -> str:
    """encodeURIComponent 等价：不自己发明十六进制编码，截断也不许切在 %XX 中间。

    自造编码踩过：á 编成 ue1，与字面 ue1 相撞，两件东西落到同一个文件上。
    """
    if not isinstance(raw, str):
        raise StoreError("NAME_NOT_TEXT", type(raw).__name__)
    out: List[str] = []
    for char in raw:
        if char in URI_SAFE:
            out.append(char)
        else:
            for byte in char.encode("utf-8"):
                out.append("%%%02X" % byte)
    text = "".join(out)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 尾巴上半个 %XX 拿到别处会解成另一个名字，所以退回最后一个完整转义之后
    index = cut.rfind("%")
    while index != -1 and len(cut) - index < 3:
        cut = cut[:index]
        index = cut.rfind("%")
    if not cut:
        raise StoreError("NAME_TOO_LONG", {"limit": limit, "raw": raw})
    return cut


def sha256_file(path: Path) -> str:
    """逐块算，别把大件整个读进内存。"""
    path = Path(path)
    if not path.exists():
        raise StoreError("FILE_MISSING", str(path))
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
