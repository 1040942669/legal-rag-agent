"""Selection contracts over fictional references, not a legal/NLU benchmark."""
from __future__ import annotations

import hashlib
import json

import pytest

from legal_rag.legal_references import ReferenceAnalysis, parse_legal_references


def selected(query, **kwargs):
    return [(item.law_title, item.article_number)
            for item in parse_legal_references(query, **kwargs).requirements]


@pytest.mark.parametrize("background", [
    "《合成甲法》第十条不用解释",
    "《合成甲法》第十条我不需要解释",
    "对方声称《合成甲法》第十条适用",
    "有人宣称《合成甲法》第十条适用",
    "同事谈及《合成甲法》第十条",
])
def test_unselected_background_does_not_become_a_hard_requirement(background):
    query = background + "，我现在只要求解释《合成乙法》第五条。"
    result = parse_legal_references(query)
    assert selected(query) == [("合成乙法", "第五条")]
    assert "合成甲法" not in result.required_law_titles
    assert not result.unresolved
    assert any(item.law_title == "合成甲法" and item.disposition in {"mentioned", "excluded"}
               for item in result.mentions)


@pytest.mark.parametrize("query,expected", [
    ("解释《合成甲法》第十条和《合成乙法》第五条。", [("合成甲法", "第十条"), ("合成乙法", "第五条")]),
    ("《合成甲法》第10条", [("合成甲法", "第十条")]),
    ("《合成甲法》第十条是什么？", [("合成甲法", "第十条")]),
    ("核查《合成甲法》第十条不适用吗？", [("合成甲法", "第十条")]),
    ("请核查对方引用《合成甲法》第十条。", [("合成甲法", "第十条")]),
    ("解释《合成甲法》第十条，不要解释《合成乙法》第五条。", [("合成甲法", "第十条")]),
    ("《合成甲法》第10条和《合成乙法》第五条是什么？", [("合成甲法", "第十条"), ("合成乙法", "第五条")]),
    ("《合成甲法》第十条、第二十条和第30条是什么？", [("合成甲法", "第十条"), ("合成甲法", "第二十条"), ("合成甲法", "第三十条")]),
    ("《合成甲法》第一条规定什么？", [("合成甲法", "第一条")]),
    ("《合成甲法》第一条规定了什么？", [("合成甲法", "第一条")]),
])
def test_positive_selection_and_negative_proposition_remain_usable(query, expected):
    result = parse_legal_references(query)
    assert selected(query) == expected
    assert not result.unresolved
    assert result.to_dict()["rules_version"] == "legal-reference-v3"


@pytest.mark.parametrize("query", [
    "他要求解释《合成甲法》第十条。",
    "不是不要解释《合成甲法》第十条。",
    "此前依据《合成甲法》第十条；请解释该规定现行内容。",
    "《合成甲法》和《合成乙法》的第十条、第五条分别是什么？",
])
def test_complex_selection_is_explicitly_unknown_not_an_invented_requirement(query):
    result = parse_legal_references(query)
    assert not result.requirements
    assert result.unresolved


@pytest.mark.parametrize("index", range(8))
def test_selection_is_invariant_under_fictional_identity_substitution(index):
    query = f"对方谈及《星云{index}规则》第{index+1}条，只解释《恒星{index}规则》第{index+2}条。"
    expected_numbers = ("第二条", "第三条", "第四条", "第五条", "第六条", "第七条", "第八条", "第九条")
    assert selected(query) == [(f"恒星{index}规则", expected_numbers[index])]


def test_v2_history_round_trips_under_its_original_rules_without_new_authority():
    query = "《合成甲法》第十条不用解释，只解释《合成乙法》第五条。"
    old = parse_legal_references(query, rules_version="legal-reference-v2")
    assert [(item.law_title, item.article_number) for item in old.requirements] == [
        ("合成甲法", "第十条"), ("合成乙法", "第五条")]
    restored = ReferenceAnalysis.from_dict(old.to_dict())
    assert restored == old
    assert restored.rules_version == "legal-reference-v2"
    assert restored.fingerprint == old.fingerprint
    modern = parse_legal_references(query)
    assert modern.rules_version == "legal-reference-v3"
    assert modern.requirements != old.requirements


