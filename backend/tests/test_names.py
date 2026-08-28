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


# ---------- M2-T4 中文拼音归一补充 ----------


def test_normalize_cn_edges() -> None:
    from app.utils.names import normalize_cn

    assert normalize_cn("张三") == "zhangsan"
    assert normalize_cn("段海鑫") == "duanhaixin"
    assert normalize_cn("") == ""           # 空串
    assert normalize_cn("Wei Zhang") == ""  # 纯拉丁 → 空串（由 normalize_name 兜底）


def test_levenshtein_edges() -> None:
    from app.utils.names import levenshtein

    assert levenshtein("", "") == 0
    assert levenshtein("abc", "") == 3
    assert levenshtein("", "abcd") == 4  # 短串在 b 位也正确（内部交换）
    assert levenshtein("zhangsan", "zhangsan") == 0
    assert levenshtein("kitten", "sitting") == 3


def test_swap_name_order_single_token() -> None:
    from app.utils.names import swap_name_order

    assert swap_name_order("Zhang Wei") == "Wei Zhang"
    assert swap_name_order("Wei Zhang") == "Zhang Wei"  # 对合
    assert swap_name_order("Cher") == "Cher"  # 单 token 原样返回
    assert swap_name_order("") == ""
