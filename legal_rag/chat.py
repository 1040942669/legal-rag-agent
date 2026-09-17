from __future__ import annotations

import re
from dataclasses import dataclass, field

from .adaptive import AdaptiveRetrievalResult, retrieve_adaptive
from .evidence import build_low_confidence_answer
from .llm import build_completion_client
from .models import EvidenceCheck, SearchResult, VerificationResult
from .query import analyze_query, extract_article_numbers, extract_law_names
from .retrieval import Retriever, format_sources
from .verifier import build_verifier_fallback_answer, verify_answer


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
        self.last_adaptive_result: AdaptiveRetrievalResult | None = None
        self.last_evidence_check: EvidenceCheck | None = None
        self.last_verification: VerificationResult | None = None
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

    def answer(self, question: str, *, generate: bool = True) -> tuple[str, list[SearchResult]]:
        standalone_question = self.condense_question(question)
        pre_analysis = analyze_query(standalone_question)
        self.last_adaptive_result = None
        self.last_evidence_check = None
        self.last_verification = None
        if should_refuse_before_retrieval(pre_analysis.risk_flags):
            answer = build_risk_refusal_answer(pre_analysis.risk_flags)
            self.memory.add("user", question)
            self.memory.add("assistant", answer)
            return answer, []
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
        self.last_adaptive_result = adaptive_result
        self.last_evidence_check = adaptive_result.evidence_check
        results = adaptive_result.results

        if not results:
            answer = build_low_confidence_answer(adaptive_result.evidence_check)
            answer = append_disclaimer(answer)
            self.memory.add("user", question)
            self.memory.add("assistant", answer)
            return answer, results

        if adaptive_result.evidence_check and not adaptive_result.evidence_check.sufficient:
            answer = append_disclaimer(build_low_confidence_answer(adaptive_result.evidence_check))
            verification = verify_answer(
                answer,
                results,
                evidence_check=adaptive_result.evidence_check,
                risk_flags=adaptive_result.analysis.risk_flags,
                disclaimer=LEGAL_DISCLAIMER,
            )
            self.last_verification = verification
            self.memory.add("user", question)
            self.memory.add("assistant", answer)
            return answer, results

        if not generate:
            answer = render_retrieval_only_answer(results)
        else:
            prompt = build_qa_prompt(
                question=standalone_question,
                original_question=question,
                memory=self.memory.render(),
                results=results,
            )
            try:
                answer = self.llm.complete(prompt)
            except RuntimeError as exc:
                answer = (
                    "当前无法连接 Ollama 生成模型，先返回检索依据。\n\n"
                    + render_retrieval_only_answer(results)
                    + f"\n\n运行错误: {exc}"
                )

        answer = append_disclaimer(answer)
        verification = verify_answer(
            answer,
            results,
            evidence_check=adaptive_result.evidence_check,
            risk_flags=adaptive_result.analysis.risk_flags,
            disclaimer=LEGAL_DISCLAIMER,
        )
        self.last_verification = verification
        if not verification.passed:
            low_confidence = append_disclaimer(build_low_confidence_answer(adaptive_result.evidence_check))
            answer = build_verifier_fallback_answer(
                answer,
                verification,
                low_confidence_answer=low_confidence,
            )
            answer = append_disclaimer(answer)
        self.memory.add("user", question)
        self.memory.add("assistant", answer)
        return answer, results

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
        except RuntimeError:
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
        reason = "这个问题涉及违法帮助或规避执法，我不能提供操作方案。"
    elif "case_strategy" in risk_flags:
        reason = "这个问题涉及具体案件策略、胜诉判断或个性化法律意见，我不能直接给出方案。"
    elif "medical_financial_advice" in risk_flags:
        reason = "这个问题涉及医疗、金融或投资等专业建议，不能作为法律文本检索回答处理。"
    else:
        reason = "这个问题不属于当前中国现行法律文本检索范围。"
    return append_disclaimer(reason + "\n\n我可以帮助检索相关法律条文或解释公开法律文本。")


def append_disclaimer(answer: str) -> str:
    if LEGAL_DISCLAIMER in answer:
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
