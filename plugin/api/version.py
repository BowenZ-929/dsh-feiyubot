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
from typing import Any, Dict, Iterable, Tuple

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


def _git_dirty() -> bool:
    """工作树有没有未提交改动。不是 git 仓库 / 没装 git / 命令失败一律 False（自报身份不该让核起不来）。"""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_DIR), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


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
    dirty = _git_dirty()
    # stale 只认「提交变了」：进程启动时记下的 HEAD 与此刻不同。HEAD 读不到（不是仓库）就当没变。
    stale = bool(GIT_HEAD != "unknown" and head_now != "unknown" and head_now != GIT_HEAD)
    if stale:
        advice = "进程跑的是旧提交（%s → %s）：必须重启核（pkill -f api.devserver 再起）" % (
            GIT_HEAD, head_now)
    elif dirty:
        advice = "提交没变，但有未提交改动：重启核更保险（不清也算干净，只是跑的是旧字节）"
    else:
        advice = "跑的就是当前提交，且工作树没有未提交改动"
    return {
        "ok": True,
        "agent": "version",
        "readonly": True,
        "started_at": STARTED_AT,
        "pid": PID,
        "git_head": GIT_HEAD,
        "git_head_now": head_now,
        "dirty": dirty,
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
