"""念的测试：一律注入假 transport，绝不发真网络请求。

每个用例都把 paths.HOME 钉到临时目录，所以本机 <仓库>/home/.env 与 os.environ 里
真有没有 key 都影响不到这里——「没配 key」这条能被稳定复现，靠的就是这件事。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organs.mind import llm
from organs.mind.llm import LLMConfigError, LLMError, complete, load_config, parse_env, read_key

# 一份形状齐全的配置：测试里可以随便换 model/base_url/retries，用不着碰磁盘
CONFIG = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "base_url": "http://model.test/v1",
    "timeout_ms": 30000,
    "retries": 2,
    "key_env": "QQAGENT_LLM_KEY",
    "temperature": 0.8,
    "max_tokens": 400,
}

MESSAGES = [{"role": "user", "content": "你在吗"}]


def conf(**change):
    """换几个字段再做一份配置（不就地改 CONFIG，免得用例之间串味）。"""
    fresh = dict(CONFIG)
    fresh.update(change)
    return fresh


def ok(content):
    """一次像样的 200：OpenAI 兼容的 choices[0].message.content。"""
    body = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)
    return 200, body.encode("utf-8")


class FakeTransport:
    """假投递：按顺序回放应答（用完了就重复最后一条），并把每次收到的参数记下来。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "payload": payload, "timeout": timeout})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class LLMTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        # 仓库根的 home/.env 不许漏进测试
        self.repo_home = self.tmp / "repo-home"
        patcher = mock.patch.object(llm.paths, "HOME", self.repo_home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class EnvParsingTest(LLMTest):
    def test_parse_env_skips_blank_and_comments(self):
        pairs = parse_env("# 注释\n\nQQAGENT_LLM_KEY=sk-1\n   \nOTHER=x\n")
        self.assertEqual(pairs["QQAGENT_LLM_KEY"], "sk-1")
        self.assertEqual(pairs["OTHER"], "x")

    def test_parse_env_ignores_export_and_strips_paired_quotes(self):
        pairs = parse_env('export QQAGENT_LLM_KEY="sk-quoted"\nexport PLAIN=  单引号\n'
                          "SQ='sk-sq'\nHALF=\"sk-half\n")
        self.assertEqual(pairs["QQAGENT_LLM_KEY"], "sk-quoted")
        self.assertEqual(pairs["SQ"], "sk-sq")
        self.assertEqual(pairs["PLAIN"], "单引号")
        self.assertEqual(pairs["HALF"], '"sk-half')   # 不配对的引号原样留着

    def test_parse_env_keeps_empty_value_as_empty(self):
        self.assertEqual(parse_env("QQAGENT_LLM_KEY=\n")["QQAGENT_LLM_KEY"], "")

    def test_find_env_file_prefers_qqagent_home(self):
        home_env = self.write(self.tmp / "live" / ".env", "QQAGENT_LLM_KEY=from-home\n")
        self.write(self.repo_home / ".env", "QQAGENT_LLM_KEY=from-repo\n")
        self.assertEqual(llm.find_env_file({"QQAGENT_HOME": str(self.tmp / "live")}), home_env)

    def test_find_env_file_falls_back_to_repo_home(self):
        repo_env = self.write(self.repo_home / ".env", "QQAGENT_LLM_KEY=from-repo\n")
        self.assertEqual(llm.find_env_file({}), repo_env)
        self.assertEqual(llm.find_env_file({"QQAGENT_HOME": str(self.tmp / "none")}), repo_env)

    def test_find_env_file_is_none_when_no_candidate_exists(self):
        with mock.patch.object(llm.paths, "HOME", self.tmp / "empty-home"):
            self.assertIsNone(llm.find_env_file({"QQAGENT_HOME": str(self.tmp / "none")}))


class KeyTest(LLMTest):
    def test_missing_key_names_key_and_every_absolute_path(self):
        places = [str(self.repo_home / ".env"), str(self.tmp / "live" / ".env")]
        env = {"QQAGENT_HOME": str(self.tmp / "live")}
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG, env=env,
                     transport=FakeTransport(ok("不该走到这里")))
        err = caught.exception
        self.assertEqual(err.kind, "no-key")
        self.assertIn("QQAGENT_LLM_KEY", err.message)
        for place in places:
            self.assertTrue(Path(place).is_absolute())
            self.assertIn(place, err.message)
        self.assertNotEqual(err.message, "")          # 缺 key 绝不静默返回空串

    def test_missing_key_never_returns_empty_string(self):
        with self.assertRaises(LLMError):
            complete(MESSAGES, config=CONFIG, env={}, transport=FakeTransport(ok("x")))

    def test_env_file_value_wins_over_environment_variable(self):
        self.write(self.tmp / "live" / ".env", "# 我的 key\nexport QQAGENT_LLM_KEY='sk-file'\n")
        keep = FakeTransport(ok("嗯"))
        text = complete(MESSAGES, config=CONFIG,
                        env={"QQAGENT_HOME": str(self.tmp / "live"),
                             "QQAGENT_LLM_KEY": "sk-env"},
                        transport=keep)
        self.assertEqual(text, "嗯")
        self.assertEqual(keep.calls[0]["headers"]["Authorization"], "Bearer sk-file")

    def test_environment_variable_is_the_fallback(self):
        keep = FakeTransport(ok("在"))
        complete(MESSAGES, config=CONFIG, env={"QQAGENT_LLM_KEY": "sk-env"}, transport=keep)
        self.assertEqual(keep.calls[0]["headers"]["Authorization"], "Bearer sk-env")

    def test_empty_value_in_env_file_counts_as_missing(self):
        self.write(self.tmp / "live" / ".env", "QQAGENT_LLM_KEY=\n")
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG,
                     env={"QQAGENT_HOME": str(self.tmp / "live")},
                     transport=FakeTransport(ok("x")))
        self.assertEqual(caught.exception.kind, "no-key")

    def test_read_key_takes_config_key_env_name(self):
        keep = FakeTransport(ok("好"))
        complete(MESSAGES, config=conf(key_env="MY_KEY"), env={"MY_KEY": "sk-mine"}, transport=keep)
        self.assertEqual(keep.calls[0]["headers"]["Authorization"], "Bearer sk-mine")


