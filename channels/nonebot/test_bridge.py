"""真实 NoneBot 进程 + 模拟 NapCat；全部账号、端口、文件均隔离。"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

import httpx
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ch-drill-nonebot-")
        self.log = open(Path(self.temp.name) / "nonebot.log", "w+")
        self.sockets = asyncio.Queue()
        self.frames = asyncio.Queue()
        self.actions = asyncio.Queue()

        async def business(ws):
            await self.sockets.put(ws)
            try:
                async for raw in ws:
                    await self.frames.put(json.loads(raw))
            except ConnectionClosed:
                pass

        self.server = await serve(business, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "run.py")], cwd=HERE,
            env={**os.environ, "QQAGENT_NONEBOT_PORT": str(self.port),
                 "QQAGENT_BUSINESS_WS": f"ws://127.0.0.1:{port}/ws",
                 "QQAGENT_ONEBOT_TOKEN": "isolated-test-token", "QQAGENT_ONEBOT_TIMEOUT": "0.3"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}")
        for _ in range(100):
            if self.proc.poll() is not None:
                self.log.seek(0)
                self.fail(self.log.read())
            try:
                if (await self.client.get("/healthz")).status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.05)
        else:
            self.fail("NoneBot 启动超时")
        self.napcat = await self.login()
        self.business = await asyncio.wait_for(self.sockets.get(), 3)
        self.reply_task = asyncio.create_task(self.reply())

    async def login(self):
        return await connect(f"ws://127.0.0.1:{self.port}/onebot/v11/ws", additional_headers={
            "X-Self-ID": "900001", "X-Client-Role": "Universal",
            "Authorization": "Bearer isolated-test-token"})

    async def reply(self):
        async for raw in self.napcat:
            action = json.loads(raw)
            await self.actions.put(action)
            name = action["action"]
            if name == "timeout":
                continue
            if name == "get_record" and action.get("params", {}).get("file") == "timeout":
                continue
            if name == "get_msg" and action.get("params", {}).get("message_id") == 4242:
                continue
            if name == "fail":
                ack = {"status": "failed", "retcode": 1200, "wording": "mock rejection", "data": None}
            elif name == "get_msg":
                ack = {"status": "ok", "retcode": 0, "data": {
                    "time": 1, "message_type": "group", "message_id": 42,
                    "real_id": 42, "sender": {"user_id": 900001},
                    "message": [{"type": "text", "data": {"text": "引用原文"}}]}}
            elif name == "get_record":
                ack = {"status": "ok", "retcode": 0, "data": {"base64": "dGVzdC1hdWRpbw=="}}
            else:
                ack = {"status": "ok", "retcode": 0, "data": {"message_id": 98765}}
            await self.napcat.send(json.dumps({**ack, "echo": action["echo"]}))

    async def asyncTearDown(self):
        await self.napcat.close()
        await self.reply_task
        await self.client.aclose()
        self.proc.terminate()
        await asyncio.to_thread(self.proc.wait, 5)
        self.server.close()
        await self.server.wait_closed()
        self.log.seek(0)
        logs = self.log.read()
        self.log.close()
        self.temp.cleanup()
        self.assertNotIn("Traceback", logs, logs)

    async def test_original_segments_and_notice(self):
        segments = [{"type": "reply", "data": {"id": "42"}},
                    {"type": "at", "data": {"qq": "900001"}},
                    {"type": "text", "data": {"text": " 看图和听音频"}},
                    {"type": "image", "data": {"file": "image.jpg", "url": "https://example.invalid/image"}},
                    {"type": "record", "data": {"file": "audio.amr", "url": "https://example.invalid/audio"}}]
        event = {"post_type": "message", "time": 1, "self_id": 900001,
                 "message_type": "group", "sub_type": "normal", "message_id": 100,
                 "group_id": 900002, "user_id": 900003, "font": 0,
                 "raw_message": "test", "sender": {"user_id": 900003, "nickname": "测试者"}, "message": segments}
        await self.napcat.send(json.dumps(event))
        got = await asyncio.wait_for(self.frames.get(), 3)
        self.assertEqual(got["message"], segments)
        self.assertEqual(got["sender"]["nickname"], "测试者")
        self.assertEqual(got["group_id"], 900002)
        notice = {"post_type": "notice", "notice_type": "group_recall", "time": 1,
                  "self_id": 900001, "group_id": 900002, "user_id": 900003,
                  "operator_id": 900003, "message_id": 100}
        await self.napcat.send(json.dumps(notice))
        self.assertEqual(await asyncio.wait_for(self.frames.get(), 3), notice)

    async def test_quote_lookup_cannot_reorder_forwarded_correction(self):
        for scene in ('private', 'group'):
            with self.subTest(scene=scene):
                base = {'post_type': 'message', 'time': 1, 'self_id': 900001,
                        'message_type': scene, 'sub_type': 'friend' if scene == 'private' else 'normal',
                        'user_id': 900003, 'font': 0, 'sender': {'user_id': 900003, 'nickname': '测试者'},
                        **({'group_id': 900002} if scene == 'group' else {})}
                original = [{'type': 'reply', 'data': {'id': '4242'}},
                            {'type': 'text', 'data': {'text': '周六去公园'}}]
                correction = [{'type': 'text', 'data': {'text': '不对，改成周日'}}]
                await self.napcat.send(json.dumps(dict(base, message_id=100, raw_message='周六去公园', message=original)))
                await self.napcat.send(json.dumps(dict(base, message_id=101, raw_message='不对，改成周日', message=correction)))
                first = await asyncio.wait_for(self.frames.get(), 3)
                second = await asyncio.wait_for(self.frames.get(), 3)
                self.assertEqual([100, 101], [first['message_id'], second['message_id']])
                self.assertEqual(original, first['message'])
                self.assertEqual(correction, second['message'])

    async def test_actions_media_and_failure(self):
        for action, params in [
            ("send_private_msg", {"user_id": 900003, "message": [{"type": "text", "data": {"text": "收到"}}]}),
            ("send_group_msg", {"group_id": 900002, "message": [{"type": "image", "data": {"file": "file:///tmp/test.jpg"}}]}),
            ("send_private_msg", {"user_id": 900003, "message": [{"type": "record", "data": {"file": "file:///tmp/test.wav"}}]}),
            ("get_login_info", {}), ("fail", {}),
        ]:
            await self.business.send(json.dumps({"action": action, "params": params, "echo": "original-echo"}))
            ack = await asyncio.wait_for(self.frames.get(), 3)
            outbound = await asyncio.wait_for(self.actions.get(), 3)
            self.assertEqual(outbound["action"], action)
            self.assertEqual(outbound["params"], params)
            self.assertEqual(ack["echo"], "original-echo")
            self.assertEqual(ack["retcode"], 1200 if action == "fail" else 0)
            if action != "fail":
                self.assertEqual(ack["data"]["message_id"], 98765)

    async def test_timeout_is_uncertain_without_retry(self):
        await self.business.send(json.dumps({"action": "timeout", "params": {}, "echo": "uncertain"}))
        await asyncio.wait_for(self.actions.get(), 3)
        await asyncio.wait_for(self.business.wait_closed(), 3)
        self.business = await asyncio.wait_for(self.sockets.get(), 3)
        await asyncio.sleep(0.4)
        self.assertTrue(self.frames.empty(), "不能把超时伪装成明确失败或成功")
        self.assertTrue(self.actions.empty(), "不能重发已不确定的调用")

    async def test_disconnect_reconnect(self):
        await self.napcat.close()
        await self.reply_task
        await asyncio.wait_for(self.business.wait_closed(), 3)
        self.napcat = await self.login()
        self.business = await asyncio.wait_for(self.sockets.get(), 3)
        self.reply_task = asyncio.create_task(self.reply())
        state = (await self.client.get("/healthz")).json()
        self.assertEqual(state["bots"], [{"self_id": "900001", "business_connected": True, "running": True}])

    async def test_wrong_token_rejected(self):
        with self.assertRaises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{self.port}/onebot/v11/ws", additional_headers={
                "X-Self-ID": "900099", "Authorization": "Bearer wrong"}):
                pass

    async def test_readonly_audio_timeout_keeps_business_link(self):
        await self.business.send(json.dumps({"action": "get_record", "params": {"file": "timeout"}, "echo": "read-timeout"}))
        ack = await asyncio.wait_for(self.frames.get(), 2)
        self.assertEqual((ack["echo"], ack["status"]), ("read-timeout", "failed"))
        state = (await self.client.get("/healthz")).json()
        self.assertTrue(state["bots"][0]["business_connected"])
        await self.business.send(json.dumps({"action": "get_msg", "params": {"message_id": 42}, "echo": "still-connected"}))
        ack = await asyncio.wait_for(self.frames.get(), 2)
        self.assertEqual((ack["echo"], ack["retcode"]), ("still-connected", 0))

    async def test_real_business_channel_and_delivery_receipt(self):
        # 替换模拟业务端为项目原有 Node 通道；假核只返回固定句，不调用模型。
        requests = asyncio.Queue()

        async def core(reader, writer):
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            path = header.split(" ")[1]
            length = next(int(line.split(":", 1)[1]) for line in header.split("\r\n") if line.lower().startswith("content-length:"))
            body = json.loads(await reader.readexactly(length))
            await requests.put((path, body))
            response = {"ok": True}
            if path == "/chat":
                response.update(chunks=[{"text": "收到测试消息", "send_at": 0, "gap_before": 0,
                                         "image": {"file": "file:///tmp/test.jpg"}}], delivery_id="a" * 32)
            data = json.dumps(response).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        fake_core = await asyncio.start_server(core, "127.0.0.1", 0)
        core_port = fake_core.sockets[0].getsockname()[1]
        business_port = self.server.sockets[0].getsockname()[1]
        self.server.close()
        await self.server.wait_closed()
        state = Path(self.temp.name) / "state"
        # 模拟耳的识别进程，验证真实 NoneBot→Node→本地工具→核的接线，不下载模型。
        ear = Path(self.temp.name) / "ear"
        ear.mkdir()
        model = Path(self.temp.name) / "asr-model"
        model.mkdir()
        (model / "model.bin").write_text("test-model")
        python = Path(self.temp.name) / "fake-asr"
        python.write_text('#!/usr/bin/env python3\nimport sys,json\nassert sys.stdin.read()=="dGVzdC1hdWRpbw=="\nprint(json.dumps({"status":"ok","source":"local_asr","transcript":"下午三点见"}))\n')
        python.chmod(0o700)
        (ear / "asr.json").write_text(json.dumps({"enabled": True, "python": str(python), "model": str(model),
            "language": "zh", "max_audio_bytes": 5000000, "max_audios": 2, "timeout_seconds": 3, "budget_seconds": 10}))
        with open(Path(self.temp.name) / "business.log", "w+") as log:
            node = subprocess.Popen(["node", str(REPO / "channels/onebot/server.ts"),
                "--port", str(business_port), "--core", f"http://127.0.0.1:{core_port}",
                "--state", str(state), "--home", self.temp.name],
                env={**os.environ, "QQAGENT_CHANNEL_MODE": "send"}, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    health = (await self.client.get("/healthz")).json()
                    if health["bots"][0]["business_connected"]:
                        break
                    await asyncio.sleep(0.1)
                else:
                    self.fail("真实业务通道未连上")
                event = {"post_type": "message", "time": 1, "self_id": 900001,
                    "message_type": "private", "sub_type": "friend", "message_id": 101,
                    "user_id": 900003, "font": 0, "raw_message": "小肥鱼看这个",
                    "sender": {"user_id": 900003, "nickname": "测试者"},
                    "message": [{"type": "text", "data": {"text": "小肥鱼看这个"}},
                                {"type": "face", "data": {"id": "76", "raw": {"faceText": None}}},
                                {"type": "face", "data": {"id": "9998", "raw": {"faceText": "/新表情"}}},
                                {"type": "image", "data": {"file": "test.jpg", "url": "https://example.invalid/test"}},
                                {"type": "record", "data": {"file": "test.amr", "url": "https://example.invalid/audio"}}]}
                await self.napcat.send(json.dumps(event))
                path, body = await asyncio.wait_for(requests.get(), 12)
                self.assertEqual(path, "/chat")
                self.assertTrue(body["delivery_ack"])
                self.assertEqual(body["scope"], "private")
                self.assertIn("[表情:赞][表情:新表情]", body["text"])
                self.assertEqual([item["kind"] for item in body["attachments"]], ["image", "audio"])
                self.assertEqual([item['speaker_id'] for item in body['attachments']], ['900003','900003'])
                self.assertEqual([item['segment_index'] for item in body['attachments']], [0,0])
                self.assertEqual(body["attachments"][1]["transcription"]["transcript"], "下午三点见")
                record = await asyncio.wait_for(self.actions.get(), 3)
                self.assertEqual(record["action"], "get_record")
                self.assertEqual(record["params"], {"file": "test.amr", "out_format": "wav"})
                sent = await asyncio.wait_for(self.actions.get(), 3)
                self.assertEqual(sent["params"]["user_id"], 900003)
                self.assertEqual(sum(part["type"] == "image" for part in sent["params"]["message"]), 1)
                path, ack = await asyncio.wait_for(requests.get(), 3)
                self.assertEqual((path, ack), ("/chat/ack", {"delivery_id": "a" * 32, "text": "收到测试消息", "images": ["file:///tmp/test.jpg"]}))
                await self.napcat.send(json.dumps(event))
                await asyncio.sleep(4)
                self.assertTrue(requests.empty(), "重投同一消息不能重复调用核")
                self.assertTrue(self.actions.empty(), "重投同一消息不能重复发送")
                # 完整 NapCat→NoneBot→业务通道→核：引用超时也不能把后来的更正放前面。
                await self.napcat.send(json.dumps(dict(event, message_id=111, raw_message='周六去公园',
                    message=[{'type': 'reply', 'data': {'id': '4242'}},
                             {'type': 'text', 'data': {'text': '周六去公园'}}])))
                await self.napcat.send(json.dumps(dict(event, message_id=112, raw_message='不对，改成周日',
                    message=[{'type': 'text', 'data': {'text': '不对，改成周日'}}])))
                path, body = await asyncio.wait_for(requests.get(), 8)
                self.assertEqual('/chat', path)
                self.assertEqual(['（引用）周六去公园', '不对，改成周日'],
                                 [row['text'] for row in body['segment']])
                self.assertEqual('none', body['segment'][0]['quote']['source'])
                self.assertEqual(112, body['messageId'])
                lookup = await asyncio.wait_for(self.actions.get(), 3)
                self.assertEqual(('get_msg', 4242), (lookup['action'], lookup['params']['message_id']))
                sent = await asyncio.wait_for(self.actions.get(), 3)
                self.assertEqual('send_private_msg', sent['action'], '引用只查一次，下一项应是回复发送')
                path, ack = await asyncio.wait_for(requests.get(), 3)
                self.assertEqual('/chat/ack', path)
                self.assertEqual('收到测试消息', ack['text'])
                async with httpx.AsyncClient() as client:
                    for kind in ("audio", "image"):
                        result = await client.post(f"http://127.0.0.1:{business_port}/say", json={
                            "scene": "private", "target": 900003,
                            "chunks": [{"text": "媒体测试", kind: {"file": "file:///tmp/test.wav" if kind == "audio" else "file:///tmp/test.jpg"}}]})
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.json()["sent"], 1, result.text)
                        outbound = await asyncio.wait_for(self.actions.get(), 3)
                        self.assertIn("record" if kind == "audio" else "image", [s["type"] for s in outbound["params"]["message"]])
            finally:
                node.terminate()
                await asyncio.to_thread(node.wait, 5)
                fake_core.close()
                await fake_core.wait_closed()


if __name__ == "__main__":
    unittest.main()
