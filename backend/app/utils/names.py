"""名称归一化（T2）：小写 + 去音标 + 去空格/连字符/句点。"""
from __future__ import annotations

import unicodedata

_STRIP = str.maketrans("", "", " -._'")


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