class ErrorKindTest(LLMTest):
    def test_auth_is_not_retried(self):
        bad = FakeTransport((401, b'{"error":{"message":"invalid api key"}}'))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG, env={"QQAGENT_LLM_KEY": "k"}, transport=bad)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("模型 key 不对或没权限", caught.exception.message)
        self.assertEqual(len(bad.calls), 1)            # 401 只调一次，不重试

    def test_forbidden_is_auth(self):
        bad = FakeTransport((403, b"no"))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG, env={"QQAGENT_LLM_KEY": "k"}, transport=bad)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(len(bad.calls), 1)

    def test_timeout_retries_exactly_retries_plus_one(self):
        late = FakeTransport(TimeoutError("愣住"))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=conf(retries=2), env={"QQAGENT_LLM_KEY": "k"}, transport=late)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIn("模型没在时限内回话", caught.exception.message)
        self.assertEqual(len(late.calls), 3)           # retries=2 -> 一共 3 次

    def test_timeout_with_zero_retries_calls_once(self):
        late = FakeTransport(TimeoutError("愣住"))
        with self.assertRaises(LLMError):
            complete(MESSAGES, config=conf(retries=0), env={"QQAGENT_LLM_KEY": "k"}, transport=late)
        self.assertEqual(len(late.calls), 1)

    def test_rate_limit_is_retried(self):
        busy = FakeTransport((429, b"slow down"))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=conf(retries=1), env={"QQAGENT_LLM_KEY": "k"}, transport=busy)
        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertIn("模型限流了，等下再试", caught.exception.message)
        self.assertEqual(len(busy.calls), 2)           # retries=1 -> 一共 2 次

    def test_server_error_is_retried_then_succeeds(self):
        flaky = FakeTransport((500, b"boom"), ok("她醒了"))
        text = complete(MESSAGES, config=conf(retries=2), env={"QQAGENT_LLM_KEY": "k"},
                        transport=flaky)
        self.assertEqual(text, "她醒了")
        self.assertEqual(len(flaky.calls), 2)

    def test_client_error_is_not_retried_and_body_is_clipped(self):
        long_body = b"x" * 400
        bad = FakeTransport((400, long_body))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=conf(retries=3), env={"QQAGENT_LLM_KEY": "k"}, transport=bad)
        err = caught.exception
        self.assertEqual(err.kind, "http")
        self.assertEqual(len(bad.calls), 1)
        self.assertIn("HTTP 400", err.message)
        self.assertEqual(err.message.count("x"), 200)  # 响应体截到 200 字

    def test_empty_content_raises_instead_of_returning_blank(self):
        blank = FakeTransport(ok("   "))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=conf(retries=2), env={"QQAGENT_LLM_KEY": "k"}, transport=blank)
        self.assertEqual(caught.exception.kind, "empty")
        self.assertIn("模型回了空内容", caught.exception.message)
        self.assertEqual(len(blank.calls), 1)          # empty 不重试

    def test_missing_content_field_is_empty(self):
        hollow = FakeTransport((200, b'{"choices":[{"message":{}}]}'))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG, env={"QQAGENT_LLM_KEY": "k"}, transport=hollow)
        self.assertEqual(caught.exception.kind, "empty")

    def test_str_of_error_equals_message(self):
        plain = FakeTransport((401, b""))
        with self.assertRaises(LLMError) as caught:
            complete(MESSAGES, config=CONFIG, env={"QQAGENT_LLM_KEY": "k"}, transport=plain)
        self.assertEqual(str(caught.exception), caught.exception.message)
        self.assertEqual(caught.exception.kind, "auth")


