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
