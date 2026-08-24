"""T2：名称归一化单测。"""
from app.utils.names import normalize_name


def test_basic_lower_strip() -> None:
    assert normalize_name("Tsinghua University") == "tsinghuauniversity"
    assert normalize_name("Wei  Li") == "weili"


def test_punctuation_removed() -> None:
    assert normalize_name("Mass.-Inst. of Tech") == "massinstoftech"
    assert normalize_name("U.C. Berkeley") == "ucberkeley"


def test_accent_folded_to_ascii() -> None:
    # 变音符号折叠：André -> Andre
    assert normalize_name("André Brown") == "andrebrown"
