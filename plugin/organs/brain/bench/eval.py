"""评测最小集：20 题 + 6 锚题，闭卷两臂。

- 20 题：同一批题跑两臂（零材料闭卷 / 给记忆），只报净增益；「给记忆答对」不算通过。
- 6 锚题：3 正锚（quote 必须是某条记忆原文的子串，给记忆必须命中）
        + 3 负锚（necessary_conclusion 不得是任何一条记忆的子串，给记忆必须不命中）。
- 人抽检：cli 的 eval 子命令把 26 题逐题打印（题面 / 答案 / 两臂命中），挑 5 题复现即可。
"""
from __future__ import annotations

from typing import List, Tuple

from organs.brain.tests.helpers import fresh_store, ingest

# (题面, 答案必须是这条记忆原文的逐字子串)
QUESTIONS: List[Tuple[str, str]] = [
    ("我妈怎么了", "住院"),
    ("最近喜欢吃什么", "小鱼干"),
    ("那个 bug 出在哪", "登录页"),
    ("那天难过吗", "难过"),
    ("谁说过医院好吵", "医院好吵"),
    ("房租什么时候交", "房租"),
    ("怕打雷吗", "打雷"),
    ("养的猫叫什么", "年糕"),
    ("上次聚会去了哪", "海边"),
    ("在学做什么", "蛋糕"),
    ("手机怎么了", "掉水"),
    ("换了什么", "新工作"),
    ("最近在追什么", "悬疑剧"),
    ("钥匙忘在哪", "公司"),
    ("周末想做什么", "电影"),
    ("嗓子怎么了", "感冒"),
    ("那家店怎么了", "关门"),
    ("买了什么新东西", "耳机"),
    ("他搬去哪了", "上海"),
    ("约了什么时候", "下周三"),
]

# 每条事实一个不同的人，避免 10 分钟同人合并把前面的句子吞掉
FACTS: List[Tuple[str, str]] = [
    ("3425635473", "我妈住院了"),
    ("234567890", "我最近喜欢吃小鱼干"),
    ("345678900", "那个 bug 在登录页"),
    ("456789012", "我今天有点难过"),
    ("567890123", "医院好吵"),
    ("678901234", "明天要交房租"),
    ("789012345", "我最怕打雷"),
    ("890123456", "我养了一只叫年糕的猫"),
    ("901234567", "上次聚会去了海边"),
    ("901234568", "我在学做蛋糕"),
    ("901234569", "手机掉水里了"),
    ("901234570", "我换了新工作"),
    ("901234571", "最近在追一部悬疑剧"),
    ("901234572", "我把钥匙忘在公司"),
    ("901234573", "周末想去看电影"),
    ("901234574", "我感冒了嗓子疼"),
    ("901234575", "那家店关门了"),
    ("901234576", "我买了个新耳机"),
    ("901234577", "他搬去上海了"),
    ("901234578", "我们约了下周三"),
]

# 锚题：(题面, 答案, 期望) —— 正锚=给记忆必须命中；负锚=给记忆必须不命中（防伪线）
ANCHORS: List[Tuple[str, str, str]] = [
    ("我妈住院了", "住院", "positive"),
    ("医院好吵", "医院好吵", "positive"),
    ("下周三", "下周三", "positive"),
    ("她妈妈病得很重", "病", "negative"),
    ("他喜欢猫", "猫", "negative"),
    ("上次说的是搬家的事", "搬家", "negative"),
]


def _hit(store, question: str, answer: str, now_ts: str) -> bool:
    result = store.recall(question, now_ts=now_ts)
    return any(answer in item.get("text", "") for item in result.get("items", []))


def run_eval(now_ts: str = "2026-11-03T12:00:00+00:00") -> dict:
    closed_store, closed_tmp = fresh_store()
    try:
        closed_20 = sum(1 for q, a in QUESTIONS if _hit(closed_store, q, a, now_ts))
    finally:
        closed_tmp.cleanup()

    open_store, open_tmp = fresh_store()
    try:
        for index, (who, text) in enumerate(FACTS):
            ingest(open_store, who, text, "2026-11-02T10:%02d:00+00:00" % index)
        open_store.today_entries(now_ts)   # 触发换日：全部事实进长期档，像真的"过去说过"
        open_20 = sum(1 for q, a in QUESTIONS if _hit(open_store, q, a, now_ts))
        anchor_rows = []
        for q, a, expect in ANCHORS:
            hit = _hit(open_store, q, a, now_ts)
            ok = hit if expect == "positive" else (not hit)
            anchor_rows.append({"question": q, "answer": a, "expect": expect,
                                "hit": hit, "ok": ok})
    finally:
        open_tmp.cleanup()

    return {
        "total": len(QUESTIONS),
        "closed_hits": closed_20,
        "open_hits": open_20,
        "net_gain": open_20 - closed_20,
        "verdict": "通过（净增益 > 0）" if open_20 - closed_20 > 0
                   else "不通过（只有给记忆答对不算数）",
        "anchors": anchor_rows,
        "anchors_ok": all(row["ok"] for row in anchor_rows),
    }


def render_for_human(now_ts: str = "2026-11-03T12:00:00+00:00") -> str:
    """把 26 题逐题打印成人能抽检的清单。"""
    closed_store, closed_tmp = fresh_store()
    open_store, open_tmp = fresh_store()
    try:
        for index, (who, text) in enumerate(FACTS):
            ingest(open_store, who, text, "2026-11-02T10:%02d:00+00:00" % index)
        open_store.today_entries(now_ts)   # 触发换日：全部事实进长期档
        lines = ["评测题集（20 题 + 6 锚题），请抽检至少 5 题：", ""]
        number = 1
        for q, a in QUESTIONS:
            c = _hit(closed_store, q, a, now_ts)
            o = _hit(open_store, q, a, now_ts)
            lines.append("%02d. %s  →  答案须含「%s」  闭卷:%s 给记忆:%s"
                         % (number, q, a, "中" if c else "空", "中" if o else "空"))
            number += 1
        lines.append("")
        lines.append("—— 锚题 ——")
        for q, a, expect in ANCHORS:
            o = _hit(open_store, q, a, now_ts)
            ok = o if expect == "positive" else (not o)
            lines.append("%02d. [%s] %s  →  答案「%s」  给记忆:%s  %s"
                         % (number, "正锚" if expect == "positive" else "负锚",
                            q, a, "中" if o else "空", "过" if ok else "FAIL"))
            number += 1
        return "\n".join(lines)
    finally:
        closed_tmp.cleanup()
        open_tmp.cleanup()


if __name__ == "__main__":
    print(render_for_human())
