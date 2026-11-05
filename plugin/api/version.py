"""核的自报身份：跑的是哪一版、什么时候起的、源码有没有在它脚下被改过。

为什么要有这条：本项目已经三次踩到「源码里有、请求 404/500」——核是 Python，启动时把
`organs/*` 与 `api/*` 载进内存，源码改了不重启就还在跑旧代码。规矩（CONTRIBUTING §1.3）
拦不住人，但让进程自己报一句「我的源码指纹和你磁盘上的不一样」，一眼就能看出来。

四件事，分得开才算诚实：
- `started_at`：进程起来的时间（重启就变）
- `git_head` / `git_head_now`：启动时读到的 HEAD 与此刻的 HEAD；**两者不等 = stale**。
- `dirty`：工作树有未提交改动。它**不参与 stale**——dirty 时进程可能跑旧字节，但那只是
  「建议重启」，不是「一定在跑旧代码」。把它算进 stale，等于每改一行就报一次假警，
  而假警多了，真 stale 就没人看。
- `source_digest` / `source_digest_now`：源码指纹，只作诊断，不再决定 stale。

只读端点 `GET /healthz`。不碰 organs，只读它们的源码文件算指纹。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

VERSION_ROUTES: Dict[str, str] = {"/healthz": "GET"}

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = PLUGIN_DIR.parent
# 只算会进内存的 Python 源码；页面是每次请求现读磁盘，不需要重启，所以不进指纹。
SOURCE_DIRS: Tuple[str, ...] = ("api", "organs")
SKIP_PARTS = {"__pycache__", "tests", "data"}

STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
PID = os.getpid()


def _source_files() -> Iterable[Path]:
    for name in SOURCE_DIRS:
        root = PLUGIN_DIR / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if SKIP_PARTS & set(path.relative_to(PLUGIN_DIR).parts):
                continue
            yield path


def source_digest() -> str:
    """源码指纹：路径 + 内容一起进 sha256，任一文件改动都会变。"""
    digest = hashlib.sha256()
    for path in _source_files():
        digest.update(path.relative_to(PLUGIN_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


#: dirty 只看代码与文档树：参考资料（仓库根下 参考文档和仓库/ 这类）不是代码，
#: 它天天被人改，算进来只会让 dirty 长期亮着——信号只该反映它能代表的东西。
DIRTY_SCOPE: Tuple[str, ...] = ("plugin/", "docs/", "tools/", "shell/")
DIRTY_FILES: Tuple[str, ...] = ("her.sh",)


def _scoped(path: str) -> bool:
    """这条改动算不算"代码与文档树"里的。"""
    return path in DIRTY_FILES or path.startswith(DIRTY_SCOPE)


def _dirty_report(raw: str) -> Tuple[bool, int]:
    """把 git status --porcelain -z 的输出分成两堆：(代码树有没有改, 非代码文件改了几个)。

    -z 的路径是原样的 UTF-8，不会被加引号转义（中文目录名也能正确分类）；
    重命名/复制条目占两段（新名 + 旧名），一起算作这一条改动。
    """
    paths: List[str] = []
    tokens = [token for token in raw.split("\0") if token]
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        status = entry[:2]
        paths.append(entry[3:] if len(entry) > 3 else "")
        if status[:1] in ("R", "C") and index + 1 < len(tokens):
            paths.append(tokens[index + 1])
            index += 1
        index += 1
    dirty = any(_scoped(path) for path in paths)
    other = sum(1 for path in paths if not _scoped(path))
    return dirty, other


def _git_dirty() -> Tuple[bool, int]:
    """(代码树有没有未提交改动, 非代码文件改了几个)。

    不是 git 仓库 / 没装 git / 命令失败一律 (False, 0)（自报身份不该让核起不来）。
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_DIR), "status", "--porcelain", "-z"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False, 0
    if done.returncode != 0:
        return False, 0
    return _dirty_report(done.stdout)


def _git_head() -> str:
    """HEAD 短哈希。不是 git 仓库、没装 git、或命令失败都退回 unknown——自报身份不该让核起不来。"""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = done.stdout.strip()
    return head if done.returncode == 0 and head else "unknown"


GIT_HEAD = _git_head()
BOOT_DIGEST = source_digest()


def report() -> Dict[str, Any]:
    """现算一次，和启动时那份比：不等就是「你在跑旧代码」。"""
    now = source_digest()
    head_now = _git_head()
    dirty, dirty_other = _git_dirty()
    # stale 只认「提交变了」：进程启动时记下的 HEAD 与此刻不同。HEAD 读不到（不是仓库）就当没变。
    stale = bool(GIT_HEAD != "unknown" and head_now != "unknown" and head_now != GIT_HEAD)
    others = ("另有 %d 个非代码文件有改动（参考资料之类，不计入 dirty）" % dirty_other
              if dirty_other else "")
    if stale:
        advice = "进程跑的是旧提交（%s → %s）：必须重启核（pkill -f api.devserver 再起）" % (
            GIT_HEAD, head_now)
    elif dirty:
        advice = "提交没变，但代码与文档树有未提交改动：重启核更保险（跑的是旧字节）"
    else:
        advice = "跑的就是当前提交，代码与文档树没有未提交改动"
    if others:
        advice = advice + "；" + others
    return {
        "ok": True,
        "agent": "version",
        "readonly": True,
        "started_at": STARTED_AT,
        "pid": PID,
        "git_head": GIT_HEAD,
        "git_head_now": head_now,
        "dirty": dirty,
        "dirty_other": dirty_other,
        "source_digest": BOOT_DIGEST[:12],
        "source_digest_now": now[:12],
        "stale": stale,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "advice": advice,
    }


def dispatch(route: str) -> Tuple[int, Dict[str, Any]]:
    if route == "/healthz":
        return 200, report()
    return 404, {"ok": False, "message": "版本端点没有这条：%s" % route}

# 验收打点：这一行只为证明指纹会变
