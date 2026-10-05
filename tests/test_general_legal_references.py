"""Fictional reference relations, not evaluation gold or legal interpretation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from legal_rag.legal_references import (
    ReferenceAnalysis,
    canonical_article_number,
    canonical_law_title,
    parse_legal_references,
)


def pairs(query: str, **kwargs):
    return [(item.law_title, item.article_number)
            for item in parse_legal_references(query, **kwargs).requirements]


def test_explicit_multi_law_pairs_are_not_a_cartesian_product():
    query = "解释《合成甲法》第十条和《合成乙法》第五条。"
    analysis = parse_legal_references(query)
    assert pairs(query) == [("合成甲法", "第十条"), ("合成乙法", "第五条")]
    assert not analysis.unresolved
    assert all(query[item.span[0]:item.span[1]].startswith("《")
               for item in analysis.requirements)


def test_multiple_articles_attach_to_one_explicit_law():
    assert pairs("《合成甲法》第十条、第二十条和第30条是什么？") == [
        ("合成甲法", "第十条"), ("合成甲法", "第二十条"), ("合成甲法", "第三十条")]


def test_law_group_with_two_articles_remains_unresolved():
    result = parse_legal_references("《合成甲法》和《合成乙法》的第十条、第五条分别是什么？")
    assert result.requirements == ()
    assert len(result.unresolved) == 2
    assert all(item.reason == "ambiguous_law_group" for item in result.unresolved)


@pytest.mark.parametrize("query", [
    "解释《合成甲法》第十条，不是《合成乙法》第五条。",
    "解释《合成甲法》第十条，不要检索《合成乙法》第五条。",
    "解释《合成甲法》第十条，《合成乙法》第五条除外。",
])
def test_excluded_mentions_do_not_become_hard_requirements(query):
    result = parse_legal_references(query)
    assert pairs(query) == [("合成甲法", "第十条")]
    assert any(item.law_title == "合成乙法" for item in result.excluded)
    assert "合成乙法" not in result.required_law_titles


def test_reported_history_is_preserved_without_becoming_a_requirement():
    query = "对方曾引用《合成乙法》第五条，我现在只要求解释《合成甲法》第十条。"
    result = parse_legal_references(query)
    assert pairs(query) == [("合成甲法", "第十条")]
    assert any(item.law_title == "合成乙法" and item.disposition == "mentioned" for item in result.mentions)
    assert not result.unresolved
    historical = parse_legal_references(query, rules_version="legal-reference-v2")
    assert any(item.reason == "reported_reference" for item in historical.excluded)


def test_free_text_report_with_suffix_question_requires_explicit_selection_in_v3():
    query = "对方引用《合成甲法》第十条是否适用？"
    assert pairs(query) == []
    assert parse_legal_references(query).unresolved
    assert pairs(query, rules_version="legal-reference-v2") == [("合成甲法", "第十条")]
    assert pairs("请核查" + query) == [("合成甲法", "第十条")]


def test_known_corpus_titles_allow_unquoted_references_without_scene_rules():
    assert pairs("合成星云规则第2条是什么？", known_law_titles=("合成星云规则",)) == [
        ("合成星云规则", "第二条")]
    result = parse_legal_references("未知规则第2条是什么？")
    assert result.requirements == ()
    assert result.unresolved[0].reason == "unpaired_article"


def test_full_and_short_title_aliases_deduplicate_without_substring_aliasing():
    query = "《中华人民共和国合成甲法》第10条与《合成甲法》第十条是什么？"
    assert pairs(query) == [("合成甲法", "第十条")]
    assert canonical_law_title("《 合成甲法实施细则 》") != canonical_law_title("合成甲法")


@pytest.mark.parametrize("raw,expected", [
    ("第10条", "第十条"), ("第010条", "第十条"),
    ("第壹佰零贰条", "第一百零二条"), ("第1001条", "第一千零一条"),
    ("第 一 千 零 三 十 二 条", "第一千零三十二条"),
])
def test_article_numerals_are_generic_and_catalog_compatible(raw, expected):
    assert canonical_article_number(raw) == expected


def test_shorthand_article_lists_and_ranges_are_not_silently_dropped():
    assert pairs("《合成甲法》第十、十一条是什么？") == [
        ("合成甲法", "第十条"), ("合成甲法", "第十一条")]
    assert parse_legal_references("《合成甲法》第十至二十条是什么？").unresolved


def test_no_law_or_article_is_inferred_from_a_scene():
    result = parse_legal_references("买了雨伞不想要，能退回吗？")
    assert result.requirements == result.required_law_titles == result.unresolved == ()


def test_reference_identity_is_immutable_strict_and_round_trips():
    analysis = parse_legal_references("《合成甲法》第十条是什么？")
    assert ReferenceAnalysis.from_dict(analysis.to_dict()) == analysis
    with pytest.raises(FrozenInstanceError):
        analysis.query = "mutated"  # type: ignore[misc]
    forged = analysis.to_dict()
    forged["requirements"][0]["article_number"] = "第十一条"
    with pytest.raises(ValueError):
        ReferenceAnalysis.from_dict(forged)
    extra = {**analysis.to_dict(), "gold": "不可进入推理"}
    with pytest.raises(ValueError):
        ReferenceAnalysis.from_dict(extra)


def test_false_integer_spans_and_title_embedded_numbers_are_not_accepted():
    analysis = parse_legal_references("《合成甲法》第十条是什么？")
    forged = analysis.to_dict()
    forged["requirements"][0]["span"][0] = False
    with pytest.raises(ValueError):
        ReferenceAnalysis.from_dict(forged)
    assert parse_legal_references("《第十条规则》有什么规定？").unresolved == ()


@pytest.mark.parametrize("query", [
    "解释《合成甲法》第十条，不查询《合成乙法》第五条。",
    "解释《合成甲法》第十条，不用检索《合成乙法》第五条。",
    "他说过《合成乙法》第五条，我现在要求解释《合成甲法》第十条。",
])
def test_generic_exclusion_grammar_is_not_limited_to_one_fixture(query):
    assert pairs(query) == [("合成甲法", "第十条")]


@pytest.mark.parametrize("index", range(12))
def test_ownership_relations_are_invariant_under_fictional_title_substitution(index):
    first, second = f"合成星云{index}规则", f"合成恒星{index}规则"
    query = f"解释《{first}》第{index+1}条和《{second}》第{index+2}条。"
    assert pairs(query) == [(first, canonical_article_number(f"第{index+1}条")),
                            (second, canonical_article_number(f"第{index+2}条"))]


@pytest.mark.parametrize("separator", ["；", "。", "\n", "，"])
@pytest.mark.parametrize("intent", ["请判断这种引用是否正确", "请核查该法条是否适用", "能否解释上述条款"])
def test_reported_reference_then_anaphoric_request_is_explicitly_unknown_in_v3(separator, intent):
    query = "对方此前引用《合成甲法》第十条" + separator + intent + "？"
    analysis = parse_legal_references(query)
    assert not analysis.requirements
    assert any(item.reason == "unresolved_reference_anaphora" for item in analysis.unresolved)
    assert pairs(query, rules_version="legal-reference-v2") == [("合成甲法", "第十条")]


@pytest.mark.parametrize("query", [
    "请核查对方引用《合成甲法》第十条。",
    "我要求解释他援引《合成甲法》第十条。",
])
def test_request_before_reporting_verb_is_still_the_user_request(query):
    assert pairs(query) == [("合成甲法", "第十条")]


def test_cross_sentence_other_explicit_request_does_not_promote_history():
    query = "他此前引用《合成乙法》第五条。请解释《合成甲法》第十条。"
    assert pairs(query) == [("合成甲法", "第十条")]
    assert not parse_legal_references(query).unresolved


def test_v3_does_not_resolve_singular_or_plural_discourse_ownership():
    prefix = "对方引用《合成甲法》第十条和《合成乙法》第五条；"
    singular = parse_legal_references(prefix + "请核查这条引用是否正确？")
    assert singular.requirements == ()
    assert any(item.reason == "unresolved_reference_anaphora" for item in singular.unresolved)
    plural = prefix + "请核查这些引用是否正确？"
    assert not pairs(plural) and parse_legal_references(plural).unresolved
    historical_singular = parse_legal_references(prefix + "请核查这条引用是否正确？", rules_version="legal-reference-v2")
    assert any(item.reason == "ambiguous_reference_anaphora" for item in historical_singular.unresolved)
    assert pairs(plural, rules_version="legal-reference-v2") == [("合成甲法", "第十条"), ("合成乙法", "第五条")]


def test_reported_user_request_is_not_mistaken_for_actual_request():
    query = "他曾引用《合成甲法》第十条；对方说请核查这种引用是否正确。"
    analysis = parse_legal_references(query)
    assert not analysis.requirements
    assert any(item.reason == "unresolved_reference_anaphora" for item in analysis.unresolved)
    historical = parse_legal_references(query, rules_version="legal-reference-v2")
    assert any(item.reason == "reported_request_intent" for item in historical.unresolved)


@pytest.mark.parametrize("query", [
    "不是不要解释《合成甲法》第十条。",
    "并非无需查询《合成甲法》第十条。",
    "不要检索《合成甲法》第十条；请核查这个引用是否正确。",
])
def test_nested_or_conflicting_negation_is_unresolved_not_silent_exclusion(query):
    analysis = parse_legal_references(query)
    assert not analysis.requirements
    assert analysis.unresolved


@pytest.mark.parametrize("label", ["第十十条", "第百十条", "第一百百条", "第一万万条",
                                    "第十二三条", "第零十条", "第1百条", "第0条", "第100000000条"])
def test_malformed_numerals_are_never_repaired_into_valid_article_keys(label):
    with pytest.raises(ValueError):
        canonical_article_number(label)
    analysis = parse_legal_references("解释《合成甲法》" + label + "。")
    assert not analysis.requirements
    assert any(item.reason == "invalid_article_numeral" for item in analysis.unresolved)


def test_malformed_list_item_remains_unknown_and_explicitly_excluded_malformed_item_is_not_required():
    checked = parse_legal_references("解释《合成甲法》第十、十十条。")
    assert any(item.reason == "invalid_article_numeral" for item in checked.unresolved)
    excluded = parse_legal_references("解释《合成甲法》第十条，不要检索《合成乙法》第十十条。")
    assert not excluded.unresolved
    assert [(item.law_title, item.article_number) for item in excluded.requirements] == [("合成甲法", "第十条")]


@pytest.mark.parametrize("raw,canonical", [
    ("第10010条", "第一万零一十条"), ("第10011条", "第一万零一十一条"),
    ("第一万零一十条", "第一万零一十条"), ("第两千零二条", "第二千零二条"),
    ("第壹万零壹拾条", "第一万零一十条"), ("第〇一〇条", "第十条"),
    ("第一十条", "第十条"), ("第十万条", "第十万条"),
    ("第10条之2", "第十条之二"),
])
def test_valid_numeral_spellings_have_one_canonical_identity(raw, canonical):
    assert canonical_article_number(raw) == canonical


def test_positive_bounded_integer_canonicalization_is_idempotent_across_decimal_and_formal_chinese():
    formal = str.maketrans("一二三四五六七八九十百千", "壹贰叁肆伍陆柒捌玖拾佰仟")
    numbers = list(range(1, 300)) + [999, 1000, 1001, 1010, 9999, 10000, 10010, 10101, 99999999]
    for number in numbers:
        canonical = canonical_article_number(f"第{number}条")
        assert canonical_article_number(canonical) == canonical
        assert canonical_article_number(canonical.translate(formal)) == canonical


@pytest.mark.parametrize("separator", ["，以及", "，还有", "、"])
def test_reported_reference_collection_does_not_turn_its_later_member_into_a_demand(separator):
    prefix = "他此前引用《合成甲法》第十条" + separator + "《合成乙法》第五条。"
    assert pairs(prefix + "我现在只要求解释《合成丙法》第七条。") == [("合成丙法", "第七条")]
    plural = prefix + "请核查这些引用是否正确？"
    assert not pairs(plural) and parse_legal_references(plural).unresolved
    assert pairs(plural, rules_version="legal-reference-v2") == [("合成甲法", "第十条"), ("合成乙法", "第五条")]


def test_new_explicit_request_does_not_resolve_a_later_anaphoric_clause_in_v3():
    query = "对方引用《合成乙法》第五条，我现在要求解释《合成甲法》第十条；请核查该法条是否适用？"
    assert pairs(query) == [("合成甲法", "第十条")]
    assert parse_legal_references(query).unresolved
    assert not parse_legal_references(query, rules_version="legal-reference-v2").unresolved


@pytest.mark.parametrize("intent", ["无需核查这些引用", "不要解释该法条", "这种引用不必说明"])
def test_anaphoric_negated_request_does_not_revive_history(intent):
    query = "他此前引用《合成甲法》第十条；" + intent + "。"
    assert not parse_legal_references(query).requirements


@pytest.mark.parametrize("suffix", ["实施细则", "实施条例", "补充规则", "的实施细则"])
def test_unknown_unquoted_title_extension_never_borrows_the_known_base_law(suffix):
    analysis = parse_legal_references("合成甲法" + suffix + "第十条是什么？", known_law_titles=("合成甲法",))
    assert not analysis.requirements
    assert analysis.unresolved
    assert "合成甲法" not in analysis.required_law_titles


def test_complete_known_unquoted_related_title_remains_its_own_identity():
    assert pairs("合成甲法实施细则第十条是什么？", known_law_titles=("合成甲法", "合成甲法实施细则")) == [
        ("合成甲法实施细则", "第十条")]
    assert pairs("合成甲法的第十条是什么？", known_law_titles=("合成甲法",)) == [("合成甲法", "第十条")]


@pytest.mark.parametrize("label", ["第-1条", "第1.5条", "第十亿条", "第条", "第" + "9" * 80 + "条", "第十条之-1"])
def test_article_looking_labels_outside_supported_grammar_are_not_silently_lost(label):
    analysis = parse_legal_references("解释《合成甲法》" + label + "。")
    assert not analysis.requirements
    assert analysis.unresolved
    assert any(item.reason in {"unsupported_article_label", "invalid_article_numeral"} for item in analysis.unresolved)


@pytest.mark.parametrize("query", [
    "《合成甲法》第十条不是我要问的，只查询《合成乙法》第五条。",
    "我不想查《合成甲法》第十条，只查询《合成乙法》第五条。",
    "对方引用《合成甲法》第十条，能否讲解该条文？",
    "对方引用《合成甲法》第十条，请别核查这条引用。",
])
def test_unknown_reference_intent_is_unresolved_instead_of_invented_or_silently_excluded(query):
    analysis = parse_legal_references(query)
    assert ("合成甲法", "第十条") not in [(item.law_title, item.article_number) for item in analysis.requirements]
    # The old parser marked all opaque background as globally unknown. V3
    # retains it as an unselected mention unless selection/discourse is unknown.
    assert analysis.unresolved or any(item.law_title == "合成甲法" and item.disposition == "mentioned"
                                      for item in analysis.mentions)
    assert parse_legal_references(query, rules_version="legal-reference-v2").unresolved


@pytest.mark.parametrize("query", ["解释《合成甲法》第十条是否适用？", "核查《合成甲法》第十条不适用吗？"])
def test_explicit_verification_of_a_negative_proposition_is_still_requested(query):
    assert pairs(query) == [("合成甲法", "第十条")]


@pytest.mark.parametrize("prefix", ["新", "特别", "超级"])
def test_unknown_bare_title_prefix_cannot_borrow_a_known_embedded_title(prefix):
    analysis = parse_legal_references(prefix + "合成甲法第十条是什么？", known_law_titles=("合成甲法",))
    assert not analysis.requirements and analysis.unresolved


@pytest.mark.parametrize("prefix", ["解释", "根据", "查询中华人民共和国"])
def test_bounded_bare_reference_prefix_still_accepts_the_exact_known_title(prefix):
    assert pairs(prefix + "合成甲法第十条是什么？", known_law_titles=("合成甲法",)) == [("合成甲法", "第十条")]


def test_unquoted_particle_sequence_and_law_disjunction_do_not_guess_an_owner():
    assert pairs("合成甲法中的第十条是什么？", known_law_titles=("合成甲法",)) == [("合成甲法", "第十条")]
    analysis = parse_legal_references("《合成甲法》或《合成乙法》第十条是什么？")
    assert not analysis.requirements and analysis.unresolved


@pytest.mark.parametrize("label", ["第十条之1abc", "第十条之二β", "第十条之十亿"])
def test_suffix_numeral_cannot_be_truncated_to_a_valid_prefix(label):
    analysis = parse_legal_references("解释《合成甲法》" + label + "。")
    assert not analysis.requirements and analysis.unresolved


@pytest.mark.parametrize("separator", ["，再查询", "和查询"])
def test_last_explicit_request_operator_anchors_a_second_bare_title(separator):
    query = "解释合成甲法第十条" + separator + "合成乙法第五条。"
    assert pairs(query, known_law_titles=("合成甲法", "合成乙法")) == [("合成甲法", "第十条"), ("合成乙法", "第五条")]


def test_unknown_title_barriers_use_text_order_not_alias_length_order():
    query = "合成甲法实施细则被提过，查询《合成丙法》第三条，并查询长合成乙法实施细则第五条。"
    analysis = parse_legal_references(query, known_law_titles=("合成甲法", "长合成乙法", "合成丙法"))
    assert [(item.law_title, item.article_number) for item in analysis.requirements] == [("合成丙法", "第三条")]
    assert analysis.unresolved


@pytest.mark.parametrize("action", ["依据", "适用", "遵循", "参照", "解释"])
@pytest.mark.parametrize("separator", ["，", "。"])
def test_temporal_statement_cannot_be_promoted_by_a_later_current_request(action, separator):
    query = "此前" + action + "《合成甲法》第十条处理" + separator + "现在只解释《合成乙法》第五条。"
    analysis = parse_legal_references(query)
    assert pairs(query) == [("合成乙法", "第五条")]
    # Temporal prose without a selection operation is merely background, not
    # a failed requirement for the later explicitly selected law.
    assert analysis.unresolved or any(item.law_title == "合成甲法" and item.disposition == "mentioned"
                                      for item in analysis.mentions)
    assert parse_legal_references(query, rules_version="legal-reference-v2").unresolved
    assert "合成甲法" not in analysis.required_law_titles


@pytest.mark.parametrize("query", [
    "请核查此前依据《合成甲法》第十条处理。",
    "此前依据《合成甲法》第十条处理是否正确？",
])
def test_explicit_request_can_query_a_historical_reference_without_temporal_promotion(query):
    if query.startswith("请核查"):
        assert pairs(query) == [("合成甲法", "第十条")]
    else:
        # An arbitrary historical proposition is outside bounded reference
        # selection; repeat a positive operation rather than infer discourse.
        assert not pairs(query) and parse_legal_references(query).unresolved
    assert pairs(query, rules_version="legal-reference-v2") == [("合成甲法", "第十条")]


@pytest.mark.parametrize("qualifier", ["（草案）", "(旧版)", "【另一版本】", "[未知限定]"])
@pytest.mark.parametrize("title", ["合成甲法", "《合成甲法》"])
def test_opaque_title_qualifier_never_silently_selects_the_unqualified_identity(title, qualifier):
    analysis = parse_legal_references("解释" + title + qualifier + "第十条。", known_law_titles=("合成甲法",))
    assert not analysis.requirements and analysis.unresolved
    assert "合成甲法" not in analysis.required_law_titles


def test_fully_known_qualified_title_and_article_parentheses_have_explicit_distinct_identity():
    assert pairs("解释合成甲法（草案）第十条。", known_law_titles=("合成甲法", "合成甲法（草案）")) == [
        ("合成甲法（草案）", "第十条")]
    assert pairs("解释《合成甲法（草案）》第十条。") == [("合成甲法（草案）", "第十条")]
    assert pairs("解释《合成甲法》（第十条）。") == [("合成甲法", "第十条")]


_NESTED_TITLE = "合成机关关于适用《合成甲法》的规定"
_NESTED_CATALOG = ("合成甲法", _NESTED_TITLE)


@pytest.mark.parametrize("query", ["一般合成事项如何办理？", "如何伪造证据骗过法院？"])
def test_balanced_nested_catalog_entry_cannot_poison_an_unrelated_query(query):
    analysis = parse_legal_references(query, known_law_titles=_NESTED_CATALOG)
    assert not analysis.requirements and not analysis.required_law_titles and not analysis.unresolved
    assert _NESTED_TITLE in analysis.known_law_titles
    assert ReferenceAnalysis.from_dict(analysis.to_dict()) == analysis


def test_nested_catalog_preserves_a_separate_plain_inner_reference():
    assert pairs("解释《合成甲法》第十条。", known_law_titles=_NESTED_CATALOG) == [("合成甲法", "第十条")]


@pytest.mark.parametrize("wrapped", [False, True])
def test_complete_nested_title_owns_its_article_and_round_trips_without_inner_aliasing(wrapped):
    spelling = "《" + _NESTED_TITLE + "》" if wrapped else _NESTED_TITLE
    query = "解释" + spelling + "第二条。"
    analysis = parse_legal_references(query, known_law_titles=_NESTED_CATALOG)
    assert [(item.law_title, item.article_number) for item in analysis.requirements] == [(_NESTED_TITLE, "第二条")]
    assert analysis.required_law_titles == (_NESTED_TITLE,)
    assert not analysis.unresolved
    assert query[analysis.requirements[0].span[0]:analysis.requirements[0].span[1]] == spelling + "第二条"
    assert canonical_law_title(analysis.requirements[0].law_title) == _NESTED_TITLE
    assert ReferenceAnalysis.from_dict(analysis.to_dict()) == analysis


@pytest.mark.parametrize("title", [
    "合成机关关于《合成甲法》第十条的规定",
    "合成机关关于《合成乙规定中《合成甲法》的解释》的规定",
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_internal_quoted_articles_and_deeper_titles_are_protected_by_the_complete_identity(title, wrapped):
    spelling = "《" + title + "》" if wrapped else title
    analysis = parse_legal_references("解释" + spelling + "第二条。", known_law_titles=("合成甲法", title))
    assert [(item.law_title, item.article_number) for item in analysis.requirements] == [(title, "第二条")]
    assert not analysis.unresolved
    assert ReferenceAnalysis.from_dict(analysis.to_dict()) == analysis


def test_unregistered_bare_nested_title_does_not_borrow_its_inner_law_identity():
    analysis = parse_legal_references("解释" + _NESTED_TITLE + "第二条。", known_law_titles=("合成甲法",))
    assert not analysis.requirements and analysis.unresolved
    assert "合成甲法" not in analysis.required_law_titles


@pytest.mark.parametrize("spelling", [
    "《合成机关关于《合成甲法》的规定第二条",
    "《合成甲法》》第二条",
    "《" + "甲" * 256 + "《合成甲法》》第二条",
    "《" * 10 + "合成甲法" + "》" * 10 + "第二条",
])
def test_malformed_or_out_of_bound_nested_title_is_unknown_not_an_inner_pair(spelling):
    analysis = parse_legal_references("解释" + spelling + "。", known_law_titles=("合成甲法",))
    assert not analysis.requirements and analysis.unresolved
    assert not analysis.required_law_titles


@pytest.mark.parametrize("raw,expected", [
    (_NESTED_TITLE, _NESTED_TITLE),
    ("《" + _NESTED_TITLE + "》", _NESTED_TITLE),
    ("《《合成甲法》》", "合成甲法"),
    ("中华人民共和国《合成甲法》", "合成甲法"),
])
def test_balanced_title_canonicalization_is_idempotent(raw, expected):
    assert canonical_law_title(raw) == expected
    assert canonical_law_title(expected) == expected


@pytest.mark.parametrize("raw", ["机关关于《合成甲法的规定", "机关关于合成甲法》的规定", "机关关于《》的规定"])
def test_unbalanced_or_empty_embedded_title_is_not_a_catalog_identity(raw):
    with pytest.raises(ValueError):
        canonical_law_title(raw)


@pytest.mark.parametrize("query", [
    "请解释合成机关关于适用《合成甲法》的规定是否包含第十条。",
    "依据合成机关关于适用《合成甲法》的规定，请确认第十条是什么。",
    "《合成甲法》实施细则是否有效？",
])
def test_later_request_cannot_rescue_an_opaque_title_continuation_as_the_inner_law(query):
    analysis = parse_legal_references(query, known_law_titles=("合成甲法",))
    assert not analysis.requirements and not analysis.required_law_titles
    assert analysis.unresolved