class SuccessTest(LLMTest):
    def test_success_returns_text_and_sends_openai_shape(self):
        keep = FakeTransport(ok("  我在。  "))
        text = complete(MESSAGES, config=conf(base_url="http://model.test/v1/"),
                        env={"QQAGENT_LLM_KEY": "sk-1"}, transport=keep)
        self.assertEqual(text, "我在。")
        call = keep.calls[0]
        self.assertEqual(call["url"], "http://model.test/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-1")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(set(call["payload"]), {"model", "messages", "temperature", "max_tokens"})
        self.assertEqual(call["payload"]["model"], "deepseek-chat")
        self.assertEqual(call["payload"]["messages"], MESSAGES)
        self.assertEqual(call["payload"]["temperature"], 0.8)
        self.assertEqual(call["payload"]["max_tokens"], 400)
        self.assertEqual(call["timeout"], 30.0)        # timeout_ms 30000 -> 30 秒


class TemperatureTest(LLMTest):
    """温度的三种来路：没给走配置默认值、给数字就覆盖、给了坏值当场 config 错。"""

    def test_default_temperature_comes_from_the_config(self):
        keep = FakeTransport(ok("在"))
        complete(MESSAGES, config=conf(temperature=0.7), env={"QQAGENT_LLM_KEY": "k"},
                 transport=keep)
        self.assertEqual(0.7, keep.calls[0]["payload"]["temperature"])

    def test_explicit_temperature_wins_and_still_goes_into_the_payload(self):
        keep = FakeTransport(ok("在"))
        complete(MESSAGES, config=conf(temperature=0.7), env={"QQAGENT_LLM_KEY": "k"},
                 transport=keep, temperature=0.5)
        self.assertEqual(0.5, keep.calls[0]["payload"]["temperature"])

    def test_bad_temperature_is_a_config_error(self):
        with self.assertRaises(LLMConfigError):
            complete(MESSAGES, config=conf(), env={"QQAGENT_LLM_KEY": "k"},
                     transport=FakeTransport(), temperature="冷一点")

    def test_factory_temperature_is_the_default_tier(self):
        """不传 temperature 时，出厂数据那一格（0.7）就是默认档。"""
        keep = FakeTransport(ok("在"))
        with mock.patch.dict(llm.os.environ, {}, clear=True):
            complete(MESSAGES, env={"QQAGENT_LLM_KEY": "k"}, transport=keep)
        self.assertEqual(0.7, keep.calls[0]["payload"]["temperature"])

    def test_tier_choice_follows_the_data_trigger(self):
        """两条判据都在数据里：回想非空 + 预算不超 90 才降成事实档。"""
        with mock.patch.dict(llm.os.environ, {}, clear=True):
            policy = llm.temperature_policy()
        self.assertEqual(("factual", 0.5), llm.choose_temperature(
            policy, recalled_nonempty=True, reply_budget=90))
        self.assertEqual(("default", 0.7), llm.choose_temperature(
            policy, recalled_nonempty=True, reply_budget=220))
        self.assertEqual(("default", 0.7), llm.choose_temperature(
            policy, recalled_nonempty=False, reply_budget=40))

    def test_injected_config_with_its_own_tiers_is_the_policy(self):
        """注入的配置自带 temperature_tiers 就用它——不必去读盘上的那一份。"""
        mine = conf(temperature=0.9)
        mine["temperature_tiers"] = {"factual": 0.1,
                                     "trigger": {"recalled_nonempty": True,
                                                 "reply_budget_at_most": 5}}
        self.assertEqual(0.1, llm.temperature_policy(mine)["temperature_tiers"]["factual"])
        self.assertEqual(("factual", 0.1), llm.choose_temperature(
            llm.temperature_policy(mine), recalled_nonempty=True, reply_budget=5))

    def test_unknown_trigger_key_is_a_config_error(self):
        """数据里写了代码不认的判据就当场报：静默忽略等于这一档永远不生效。"""
        data = conf()
        data["temperature_tiers"] = {"factual": 0.5,
                                     "trigger": {"moon_is_full": True}}
        with self.assertRaises(LLMConfigError) as caught:
            llm.temperature_policy(data)
        self.assertIn("moon_is_full", caught.exception.message)

    def test_factual_is_not_a_boolean(self):
        data = conf()
        data["temperature_tiers"] = {"factual": True, "trigger": {"recalled_nonempty": True}}
        with self.assertRaises(LLMConfigError):
            llm.temperature_policy(data)


