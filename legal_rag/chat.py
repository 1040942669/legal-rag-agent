from __future__ import annotations

from dataclasses import dataclass, field

from .llm import OllamaClient
from .models import SearchResult
from .retrieval import Retriever, format_sources


LEGAL_DISCLAIMER = "仅供课程学习和法律文本检索参考，不构成法律意见。"


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
    ) -> None:
        self.retriever = retriever
        self.model = model
        self.top_k = top_k
        self.memory = ConversationMemory(token_limit=memory_token_limit)
        self.llm = OllamaClient(
            model=model,
            base_url=ollama_base_url,
            request_timeout=request_timeout,
        )

    def answer(self, question: str, *, generate: bool = True) -> tuple[str, list[SearchResult]]:
        standalone_question = self.condense_question(question)
        results = self.retriever.retrieve(standalone_question, top_k=self.top_k)

        if not results:
            answer = f"我无法在现行法律文本数据集中找到可靠依据。{LEGAL_DISCLAIMER}"
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

        if LEGAL_DISCLAIMER not in answer:
            answer = answer.rstrip() + "\n\n" + LEGAL_DISCLAIMER
        self.memory.add("user", question)
        self.memory.add("assistant", answer)
        return answer, results

    def condense_question(self, question: str) -> str:
        last_question = self.memory.last_user_question()
        if not last_question:
            return question
        pronouns = ("这个", "这条", "上一条", "刚才", "它", "该条", "这一条")
        if any(word in question for word in pronouns):
            return f"结合上一轮问题“{last_question}”，回答：{question}"
        return question


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


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def render_sources(results: list[SearchResult]) -> str:
    return format_sources(results)

