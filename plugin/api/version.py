"""核的自报身份：跑的是哪一版、什么时候起的、源码有没有在它脚下被改过。

为什么要有这条：本项目已经三次踩到「源码里有、请求 404/500」——核是 Python，启动时把
`organs/*` 与 `api/*` 载进内存，源码改了不重启就还在跑旧代码。规矩（CONTRIBUTING §1.3）
拦不住人，但让进程自己报一句「我的源码指纹和你磁盘上的不一样」，一眼就能看出来。

三个值：
- `started_at`：进程起来的时间（重启就变）
- `git_head`：启动时读到的 HEAD 短哈希（提交了才变）
- `source_digest`：启动时算的源码指纹；`source_digest_now` 是**每次请求现算**的。
  两者不等 = 源码在进程脚下被改过 = 该重启核了。

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
    return {
        "ok": True,
        "agent": "version",
        "readonly": True,
        "started_at": STARTED_AT,
        "pid": PID,
        "git_head": GIT_HEAD,
        "source_digest": BOOT_DIGEST[:12],
        "source_digest_now": now[:12],
        "stale": now != BOOT_DIGEST,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "advice": ("源码在进程脚下被改过：重启核（pkill -f api.devserver 再起）"
                   if now != BOOT_DIGEST else "跑的就是磁盘上这一版"),
    }


def dispatch(route: str) -> Tuple[int, Dict[str, Any]]:
    if route == "/healthz":
        return 200, report()
    return 404, {"ok": False, "message": "版本端点没有这条：%s" % route}

# 验收打点：这一行只为证明指纹会变