class MessageShapeTest(LLMTest):
    def test_empty_messages_is_config_error(self):
        with self.assertRaises(LLMError) as caught:
            complete([], config=CONFIG, env={"QQAGENT_LLM_KEY": "k"}, transport=FakeTransport())
        self.assertEqual(caught.exception.kind, "config")

    def test_all_blank_messages_is_config_error(self):
        with self.assertRaises(LLMError) as caught:
            complete([{"role": "user", "content": "  "}], config=CONFIG,
                     env={"QQAGENT_LLM_KEY": "k"}, transport=FakeTransport())
        self.assertEqual(caught.exception.kind, "config")

    def test_bad_item_is_config_error(self):
        with self.assertRaises(LLMError) as caught:
            complete([{"content": "无角色"}], config=CONFIG,
                     env={"QQAGENT_LLM_KEY": "k"}, transport=FakeTransport())
        self.assertEqual(caught.exception.kind, "config")


class ConfigFileTest(LLMTest):
    def test_factory_file_is_deepseek(self):
        with mock.patch.dict(llm.os.environ, {}, clear=True):
            factory = load_config()
        self.assertEqual(factory["provider"], "deepseek")
        self.assertEqual(factory["model"], "deepseek-flash")
        self.assertEqual(factory["thinking"], {"type": "enabled"})
        self.assertEqual(factory["reasoning_effort"], "high")
        self.assertEqual(factory["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(factory["timeout_ms"], 45000)
        self.assertEqual(factory["retries"], 0)
        self.assertEqual(factory["key_env"], "QQAGENT_LLM_KEY")
        self.assertEqual(factory["temperature"], 0.7)
        self.assertEqual(factory["max_tokens"], 8192)

    def test_factory_file_carries_the_two_temperature_tiers(self):
        """两个温度（0.7 / 0.5）与触发条件都只在数据里：这一条钉的是数据，不是代码常量。"""
        with mock.patch.dict(llm.os.environ, {}, clear=True):
            tiers = load_config()["temperature_tiers"]
        self.assertAlmostEqual(0.5, tiers["factual"])
        self.assertEqual({"recalled_nonempty": True, "reply_budget_at_most": 90},
                         tiers["trigger"])

    def test_live_file_overrides_factory(self):
        live = self.write(self.tmp / "live" / "mind" / "llm.json",
                          json.dumps(conf(model="live-model", base_url="http://live.test/v1")))
        with mock.patch.dict(llm.os.environ, {"QQAGENT_HOME": str(self.tmp / "live")}, clear=True):
            self.assertEqual(load_config()["model"], "live-model")
        self.assertTrue(live.is_file())

    def test_swapped_data_file_changes_url_and_model(self):
        self.write(self.tmp / "live" / "mind" / "llm.json",
                   json.dumps(conf(provider="other", model="other-model",
                                   base_url="http://other.test/api")))
        keep = FakeTransport(ok("换了我"))
        with mock.patch.dict(llm.os.environ, {"QQAGENT_HOME": str(self.tmp / "live")}, clear=True):
            text = complete(MESSAGES, env={"QQAGENT_LLM_KEY": "sk-1"}, transport=keep)
        self.assertEqual(text, "换了我")
        self.assertEqual(keep.calls[0]["url"], "http://other.test/api/chat/completions")
        self.assertEqual(keep.calls[0]["payload"]["model"], "other-model")

    def test_bad_json_is_config_error(self):
        broken = self.write(self.tmp / "llm.json", "{ 这不是 json")
        with self.assertRaises(LLMError) as caught:
            load_config(broken)
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn(str(broken), caught.exception.message)

    def test_missing_field_is_config_error(self):
        data = conf()
        data.pop("model")
        missing = self.write(self.tmp / "llm.json", json.dumps(data))
        with self.assertRaises(LLMError) as caught:
            load_config(missing)
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn("model", caught.exception.message)

    def test_empty_base_url_is_config_error(self):
        empty = self.write(self.tmp / "llm.json", json.dumps(conf(base_url="   ")))
        with self.assertRaises(LLMError) as caught:
            load_config(empty)
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn("base_url", caught.exception.message)

    def test_no_config_anywhere_is_config_error(self):
        with mock.patch.object(llm.paths, "FACTORY", self.tmp / "no-factory"):
            with mock.patch.dict(llm.os.environ, {"QQAGENT_HOME": str(self.tmp / "nothing")},
                                 clear=True):
                with self.assertRaises(LLMError) as caught:
                    load_config()
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn("都不存在", caught.exception.message)

    def test_config_injection_is_validated_too(self):
        with self.assertRaises(LLMConfigError):
            complete(MESSAGES, config=conf(max_tokens=0),
                     env={"QQAGENT_LLM_KEY": "k"}, transport=FakeTransport())
