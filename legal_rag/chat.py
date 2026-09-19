from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .adaptive import AdaptiveRetrievalResult, retrieve_adaptive
from .evidence import build_low_confidence_answer, check_evidence_sufficiency
from .llm import build_completion_client
from .models import (
    AnswerClaim,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
    VerificationResult,
)
from .query import analyze_query, extract_article_numbers, extract_law_names
from .retrieval import Retriever, format_sources
from .verifier import (
    build_verifier_fallback_answer,
    filter_results_to_context,
    parse_structured_answer,
    verify_answer,
)


LEGAL_DISCLAIMER = "仅供课程学习和法律文本检索参考，不构成法律意见。"
PRE_RETRIEVAL_REFUSAL_FLAGS = {
    "case_strategy",
    "illegal_help",
    "medical_financial_advice",
    "non_legal",
}


@dataclass
class ConversationMemory:
    token_limit: int = 2000
    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        self.messages.append((role, content))
        self._trim()

    def render(self) -> str:
        return "\n".join(f"{role}: {content}" for role, content in self.messages[-8:])

    def last_user_question(self) -> str:
        for role, content in reversed(self.messages):
            if role == "user":
                return content
        return ""

    def last_assistant_answer(self) -> str:
        for role, content in reversed(self.messages):
            if role == "assistant":
                return content
        return ""

    def clear(self) -> None:
        self.messages.clear()

    def _trim(self) -> None:
        while estimate_tokens(self.render()) > self.token_limit and self.messages:
            self.messages.pop(0)


