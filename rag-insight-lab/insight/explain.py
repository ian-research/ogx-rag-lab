"""Model explanation over program-computed facts (OGX /v1/chat/completions)."""
from __future__ import annotations

import json
import re
import time

import httpx

from . import core

SYSTEM = """你是企业运维分析助手。你只解释给定的证据，不执行任何操作，也不调用工具。
要求：
1. 只使用证据里的数字与标识；没有的就说“证据中没有”。
2. 把“观察到的事实”和“解释/推断”分开写，推断处标注“推测”。
3. 引用只能使用“证据元信息”里给出的那个编号，写成 [E-...]；不得写 [<证据编号>]、[<证据元信息>] 这类占位符。
4. 不给 P95、健康评分、根因定论；样本不足或缺少数据时明确说无法判断。
5. 输出四个部分：事实、解释、证据、下一步检查。中文，300 字以内。
"""


def build_prompt(bundle: dict, question: str) -> list[dict]:
    facts = json.dumps(bundle["summary"], ensure_ascii=False, indent=2, sort_keys=True)
    meta = {
        "evidence_id": bundle["evidence_id"],
        "question_type": bundle["question_type"],
        "window": bundle["window"],
        "frozen_end": bundle["frozen_end"],
        "sources": [{"name": s["name"], "sha256": s["sha256"]} for s in bundle["sources"]],
        "limits": bundle["limits"],
    }
    user = (
        f"问题：{question}\n\n"
        f"证据元信息：\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"程序计算结果（已统一单位为毫秒或次数）：\n{facts}\n\n"
        f"请按四部分回答。证据部分必须原样写出编号 [{bundle['evidence_id']}]，不得改写或省略。"
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


class Explainer:
    def __init__(self, base: str = core.OGX_BASE, model: str = core.OGX_MODEL):
        self.base = base.rstrip("/")
        self.model = model
        self.temperature = 0.2
        self.max_tokens = 700

    def request_body(self, messages: list[dict]) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # The served model is a reasoning model: without this every token goes
            # to the reasoning channel and content comes back empty.
            "chat_template_kwargs": {"enable_thinking": False},
        }

    async def explain(self, bundle: dict, question: str) -> dict:
        messages = build_prompt(bundle, question)
        body = self.request_body(messages)
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=core.MODEL_TIMEOUT) as client:
            try:
                resp = await client.post(f"{self.base}/v1/chat/completions", json=body)
            except httpx.HTTPError as exc:
                return {
                    "ok": False,
                    "status": "model_unavailable",
                    "error": f"model_unavailable: {type(exc).__name__}",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "request": body,
                }
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if resp.status_code != 200:
            return {
                "ok": False,
                "status": "model_http_error",
                "error": f"model_http_{resp.status_code}",
                "elapsed_ms": elapsed_ms,
                "request": body,
                "raw_text": resp.text[:2000],
            }
        payload = resp.json()
        choice = (payload.get("choices") or [{}])[0]
        answer = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        status = "ok"
        if not answer:
            status = "empty_answer"
        elif finish == "length":
            status = "truncated"
        return {
            "ok": status == "ok",
            "status": status,
            "answer": answer,
            "finish_reason": finish,
            "usage": payload.get("usage"),
            "elapsed_ms": elapsed_ms,
            "model": self.model,
            "temperature": self.temperature,
            "enable_thinking": False,
            "response_sha256": core.sha256_text(resp.text),
            "request_sha256": core.sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True)),
            "request": body,
            "raw_response": payload,
        }


def guard_answer(answer: str, bundle: dict, status: str = "ok") -> dict:
    """Structural and citation checks; semantics still need a human."""
    answer = answer or ""
    sections = ["事实", "解释", "证据", "下一步"]
    missing = [s for s in sections if s not in answer]
    refs = []
    for raw in answer.replace("[", " ").replace("]", " ").split():
        token = raw.strip("，。,.;：:")
        if token.startswith("E-"):
            refs.append(token)
    placeholders = sorted({t for t in re.findall(r"\[[^\]]*\]", answer)
                           if "<" in t or "证据编号" in t or "证据元信息" in t})
    known = {bundle["evidence_id"]}
    unknown = sorted({r for r in refs if r not in known})
    cited = sorted(set(refs) & known)
    citation_ok = bool(cited) and not unknown and not placeholders
    if status != "ok":
        verdict = status
    elif missing:
        verdict = "structure_missing"
    elif not cited:
        verdict = "citation_missing"
    elif placeholders:
        verdict = "citation_placeholder"
    elif unknown:
        verdict = "citation_unknown"
    else:
        verdict = "ok"
    return {
        "sections_present": not missing,
        "missing_sections": missing,
        "citations": sorted(set(refs)),
        "known_citations": cited,
        "unknown_citations": unknown,
        "placeholder_citations": placeholders,
        "citation_supports": "unverified",  # an id can exist without supporting the claim
        "citation_ok": citation_ok,
        "chars": len(answer),
        "verdict": verdict,
        "structural_pass": verdict == "ok",
    }