def test_v3_history_rejects_tampered_selection_even_with_recomputed_hash():
    result = parse_legal_references("解释《合成甲法》第十条。")
    artifact = result.to_dict()
    artifact["requirements"][0]["article_number"] = "第十一条"
    artifact["fingerprint"] = hashlib.sha256(json.dumps(
        {key: value for key, value in artifact.items() if key != "fingerprint"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="original input"):
        ReferenceAnalysis.from_dict(artifact)


def test_unknown_rule_versions_do_not_silently_dispatch():
    with pytest.raises(ValueError, match="rules"):
        parse_legal_references("《合成甲法》第十条", rules_version="unknown")


@pytest.mark.parametrize("query", [
    "对方声称请解释《合成甲法》第十条。",
    "他要求解释《合成甲法》第十条。",
    "备注《查询解释规则》第十条。",
])
def test_an_operation_in_reported_prose_or_a_title_is_not_positive_user_selection(query):
    result = parse_legal_references(query)
    assert not result.requirements
    assert not result.required_law_titles


@pytest.mark.parametrize("query,expected", [
    ("《合成甲法》的第十条和《合成乙法》的第五条是什么？", [("合成甲法", "第十条"), ("合成乙法", "第五条")]),
    ("合成甲法中的第十条是什么？", [("合成甲法", "第十条")]),
    ("《合成机关关于《合成甲法》第十条的规定》第二条是什么？", [("合成机关关于《合成甲法》第十条的规定", "第二条")]),
])
def test_reference_expression_operators_do_not_change_pair_or_nested_title_identity(query, expected):
    result = parse_legal_references(query, known_law_titles=("合成甲法", "合成乙法"))
    assert [(item.law_title, item.article_number) for item in result.requirements] == expected
    assert not result.unresolved
    assert ReferenceAnalysis.from_dict(result.to_dict()) == result


def test_shared_article_for_disjoined_laws_does_not_guess_ownership():
    result = parse_legal_references("《合成甲法》或《合成乙法》第十条是什么？")
    assert not result.requirements
    assert result.unresolved


@pytest.mark.parametrize("query", ["解释《合成甲法》第十十条。", "解释《合成甲法》实施细则第十条。"])
def test_selection_does_not_repair_invalid_labels_or_unknown_title_extensions(query):
    result = parse_legal_references(query)
    assert not result.requirements
    assert result.unresolved


@pytest.mark.parametrize("version", ["legal-reference-v2", "legal-reference-v3"])
def test_version_identity_is_part_of_the_strict_fingerprint(version):
    result = parse_legal_references("《合成甲法》第十条", rules_version=version)
    assert result.rules_version == version
    assert ReferenceAnalysis.from_dict(result.to_dict()) == result
    changed = result.to_dict()
    changed["rules_version"] = "legal-reference-v3" if version.endswith("v2") else "legal-reference-v2"
    with pytest.raises(ValueError, match="fingerprint"):
        ReferenceAnalysis.from_dict(changed)


@pytest.mark.parametrize("separator", ["，以及", ",以及", "，", ",", "、"])
def test_reference_list_retains_the_selection_scope_before_its_separator(separator):
    expression = "《合成甲法》第十条" + separator + "《合成乙法》第五条。"
    assert selected("对方谈及" + expression + "只解释《合成丙法》第七条。") == [("合成丙法", "第七条")]
    assert selected("解释" + expression) == [("合成甲法", "第十条"), ("合成乙法", "第五条")]


@pytest.mark.parametrize("continuation", ["不要解释第二十条", "不要第二十条", "对方要求解释第二十条"])
def test_reusing_law_identity_does_not_reuse_an_earlier_article_selection(continuation):
    analysis = parse_legal_references("解释《合成甲法》第十条，" + continuation + "。")
    assert [(item.law_title, item.article_number) for item in analysis.requirements] == [("合成甲法", "第十条")]
    assert any(item.article_number == "第二十条" and item.disposition != "requested"
               for item in analysis.mentions)


@pytest.mark.parametrize("label", ["第十条之二之三", "第10条之2之3"])
def test_repeated_suffix_is_unknown_not_a_truncated_valid_article(label):
    analysis = parse_legal_references("解释《合成甲法》" + label + "。")
    assert not analysis.requirements
    assert analysis.unresolved


@pytest.mark.parametrize("continuation", ["但解释第二十条", "解释第二十条"])
def test_an_earlier_exclusion_does_not_silently_exclude_a_later_selection(continuation):
    analysis = parse_legal_references("不要解释《合成甲法》第十条，" + continuation + "。")
    assert not analysis.requirements
    assert any(item.article_number == "第二十条" for item in analysis.unresolved)
    assert not any(item.article_number == "第二十条" for item in analysis.excluded)
