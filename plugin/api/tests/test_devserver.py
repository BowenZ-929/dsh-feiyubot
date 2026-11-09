"""devserver 的节律线程：起得来、关得掉、抛错不死、摘要落在 stdout。

线程的真相由假 tick 代表：换掉 api.day.run_tick 就换掉了整条路
（devserver 调的就是它，世上没有第二套 tick）——这本身也是"端点与线程共用同一份实现"的证词。
"""
from __future__ import annotations

import io
import http.client
import os
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from api import day as day_api
from api import devserver


class HealthWhileBusyTest(unittest.TestCase):
    def test_health_responds_before_slow_chat_finishes_and_other_business_waits(self):
        entered, release, other_entered = threading.Event(), threading.Event(), threading.Event()
        order = []

        def handle(method, path, body, store, brain=None):
            if path == '/chat':
                order.append('chat_start')
                entered.set()
                release.wait(5)
                order.append('chat_end')
            elif path == '/day/overview':
                other_entered.set()
                order.append('other')
            return 200, {'ok': True}

        server = devserver.HTTPServer(('127.0.0.1', 0), devserver._Handler)
        worker = threading.Thread(target=server.serve_forever)

        def request(path, method='GET', timeout=3):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=timeout)
            try:
                client.request(method, path, body='{}' if method == 'POST' else None)
                response = client.getresponse()
                response.read()
                return response.status
            finally:
                client.close()

        with mock.patch.object(devserver.api, 'handle', handle), ThreadPoolExecutor(max_workers=2) as pool:
            worker.start()
            try:
                chat = pool.submit(request, '/chat', 'POST')
                self.assertTrue(entered.wait(2))
                other = pool.submit(request, '/day/overview')
                for path in ('/soul/check', '/healthz', '/observe/channel', '/observe/channel-runtime', '/eye/status', '/ear/status', '/voice/status'):
                    try:
                        status = request(path, timeout=1)
                    except socket.timeout:
                        self.fail('慢聊天尚未完成时，守护探活被阻塞：' + path)
                    self.assertEqual(200, status)
                self.assertFalse(chat.done(), '探活应在聊天完成前返回')
                self.assertEqual(200,request('/day/proactive/disable',method='POST',timeout=1),
                                 '停止主动发送不能排在慢生成之后')
                self.assertFalse(other_entered.wait(0.1), '普通业务仍保持串行，不能同时改同一份器官状态')
                release.set()
                self.assertEqual(200, chat.result(timeout=2))
                self.assertEqual(200, other.result(timeout=2))
                self.assertEqual(['chat_start', 'chat_end', 'other'], order)
            finally:
                release.set()
                server.shutdown()
                worker.join(2)
                server.server_close()


def _payload(calls: int) -> Dict[str, Any]:
    """tick 的返回体只留摘要要用的那几个键，其余是空的：线程不该依赖更多。"""
    return {"ok": True, "now": "2026-11-05T09:30:00+09:00", "day": "2026-11-05",
            "ticks": calls, "due": [],
            "night": {"executed": [], "retried": [], "never_ran": [], "expired": []},
            "proactive": {"rows": [], "actions": {}},
            "proactive_consider": {"ok": True, "fired": False, "skipped": "no-source"}}


class TickThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(devserver.stop_tick_thread, 2.0)
        self.addCleanup(os.environ.pop, devserver.TICK_OFF_ENV, None)
        self.addCleanup(os.environ.pop, devserver.TICK_SECONDS_ENV, None)
        os.environ.pop(devserver.TICK_OFF_ENV, None)
        os.environ.pop(devserver.TICK_SECONDS_ENV, None)
        self.calls: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

    def _patch(self, side_effect: Optional[Any] = None) -> None:
        """把 run_tick 换成记账的假的；抛错由 side_effect(第几次) 决定。"""

        def fake(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            self.calls.append((args, kwargs))
            if side_effect is not None:
                side_effect(len(self.calls))
            return _payload(len(self.calls))

        patcher = mock.patch.object(day_api, "run_tick", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_for(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.calls) >= count:
                return True
            time.sleep(0.01)
        return False

    def test_first_tick_is_immediate_then_each_interval(self) -> None:
        """起线程就立刻跑一次（不等一个间隔），之后按间隔再来；每次 stdout 留一行。"""
        self._patch()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            info = devserver.start_tick_thread(every=0.05)
            self.assertTrue(info["started"], info)
            self.assertEqual(0.05, info["interval_seconds"])
            self.assertTrue(self._wait_for(2), "一起身就该跑第一次，之后每个间隔一次")
        args, kwargs = self.calls[0]
        self.assertEqual((), args)
        self.assertNotIn("now", kwargs, "线程只按当前时间 tick，不许自己塞时刻")
        self.assertIn("[devserver] tick at=2026-11-05T09:30:00+09:00", buffer.getvalue(),
                      "每次 tick 要在 stdout 留一行（有 now、有摘要）")
        self.assertIn("day=2026-11-05", buffer.getvalue())
        self.assertTrue(devserver.stop_tick_thread(), "关得掉")

    def test_no_tick_env_turns_the_thread_off(self) -> None:
        """QQAGENT_NO_TICK 置上就一次都不调：测试靠它避免后台线程插手既有用例。"""
        self._patch()
        os.environ[devserver.TICK_OFF_ENV] = "1"
        info = devserver.start_tick_thread(every=0.02)
        self.assertFalse(info["started"], info)
        self.assertIn(devserver.TICK_OFF_ENV, str(info["message"]))
        time.sleep(0.2)
        self.assertEqual([], self.calls)

    def test_one_bad_tick_does_not_kill_the_loop(self) -> None:
        """第一次抛错只记一行：循环不许死，第二次仍要被调到。"""

        def boom(call_no: int) -> None:
            if call_no == 1:
                raise RuntimeError("夜跑炸了")

        self._patch(side_effect=boom)
        info = devserver.start_tick_thread(every=0.02)
        self.assertTrue(info["started"], info)
        self.assertTrue(self._wait_for(2), "第一次抛错之后，第二次仍要被调到")
        self.assertTrue(devserver.stop_tick_thread(), "循环活着，所以关得掉")

    def test_interval_comes_from_env_then_from_config(self) -> None:
        """间隔先看环境，坏值退回配置：代码里不留写死的秒数。"""
        os.environ[devserver.TICK_SECONDS_ENV] = "7"
        self.assertEqual(7.0, devserver.tick_seconds())
        os.environ[devserver.TICK_SECONDS_ENV] = "不是数字"
        self.assertGreater(devserver.tick_seconds(), 0, "坏值回配置里的 tick_seconds")


if __name__ == "__main__":
    unittest.main()
