"""复用本机 ADB。参数来自手的固定动作，不接收模型命令或 shell 脚本。"""
import subprocess
import sys


class AdbError(RuntimeError):
    pass


def run(args, *, executable="adb", timeout=8):
    try:
        result = subprocess.run([executable, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AdbError("手机操作超时，结果未确认；请查看屏幕，不自动重试") from exc
    except OSError as exc:
        raise AdbError("无法使用 ADB：" + str(exc)) from exc
    if result.returncode:
        raise AdbError(result.stderr.decode("utf-8", errors="replace").strip()[:400] or "ADB 操作失败")
    return result.stdout


def hierarchy(serial, *, python=None):
    # 固定程序复用现成 uiautomator2 环境；设备地址通过 argv 传递，不拼进程序。
    return run(["-c", "import sys,uiautomator2 as u2;sys.stdout.write(u2.connect(sys.argv[1]).dump_hierarchy())",
                serial], executable=python or sys.executable, timeout=15)