class LegalChatAssistant:
    def __init__(
        self,
        retriever: Retriever,
        *,
        model: str,
        top_k: int = 5,
        memory_token_limit: int = 2000,
        ollama_base_url: str = "http://localhost:11434",
        request_timeout: int = 180,
        adaptive_enabled: bool = False,
        adaptive_use_llm: bool = False,
        adaptive_max_queries: int = 3,
        adaptive_per_plan_top_k: int | None = None,
        normalizer_retries: int = 0,
        condense_with_llm: bool = False,
        verification_context: VerificationContext | None = None,
    ) -> None:
        self.retriever = retriever
        self.model = model
        self.top_k = top_k
        self.adaptive_enabled = adaptive_enabled
        self.adaptive_use_llm = adaptive_use_llm
        self.adaptive_max_queries = adaptive_max_queries
        self.adaptive_per_plan_top_k = adaptive_per_plan_top_k
        self.normalizer_retries = normalizer_retries
        self.condense_with_llm = condense_with_llm
        self.verification_context = verification_context
        self.last_adaptive_result: AdaptiveRetrievalResult | None = None
        self.last_evidence_check: EvidenceCheck | None = None
        self.last_verification: VerificationResult | None = None
        self.last_pre_fallback_verification: VerificationResult | None = None
        self.last_pre_fallback_answer: StructuredAnswer | None = None
        self.last_structured_answer: StructuredAnswer | None = None
        self.last_rejected_original_source_ids: list[str] = []
        self.last_evidence_source_id_map: dict[str, str] = {}
        self.last_generation_error: str | None = None
        self.memory = ConversationMemory(token_limit=memory_token_limit)
        self.llm = build_completion_client(
            model,
            ollama_base_url=ollama_base_url,
            request_timeout=request_timeout,
        )

    def reset_memory(self) -> None:
        """Clear conversation state so independent questions do not leak context.

        Used by evaluation, where each case must be answered in isolation.
        """
        self.memory.clear()
        self.last_adaptive_result = None
        self.last_evidence_check = None
        self.last_verification = None
        self.last_pre_fallback_verification = None
        self.last_pre_fallback_answer = None
        self.last_structured_answer = None
        self.last_rejected_original_source_ids = []
        self.last_evidence_source_id_map = {}
        self.last_generation_error = None

    def answer(self, question: str, *, generate: bool = True) -> tuple[str, list[SearchResult]]:
        standalone_question = self.condense_question(question)
        pre_analysis = analyze_query(standalone_question)
        self.last_adaptive_result = None
        self.last_evidence_check = None
        self.last_verification = None
        self.last_pre_fallback_verification = None
        self.last_pre_fallback_answer = None
        self.last_structured_answer = None
        self.last_rejected_original_source_ids = []
        self.last_evidence_source_id_map = {}
        self.last_generation_error = None
        if should_refuse_before_retrieval(pre_analysis.risk_flags):
            answer = programmatic_answer(
                build_risk_refusal_answer(pre_analysis.risk_flags),
                answer_mode="out_of_scope",
                limitations=["请求超出法律文本学习助手允许的帮助范围。"],
            )
            answer, verification = self._verify_programmatic_answer(
                answer,
                [],
                expected_answer_mode="out_of_scope",
                risk_flags=pre_analysis.risk_flags,
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
            self.last_structured_answer = answer
            self.last_verification = verification
            self.memory.add("user", question)
            self.memory.add("assistant", answer.answer_text)
            return answer.answer_text, []
        adaptive_result = retrieve_adaptive(
            standalone_question,
            self.retriever,
            top_k=self.top_k,
            enabled=self.adaptive_enabled,
            use_llm=self.adaptive_use_llm,
            llm_client=self.llm if self.adaptive_use_llm else None,
            max_queries=self.adaptive_max_queries,
            per_plan_top_k=self.adaptive_per_plan_top_k,
            normalizer_retries=self.normalizer_retries,
        )
        results, rejected_source_ids, source_id_map = filter_results_to_context(
            adaptive_result.results,
            self.verification_context,
        )
        if rejected_source_ids:
            evidence_check = check_evidence_sufficiency(
                standalone_question,
                results,
                analysis=adaptive_result.analysis,
                normalized_query=adaptive_result.normalized_query,
                plans=adaptive_result.plans,
            )
            evidence_check = replace(evidence_check, stop_reason="evidence_scope_filtered")
            adaptive_result = replace(
                adaptive_result,
                results=results,
                evidence_check=evidence_check,
                merge_trace={
                    **adaptive_result.merge_trace,
                    "evidence_filter": {
                        "rejected_original_source_ids": rejected_source_ids,
                        "source_id_map": source_id_map,
                        "returned_count": len(results),
                    },
                },
            )
        self.last_rejected_original_source_ids = rejected_source_ids
        self.last_evidence_source_id_map = source_id_map
        self.last_adaptive_result = adaptive_result
        self.last_evidence_check = adaptive_result.evidence_check

        if not results:
            answer = build_limited_structured_answer(adaptive_result.evidence_check)
            answer, verification = self._verify_programmatic_answer(
                answer,
                [],
                evidence_check=adaptive_result.evidence_check,
                risk_flags=adaptive_result.analysis.risk_flags,
                expected_answer_mode=expected_limited_answer_mode(adaptive_result.evidence_check),
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
            self.last_structured_answer = answer
            self.last_verification = verification
            self.memory.add("user", question)
            self.memory.add("assistant", answer.answer_text)
            return answer.answer_text, results

        if adaptive_result.evidence_check and not adaptive_result.evidence_check.sufficient:
            answer = build_limited_structured_answer(adaptive_result.evidence_check)
            answer, verification = self._verify_programmatic_answer(
                answer,
                results,
                evidence_check=adaptive_result.evidence_check,
                risk_flags=adaptive_result.analysis.risk_flags,
                expected_answer_mode=expected_limited_answer_mode(adaptive_result.evidence_check),
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
            self.last_structured_answer = answer
            self.last_verification = verification
            self.memory.add("user", question)
            self.memory.add("assistant", answer.answer_text)
            return answer.answer_text, results

        if not generate:
            answer_text = append_disclaimer(render_retrieval_only_answer(results))
            self.memory.add("user", question)
            self.memory.add("assistant", answer_text)
            return answer_text, results
        else:
            prompt = build_qa_prompt(
                question=standalone_question,
                original_question=question,
                memory=self.memory.render(),
                results=results,
            )
            try:
                raw_answer = self.llm.complete(prompt)
            except Exception:
                self.last_generation_error = "generation_error"
                answer = programmatic_answer(
                    "当前无法连接生成模型，因此无法给出可靠结论。",
                    answer_mode="insufficient_evidence",
                    limitations=["生成模型当前不可用。"],
                )
                answer, verification = self._verify_programmatic_answer(
                    answer,
                    results,
                    expected_answer_mode="insufficient_evidence",
                    disclaimer=LEGAL_DISCLAIMER,
                    context=self.verification_context,
                )
                self.last_structured_answer = answer
                self.last_verification = verification
                self.memory.add("user", question)
                self.memory.add("assistant", answer.answer_text)
                return answer.answer_text, results

        answer = parse_structured_answer(raw_answer)
        answer = replace(answer, answer_text=append_disclaimer(answer.answer_text))
        verification = verify_answer(
            answer,
            results,
            evidence_check=adaptive_result.evidence_check,
            risk_flags=adaptive_result.analysis.risk_flags,
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
            context=self.verification_context,
        )
        if not verification.passed:
            self.last_pre_fallback_verification = verification
            self.last_pre_fallback_answer = answer
            low_confidence = build_low_confidence_answer(adaptive_result.evidence_check)
            fallback_text = build_verifier_fallback_answer(
                answer.answer_text,
                verification,
                low_confidence_answer=low_confidence,
            )
            answer = programmatic_answer(
                fallback_text,
                answer_mode="insufficient_evidence",
                limitations=["原生成回答未通过结构或行为检查。"],
            )
            answer, verification = self._verify_programmatic_answer(
                answer,
                results,
                expected_answer_mode="insufficient_evidence",
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
        self.last_structured_answer = answer
        self.last_verification = verification
        self.memory.add("user", question)
        self.memory.add("assistant", answer.answer_text)
        return answer.answer_text, results

    def _verify_programmatic_answer(
        self,
        answer: StructuredAnswer,
        results: list[SearchResult],
        *,
        expected_answer_mode: str,
        evidence_check: EvidenceCheck | None = None,
        risk_flags: list[str] | None = None,
        disclaimer: str = "",
        context: VerificationContext | None = None,
    ) -> tuple[StructuredAnswer, VerificationResult]:
        verification = verify_answer(
            answer,
            results,
            expected_answer_mode=expected_answer_mode,
            evidence_check=evidence_check,
            risk_flags=risk_flags,
            disclaimer=disclaimer,
            context=context,
        )
        if verification.passed:
            return answer, verification

        safe_answer = safe_terminal_answer(expected_answer_mode)
        safe_verification = verify_answer(
            safe_answer,
            results,
            expected_answer_mode=expected_answer_mode,
            evidence_check=evidence_check,
            risk_flags=risk_flags,
            disclaimer=disclaimer,
            context=context,
        )
        if not safe_verification.passed:
            raise RuntimeError("programmatic terminal response failed verification")
        return safe_answer, safe_verification

    def condense_question(self, question: str) -> str:
        last_question = self.memory.last_user_question()
        if not last_question:
            return question
        pronouns = ("这个", "这条", "上一条", "刚才", "它", "该条", "这一条")
        if not any(word in question for word in pronouns):
            return question
        if self.condense_with_llm:
            rewritten = self.condense_with_llm_rewrite(question, last_question)
            if rewritten:
                return rewritten
        # Rule fallback: carry over the previous question plus the laws and
        # article numbers the assistant just cited, so retrieval keeps anchors.
        references = extract_reference_hints(self.memory.last_assistant_answer())
        condensed = f"结合上一轮问题“{last_question}”，回答：{question}"
        if references:
            condensed += f"（上一轮涉及：{'、'.join(references)}）"
        return condensed

    def condense_with_llm_rewrite(self, question: str, last_question: str) -> str:
        prompt = (
            "把用户的追问改写成一个不依赖上下文的独立中文检索问题。\n"
            "只输出改写后的问题本身，不要解释，不要加引号。\n\n"
            f"上一轮问题: {last_question}\n"
            f"上一轮回答摘要: {self.memory.last_assistant_answer()[:300]}\n"
            f"用户追问: {question}\n"
        )
        try:
            rewritten = self.llm.complete(prompt).strip().strip("\"'“”")
        except Exception:
            return ""
        if 0 < len(rewritten) <= 120 and "\n" not in rewritten:
            return rewritten
        return ""


def build_qa_prompt(
    *,
    question: str,
    original_question: str,
    memory: str,
    results: list[SearchResult],
) -> str:
    context_lines = []
    for result in results:
        chunk = result.chunk
        law = "、".join(chunk.law_names) or "未知法律"
        article = "、".join(chunk.article_numbers) or "未知条文"
        context_lines.append(
            f"[S{result.rank}] 来源: {law} {article}; 文件: {chunk.source_files[0] if chunk.source_files else ''}\n{chunk.text}"
        )
    context = "\n\n".join(context_lines)
    return f"""你是一个中国现行法律文本学习助手。
你只能根据给定的检索资料回答。资料不足时必须拒答，不能编造法律依据。
回答要求:
1. 用中文回答。
2. 先给结论，再列出依据。
3. 必须引用资料编号，例如 [S1]。
4. 具体案件策略、胜诉判断、个性化法律意见必须拒答。
5. 末尾保留免责声明: {LEGAL_DISCLAIMER}
6. 只输出一个 JSON 对象，不要输出 Markdown 代码围栏或额外说明。
7. JSON 必须且只能包含以下字段:
   - answer_text: 面向用户的完整字符串
   - answer_mode: evidence_answer / insufficient_evidence / needs_clarification / out_of_scope 之一
   - claims: 数组；每项只能包含 claim_id、text、source_ids
   - limitations: 字符串数组
   - clarification_question: 字符串或 null
8. 每条需要证据支撑的 claim 都要绑定本次资料中的 source_ids，禁止生成不存在的编号。
9. 不要在 JSON 中填写 snapshot、用户、权限或其他系统字段。

最近对话:
{memory or "无"}

用户原始问题:
{original_question}

独立检索问题:
{question}

检索资料:
{context}

请给出回答:
"""


def programmatic_answer(
    answer_text: str,
    *,
    answer_mode: str,
    limitations: list[str] | None = None,
    clarification_question: str | None = None,
    claims: list[AnswerClaim] | None = None,
) -> StructuredAnswer:
    if answer_mode != "evidence_answer":
        answer_text = sanitize_source_tokens(answer_text)
        limitations = [sanitize_source_tokens(item) for item in limitations or []]
        if clarification_question is not None:
            clarification_question = sanitize_source_tokens(clarification_question)
    return StructuredAnswer(
        answer_text=append_disclaimer(answer_text),
        answer_mode=answer_mode,
        claims=claims or [],
        limitations=limitations or [],
        clarification_question=clarification_question,
        adapter_source="programmatic",
    )


def safe_terminal_answer(answer_mode: str) -> StructuredAnswer:
    if answer_mode == "out_of_scope":
        return programmatic_answer(
            "这个请求超出当前法律文本学习助手的范围，我不能提供具体操作方案。",
            answer_mode="out_of_scope",
            limitations=["请求超出允许范围。"],
        )
    if answer_mode == "needs_clarification":
        question = "请补充作答所需的关键事实。"
        return programmatic_answer(
            "当前信息不足，需要补充关键事实后再检索。\n\n" + question,
            answer_mode="needs_clarification",
            limitations=["缺少作答所需的关键事实。"],
            clarification_question=question,
        )
    return programmatic_answer(
        "当前检索资料不足，无法给出可靠结论。",
        answer_mode="insufficient_evidence",
        limitations=["当前检索资料不足。"],
    )


def sanitize_source_tokens(text: str) -> str:
    """Prevent user/provider text from being interpreted as trusted citations."""

    return re.sub(r"\[S(\d+)\]", r"S\1", text)


def build_limited_structured_answer(check: EvidenceCheck) -> StructuredAnswer:
    text = build_low_confidence_answer(check)
    limitations = [
        *check.missing_law_support,
        *check.missing_facts,
        *check.low_coverage,
    ]
    if check.missing_facts:
        clarification = "请补充这些事实信息：" + "、".join(check.missing_facts) + "？"
        return programmatic_answer(
            text + "\n\n" + clarification,
            answer_mode="needs_clarification",
            limitations=limitations,
            clarification_question=clarification,
        )
    return programmatic_answer(
        text,
        answer_mode="insufficient_evidence",
        limitations=limitations or ["当前检索资料不足。"],
    )


def expected_limited_answer_mode(check: EvidenceCheck) -> str:
    return "needs_clarification" if check.missing_facts else "insufficient_evidence"


def render_retrieval_only_answer(results: list[SearchResult]) -> str:
    snippets = []
    for result in results:
        chunk = result.chunk
        snippets.append(f"[S{result.rank}] {chunk.text[:400]}")
    return "检索到以下可能相关的法律依据：\n\n" + "\n\n".join(snippets)


def should_refuse_before_retrieval(risk_flags: list[str]) -> bool:
    return bool(set(risk_flags) & PRE_RETRIEVAL_REFUSAL_FLAGS)


def build_risk_refusal_answer(risk_flags: list[str]) -> str:
    if "illegal_help" in risk_flags:
        reason = "我不能提供违法帮助、规避执法或相关操作方案。"
    elif "case_strategy" in risk_flags:
        reason = "我不能直接给出具体案件策略、胜诉判断或个性化法律意见。"
    elif "medical_financial_advice" in risk_flags:
        reason = "我不能提供医疗、金融或投资等专业建议。"
    else:
        reason = "这个问题不属于当前中国现行法律文本检索范围。"
    return append_disclaimer(reason + "\n\n我可以帮助检索相关法律条文或解释公开法律文本。")


def append_disclaimer(answer: str) -> str:
    if answer.rstrip().endswith(LEGAL_DISCLAIMER):
        return answer
    return answer.rstrip() + "\n\n" + LEGAL_DISCLAIMER


def extract_reference_hints(text: str, limit: int = 4) -> list[str]:
    if not text:
        return []
    hints = extract_law_names(text) + extract_article_numbers(text)
    return hints[:limit]


def estimate_tokens(text: str) -> int:
    """Approximate token count: one token per CJK char, one per ASCII word.

    The old `len(text) // 2` heuristic underestimated Chinese text by ~2x and
    let the memory window grow far beyond its configured limit.
    """
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    ascii_words = len(re.findall(r"[a-zA-Z0-9]+", text))
    other_chars = len(re.findall(r"[^\u4e00-\u9fffa-zA-Z0-9\s]", text))
    return max(1, cjk_chars + ascii_words + other_chars // 2)


def render_sources(results: list[SearchResult]) -> str:
    return format_sources(results)
