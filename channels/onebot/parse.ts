// OneBot v11 消息事件的最小解析：谁、在哪、说了什么、有没有图。
// 纯函数，不抛异常：任何输入都返回可判定的结果（ParsedEvent 或 ParseFailure），调用方不需要 try/catch。

type Rec = Record<string, unknown>;

export type ImageSummary = {
  file?: string;
  url?: string;
  summary?: string;
};

export type Who = {
  id: number;
  nickname: string;
  card?: string;
  role?: string;
};

export type Where =
  | { scene: "group"; groupId: number }
  | { scene: "private"; userId: number };

export type ParsedEvent = {
  ok: true;
  messageType: "group" | "private";
  subType: string;
  messageId: number | null;
  time: number | null;
  selfId: number | null;
  who: Who;
  where: Where;
  text: string;
  hasImage: boolean;
  images: ImageSummary[];
  // 下面几个字段是给合批用的结论，不在合批里重读消息：同一件事只判一次。
  // atTargets 留着原始 qq（"all" 也在里面），排障时要能一眼看出这条 @ 的是谁。
  atTargets: string[];
  mentionedSelf: boolean;
  mentionedAll: boolean;
  hasTextSegment: boolean;
  isBareImage: boolean;
  isSticker: boolean;
};

export type ParseFailure = {
  ok: false;
  reason: string;
};

export type ParseResult = ParsedEvent | ParseFailure;

const fail = (reason: string): ParseFailure => ({ ok: false, reason });

const isRecord = (value: unknown): value is Rec =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const asText = (value: unknown): string | undefined => {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return undefined;
};

const asId = (value: unknown): number | undefined => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && /^-?\d+$/.test(value)) return Number(value);
  return undefined;
};

const describe = (value: unknown): string => {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
};

// 非文本段也要留一句人看得懂的话，不能让它们变成空字符串。
const segmentToText = (segment: unknown): string => {
  if (!isRecord(segment)) return "[未知段]";
  const data = isRecord(segment.data) ? segment.data : {};
  switch (segment.type) {
    case "text":
      return asText(data.text) ?? "";
    case "at": {
      const qq = asText(data.qq) ?? "?";
      return qq === "all" ? "@全体成员" : `@${qq}`;
    }
    case "reply":
      return `[回复:${asText(data.id) ?? "?"}]`;
    case "image":
      return "[图片]";
    case "face":
      return `[表情:${asText(data.id) ?? "?"}]`;
    case "mface":
      return asText(data.summary) ?? "[动画表情]";
    case "record":
      return "[语音]";
    case "video":
      return "[视频]";
    case "file":
      return `[文件:${asText(data.name) ?? "?"}]`;
    case "forward":
      return "[合并转发]";
    case "json":
    case "xml":
      return "[卡片]";
    default:
      return typeof segment.type === "string" ? `[${segment.type}]` : "[未知段]";
  }
};

const imageSummary = (segment: Rec): ImageSummary => {
  const data = isRecord(segment.data) ? segment.data : {};
  const summary: ImageSummary = {};
  const file = asText(data.file);
  const url = asText(data.url);
  const text = asText(data.summary);
  if (file !== undefined) summary.file = file;
  if (url !== undefined) summary.url = url;
  if (text !== undefined) summary.summary = text;
  return summary;
};

export const parseEvent = (raw: unknown): ParseResult => {
  let value: unknown = raw;

  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw);
    } catch (error) {
      return fail(`不是合法 JSON：${(error as Error).message}`);
    }
  }

  if (!isRecord(value)) {
    return fail(`事件必须是 JSON 对象，实际是 ${describe(value)}`);
  }

  const postType = asText(value.post_type);
  if (postType === undefined) {
    return fail("缺少 post_type 字符串字段，不像 OneBot v11 事件");
  }
  if (postType !== "message") {
    return fail(`暂不解析 post_type=${postType} 的事件（M0 只处理消息事件）`);
  }

  const messageType = asText(value.message_type);
  if (messageType !== "group" && messageType !== "private") {
    return fail(`不支持的 message_type=${messageType ?? "（缺失）"}`);
  }

  const sender = isRecord(value.sender) ? value.sender : {};
  const senderId = asId(sender.user_id) ?? asId(value.user_id);
  if (senderId === undefined) {
    return fail("缺少发送者 id（sender.user_id 或 user_id），无法确定是谁");
  }

  let where: Where;
  if (messageType === "group") {
    const groupId = asId(value.group_id);
    if (groupId === undefined) {
      return fail("群消息缺少 group_id，无法确定在哪");
    }
    where = { scene: "group", groupId };
  } else {
    where = { scene: "private", userId: senderId };
  }

  if (!Array.isArray(value.message)) {
    return fail(`message 应为消息段数组，实际是 ${describe(value.message)}（M0 不解析 CQ 码字符串）`);
  }

  const who: Who = { id: senderId, nickname: asText(sender.nickname) ?? "" };
  const card = asText(sender.card);
  const role = asText(sender.role);
  if (card) who.card = card;
  if (role) who.role = role;

  const texts: string[] = [];
  const images: ImageSummary[] = [];
  const atTargets: string[] = [];
  let hasTextSegment = false;
  let stickerSegments = 0;
  for (const segment of value.message) {
    texts.push(segmentToText(segment));
    if (!isRecord(segment)) continue;
    const data = isRecord(segment.data) ? segment.data : {};
    if (segment.type === "image") images.push(imageSummary(segment));
    // 空白文本段不算"有话说"：她收到一个只有空格的消息，那仍然是一张裸图。
    if (segment.type === "text" && (asText(data.text) ?? "").trim() !== "") hasTextSegment = true;
    if (segment.type === "at") {
      const qq = asText(data.qq);
      if (qq !== undefined) atTargets.push(qq);
    }
    if (segment.type === "face" || segment.type === "mface") stickerSegments += 1;
  }

  const selfId = asId(value.self_id) ?? null;
  // 没有 self_id 就答不出"@ 我"：宁可说没有，也不能猜一个 id 当成自己。
  const mentionedSelf = selfId !== null && atTargets.some((qq) => qq === String(selfId));
  const mentionedAll = atTargets.includes("all");
  // 裸图 = 有图且一句话都没有；纯表情 = 只有 face/mface，别的段一个都不许有。
  const isBareImage = images.length > 0 && !hasTextSegment;
  const isSticker =
    stickerSegments > 0 && stickerSegments === value.message.length && images.length === 0 && !hasTextSegment;

  return {
    ok: true,
    messageType,
    subType: asText(value.sub_type) ?? "",
    messageId: asId(value.message_id) ?? null,
    time: asId(value.time) ?? null,
    selfId,
    who,
    where,
    text: texts.join(""),
    hasImage: images.length > 0,
    images,
    atTargets,
    mentionedSelf,
    mentionedAll,
    hasTextSegment,
    isBareImage,
    isSticker,
  };
};
