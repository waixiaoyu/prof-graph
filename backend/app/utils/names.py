"""名称归一化（T2）：小写 + 去音标 + 去空格/连字符/句点。

M2-T4 扩展：中文人名拼音归一（RD-M2-12）——"张三" → "zhangsan"，
与 "Zhang San" 的 normalize_name 结果同域，中英文姓名可在同一
name_normalized 键上相遇。
"""
from __future__ import annotations

import re
import unicodedata

from pypinyin import lazy_pinyin

_STRIP = str.maketrans("", "", " -._'")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def normalize_name(name: str) -> str:
    """归一化机构名/人名，用于唯一键与检索。

    >>> normalize_name("Tsinghua University")
    'tsinghuauniversity'
    >>> normalize_name("Wei  Li")
    'weili'
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return ascii_only.lower().translate(_STRIP)


def normalize_cn(name: str) -> str:
    """中文名 → 拼音归一（pypinyin 常用音拼接小写）；不含中文返回空串。

    多音字取词典默认音（如"单"姓按默认音处理，不崩溃）。
    >>> normalize_cn("张三")
    'zhangsan'
    """
    if not _CJK_RE.search(name):
        return ""
    joined = "".join(lazy_pinyin(name)).lower()
    return "".join(ch for ch in joined if ch.isalnum())


def normalize_person_name(name: str) -> str:
    """人名统一归一：含中文走拼音，否则走 normalize_name。

    >>> normalize_person_name("张三") == normalize_name("Zhang San")
    True
    """
    return normalize_cn(name) or normalize_name(name)


def levenshtein(a: str, b: str) -> int:
    """编辑距离（T11 模糊候选与姓名得分用，小字符串全量 DP 即可）。"""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def swap_name_order(name: str) -> str:
    """姓名序颠倒变体：'Zhang Wei' → 'Wei Zhang'。"""
    parts = name.split()
    return " ".join(reversed(parts)) if len(parts) >= 2 else name
