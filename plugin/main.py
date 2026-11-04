"""最小壳：定位魂、载入一次、醒时自检打日志。不注册命令、钩子、LLM 工具。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

try:  # AstrBot 以包的形式加载插件，走相对导入
    from .organs.brain import load as load_brain
    from .organs.soul import SoulLoadError, load, model
    from .organs.soul.store import SoulStore
except ImportError:  # 单独 import main（测试、本地脚本）
    from organs.brain import load as load_brain
    from organs.soul import SoulLoadError, load, model
    from organs.soul.store import SoulStore

try:  # 没装 astrbot 的解释器里也必须能 import 本模块
    from astrbot.api import logger
    from astrbot.api.star import Star
except ImportError:
    logger = logging.getLogger(__name__)

    class Star:  # type: ignore[no-redef]
        """降级基类：只为让本模块能被 import，不做任何事。"""

        def __init__(self, context: Any = None, config: Optional[Dict[str, Any]] = None) -> None:
            self.context = context


DEFAULT_SOUL_DIR = "organs/soul/data"
DEFAULT_BRAIN_DIR = "organs/brain/data"
# 宿主把面板挂载点叫成什么，就在这儿认什么；一个都没有就跳过，面板不是起身的前提。
MOUNT_HOOKS = ("mount_panel_routes", "register_panel_routes")


def _conf(context: Any, key: str, default: Any) -> Any:
    """读宿主配置；读不到就退回默认值（配置坏了不该拦住她起身）。"""
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return default
    try:
        conf = getter()
    except Exception as exc:  # noqa: BLE001 - 宿主接口没有 / 配置坏了
        logger.warning("[qqagent] 读宿主配置失败：%s", exc)
        return default
    value = conf.get(key) if hasattr(conf, "get") else None
    return default if value is None else value


def _mount_panel(context: Any, store: Any, brain: Any = None) -> None:
    """宿主给了面板挂载点就把 api/panel.routes() 交出去；没给就跳过，不抛。"""
    if store is None:
        return
    try:
        from .api import panel
    except ImportError:
        from api import panel

    for name in MOUNT_HOOKS:
        try:
            mount = getattr(context, name, None)
        except Exception:  # noqa: BLE001 - 宿主对象的 __getattr__ 可能自己抛
            return
        if callable(mount):
            try:
                mount(panel.routes(store, brain))
            except Exception as exc:  # noqa: BLE001 - 挂不上面板也得让她起身
                logger.warning("[qqagent] 面板挂载失败，核照常跑：%s", exc)
            return


def _report(issue: model.Issue) -> None:
    line = "[qqagent] %s %s" % (issue.code, issue.message)
    if issue.level == model.ERROR:
        logger.error(line)
    else:
        logger.warning(line)


class QQAgent(Star):
    """七器官合体；本轮只把魂接起来。"""

    def __init__(self, context: Any, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(context)
        soul_dir = Path(__file__).resolve().parent / str(_conf(context, "soul_dir", DEFAULT_SOUL_DIR))
        brain_dir = Path(__file__).resolve().parent / str(_conf(context, "brain_dir", DEFAULT_BRAIN_DIR))
        self.store: Optional[SoulStore] = None
        self.brain: Any = None
        try:
            self.store = load(soul_dir)
        except SoulLoadError as exc:
            for issue in exc.issues:
                _report(issue)
            logger.error("[qqagent] 魂载入失败，数据目录 %s", soul_dir)
            return
        try:
            self.brain = load_brain(brain_dir)
        except Exception as exc:  # noqa: BLE001 - 脑起不来也得让她起身
            logger.warning("[qqagent] 脑载入失败，数据目录 %s：%s", brain_dir, exc)
            self.brain = None
        _mount_panel(context, self.store, self.brain)
        if not _conf(context, "enable_selfcheck", True):
            return
        for issue in self.store.check():
            _report(issue)
