import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseEvent } from "./parse.ts";

const loadFixture = (name: string): unknown =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));

// 解析成功时把 ParsedEvent 交出来，失败直接让断言报出原因，避免每个用例都重复宽化判断。
const expectParsed = (raw: unknown) => {
  const result = parseEvent(raw);
  assert.equal(result.ok, true, result.ok ? "" : `意外失败：${result.reason}`);
  if (!result.ok) throw new Error("unreachable");
  return result;
};

test("群文本：who/where/text/hasImage", () => {
  const event = expectParsed(loadFixture("event.group.text.json"));

  assert.deepEqual(event.who, {
    id: 3000000001,
    nickname: "小明",
    card: "小明",
    role: "member",
  });
  assert.deepEqual(event.where, { scene: "group", groupId: 700000001 });
  assert.equal(event.text, "在吗");
  assert.equal(event.hasImage, false);
  assert.deepEqual(event.images, []);
  assert.equal(event.messageId, 2000000001);
  assert.equal(event.messageType, "group");
  assert.equal(event.subType, "normal");
  assert.equal(event.selfId, 1000000001);
  assert.equal(event.time, 1761523200);
});

test("私聊：who/where/text/hasImage，群字段不串味", () => {
  const event = expectParsed(loadFixture("event.private.text.json"));

  assert.deepEqual(event.who, { id: 3000000003, nickname: "路人甲" });
  assert.deepEqual(event.where, { scene: "private", userId: 3000000003 });
  assert.equal("groupId" in event.where, false);
  assert.equal(event.text, "私聊测试[卡片]");
  assert.equal(event.hasImage, false);
  assert.deepEqual(event.images, []);
  assert.equal(event.messageType, "private");
  assert.equal(event.subType, "friend");
});

test("带图群消息：图片段汇总 + 非文本段有可读占位", () => {
  const event = expectParsed(loadFixture("event.group.image.json"));

  assert.deepEqual(event.who, {
    id: 3000000002,
    nickname: "阿花",
    card: "花姐",
    role: "admin",
  });
  assert.deepEqual(event.where, { scene: "group", groupId: 700000001 });
  assert.equal(event.text, "（引用）@1000000001 帮我看下这段[图片][表情:微笑][动画表情][卡片]");
  assert.equal(event.hasImage, true);
  assert.deepEqual(event.images, [
    { file: "ab12cd34.image", url: "https://example.invalid/img.png", summary: "[图片]" },
  ]);
});

test("畸形输入：一律返回 ParseFailure，不抛异常", () => {
  const cases: Array<{ name: string; raw: unknown; expect: RegExp }> = [
    {
      name: "非法 JSON 字符串",
      raw: '{"post_type":"message","message":[',
      expect: /不是合法 JSON/,
    },
    {
      name: "合法 JSON 但不是事件对象",
      raw: "[1,2,3]",
      expect: /事件必须是 JSON 对象，实际是 array/,
    },
    {
      name: "post_type 存在但 message 类型错",
      raw: {
        post_type: "message",
        message_type: "group",
        group_id: 700000001,
        user_id: 3000000001,
        message: "在吗",
        sender: { user_id: 3000000001, nickname: "小明" },
      },
      expect: /message 应为消息段数组，实际是 string/,
    },
    {
      name: "空对象",
      raw: {},
      expect: /缺少 post_type/,
    },
  ];

  for (const testCase of cases) {
    const result = parseEvent(testCase.raw);
    assert.equal(result.ok, false, `${testCase.name} 应当解析失败`);
    if (result.ok) throw new Error("unreachable");
    assert.match(result.reason, testCase.expect, testCase.name);
  }
});

test("畸形输入的补充边界：字符串输入被当成 JSON，null 与数字也不崩", () => {
  assert.equal(parseEvent('"just a string"').ok, false);
  assert.equal(parseEvent(null).ok, false);
  assert.equal(parseEvent(42).ok, false);
  assert.equal(parseEvent(undefined).ok, false);

  const notice = parseEvent({ post_type: "notice", notice_type: "group_upload", group_id: 700000001 });
  assert.equal(notice.ok, false);
  if (!notice.ok) assert.match(notice.reason, /暂不解析 post_type=notice/);
});

test("QQ原生表情：私聊和群聊保留名称、连发顺序与表情属性", () => {
  for (const scene of ["private", "group"]) {
    const fixture = loadFixture(`event.${scene}.text.json`) as Record<string, unknown>;
    const event = expectParsed({ ...fixture, message: [
      { type: "text", data: { text: "就是这个" } },
      ...Array.from({ length: 3 }, () => ({ type: "face", data: { id: "76", raw: { faceIndex: 76, faceText: null, faceType: 1 } } })),
    ] });
    assert.equal(event.text, "就是这个[表情:赞][表情:赞][表情:赞]");
    assert.equal(event.isSticker, false);
    const pure = expectParsed({ ...fixture, message: [{ type: "face", data: { id: "76" } }] });
    assert.equal(pure.isSticker, true);
    assert.equal(pure.text, "[表情:赞]");
    assert.equal(event.hasImage, false);
  }
});

test("QQ表情使用平台名称，未知或畸形名称不猜", () => {
  for (const [data, expected] of [
    [{ id: "9998", raw: { faceText: "/新表情" } }, "[表情:新表情]"],
    [{ id: "76", raw: { faceText: "/超级赞" } }, "[表情:超级赞]"],
    [{ id: 76, raw: { faceText: 123 } }, "[表情:赞]"],
    [{ id: "9999" }, "[表情:9999]"],
    [{ id: "9999", raw: { faceText: " / " } }, "[表情:9999]"],
    [{}, "[表情:?]"],
    [{ id: "319" }, "[表情:319]"], // 参考仓库把比心误记成生气，未核对数据不沿用。
  ] as Array<[Record<string, unknown>, string]>) {
    const event = expectParsed({ ...(loadFixture("event.private.text.json") as object), message: [{ type: "face", data }] });
    assert.equal(event.text, expected);
  }
});
