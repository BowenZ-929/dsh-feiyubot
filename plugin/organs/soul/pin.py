"""M4：压缩前把约束 pin 在卡片顶部——本轮只做接口与拒收，不实现压缩。

参考实现压的是宿主选中的跨度，产物是「pin 住的约束 + 目录指针」，不是摘要。落到我们这里：
take_pin() 把每条红线压成「条目 id ｜ 禁令首句」，整体序列化 ≤1200 字节（UTF-8）。

三条拒收（压缩入口第一件事就是调 admit_compression）：
1. 跨度 <1200 字节的压缩请求 → COMPRESS_SPAN_TOO_SMALL（压完只会比原文长，没有意义）；
2. 压缩产物为空 → COMPRESS_EMPTY；
3. 产物首段必须逐字回填 pin（check_pin_roundtrip）→ 不符即 PIN_ROUNDTRIP_MISMATCH。

另外 pin 与产物本身都要过 M1 断言（redline.check_text）：软话词与重复条目不许从压缩这条缝里
进来——压缩后模型看到的是 pin + 产物，漏了这一道，M1 就白做了。
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import model, redline

PIN_LIMIT = 1200        # 整体序列化硬顶（UTF-8 字节）
SPAN_MIN = 1200         # 跨度低于这个数不压
SPAN_TOO_SMALL = "COMPRESS_SPAN_TOO_SMALL"
PRODUCT_EMPTY = "COMPRESS_EMPTY"
ROUNDTRIP_MISMATCH = "PIN_ROUNDTRIP_MISMATCH"
_PARAGRAPH = re.compile(r"\n\s*\n")


def first_sentence(rule: str) -> str:
    """禁令首句：到 。！？ 为止（含标点）；没有句末标点就是整条。"""
    for index, char in enumerate(rule):
        if char in "。！？":
            return rule[:index + 1]
    return rule


def take_pin(bounds: model.Bounds) -> str:
    """每条生效红线一行：「条目 id ｜ 禁令首句」；只取生效条目，撤下的线不 pin。"""
    lines = ["%s｜%s" % (item.id, first_sentence(item.rule))
             for item in bounds.items if item.active]
    text = "\n".join(lines)
    size = pin_bytes(text)
    if size > PIN_LIMIT:
        # 红线正文合计 ≤250 字，正常到不了这里；到不了才说明卡是坏的，不许悄悄截断
        raise ValueError("pin 序列化 %d 字节 > %d 字节硬顶：红线合计已经超预算，先改红线"
                         % (size, PIN_LIMIT))
    return text


def pin_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def first_paragraph(product: str) -> str:
    """产物的首段：以空行为界（pin 自己可以有多行）。"""
    return _PARAGRAPH.split(product, maxsplit=1)[0]


def check_pin_roundtrip(pin_text: str, product: str) -> List[model.Issue]:
    """产物首段必须逐字回填 pin——压缩不许把约束「复述」走样。"""
    head = first_paragraph(product)
    if head == pin_text:
        return []
    return [model.Issue(
        ROUNDTRIP_MISMATCH, model.ERROR,
        "压缩产物首段与 pin 不逐字一致：压缩还没接上，这一道先拦住「复述式回填」"
        "（pin %d 字节，产物首段 %d 字节）" % (pin_bytes(pin_text), pin_bytes(head)))]


def admit_compression(span_bytes: int, product: Optional[str], pin_text: str,
                      soft_words=redline.SOFT_WORDS) -> List[model.Issue]:
    """压缩入口的准入：三条拒收 + pin 与产物各自的 M1 断言。返回空列表才算过。"""
    issues: List[model.Issue] = []
    if span_bytes < SPAN_MIN:
        issues.append(model.Issue(
            SPAN_TOO_SMALL, model.ERROR,
            "压缩跨度 %d 字节 < %d 字节：这一段压完只会更长，拒压"
            % (span_bytes, SPAN_MIN)))
    if not isinstance(product, str) or not product.strip():
        issues.append(model.Issue(
            PRODUCT_EMPTY, model.ERROR,
            "压缩产物为空：拒收（空产物等于把这一段直接丢了）"))
    if isinstance(product, str) and product.strip():
        issues.extend(check_pin_roundtrip(pin_text, product))
        issues.extend(redline.check_text(product, soft_words=soft_words, where="压缩产物"))
    issues.extend(redline.check_text(pin_text, soft_words=soft_words, where="pin 清单"))
    return issues


def compress_once(pin_text: str, span: str, product: Optional[str] = None) -> List[model.Issue]:
    """压缩入口 hook：本轮只留函数与注释，不实现压缩逻辑。

    真接上压缩时这里是四步：① 取跨度字节数过 admit_compression 的拒收；② 调压缩器；
    ③ 把产物交回 admit_compression 复核（pin 回填 + M1 断言）；④ 返回产物。
    第 ② 步没有实现，所以准入通过之后这里直接抛 NotImplementedError——没有别的出路。
    """
    issues = admit_compression(pin_bytes(span) if isinstance(span, str) else int(span),
                              product, pin_text)
    if issues:
        return issues
    raise NotImplementedError("压缩逻辑未实现：本轮只做 pin 与拒收（M4）")
