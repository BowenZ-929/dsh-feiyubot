#!/usr/bin/env python3
"""一屏看板：她现在的状态。跑在仓库根：python3 tools/status.py

看四件事，都打活进程/活文件，不读缓存：
  1. 核：pid / git HEAD / stale（跑的是不是当前代码）
  2. 她的实盘：各器官有几份文件、有多大
  3. 测试：按目录分别统计条数（全绿才值得信）
  4. 工作区：未提交的文件
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
HOME = ROOT / "home" / "organs"
PORT = os.environ.get("QQAGENT_CORE_PORT", "8787")


def sh(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, timeout=180)
        # unittest 把结果写 stderr，只收 stdout 会永远显示「0 条」
        return (done.stdout or "") + (done.stderr or "")
    except Exception as exc:  # noqa: BLE001 - 看板不该因为一条命令挂了
        return f"(失败：{exc})"


def kernel() -> None:
    print("== 核 ==")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=3) as resp:
            data = json.load(resp)
        stale = data.get("stale")
        print(f"  pid={data.get('pid')} head={data.get('git_head')} stale={stale}")
        if stale:
            print("  ⚠ 源码在进程脚下改过：重启核（kill 后重起）")
    except Exception as exc:  # noqa: BLE001
        print(f"  没应答（{exc}）：核没起")


def live_data() -> None:
    print("== 她的实盘 ==")
    if not HOME.exists():
        print("  home/organs 不存在")
        return
    for organ in sorted(p.name for p in HOME.iterdir() if p.is_dir()):
        files = [p for p in (HOME / organ).rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        print(f"  {organ:<8} {len(files):>3} 份 {size / 1024:>7.1f}K")


def tests() -> None:
    print("== 测试（按目录）==")
    total = 0
    for sub in ("organs", "api", "tests", "."):
        path = PLUGIN / sub if sub != "." else PLUGIN
        if not path.exists():
            continue
        out = sh([sys.executable, "-m", "unittest", "discover", "-s", str(path), "-t", str(PLUGIN), "-p", "test_*.py"], cwd=PLUGIN)
        ran = re.search(r"Ran (\d+) tests", out)
        ok = "OK" in out.splitlines()[-1] if out.strip() else False
        n = int(ran.group(1)) if ran else 0
        total += n
        print(f"  {sub:<8} {n:>5} 条 {'OK' if ok else 'FAILED' if n else '(无)'}")
    print(f"  合计     {total:>5} 条")


def worktree() -> None:
    print("== 工作区 ==")
    out = sh(["git", "status", "--short"]).strip()
    print("  " + ("干净" if not out else out.replace("\n", "\n  ")))


def main() -> int:
    kernel()
    live_data()
    tests()
    worktree()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
