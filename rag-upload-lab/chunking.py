"""保留标题，优先按句子边界截取；所有完整片段经过实际 tokenizer 校验。"""
import hashlib
import re

from settings import MAX_TOKENS


def make_chunks(blocks, tokenizer, document_id, filename, sha256):
    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=True))

    result = []
    for block_no, block in enumerate(blocks, 1):
        heading = block.get("heading", "")
        # 过长标题压缩到 64 tokens 以内，确保正文有空间。
        while heading and count("标题：" + heading) > 64:
            heading = heading[:max(0, len(heading) // 2)]
        prefix = f"标题：{heading}\n" if heading else ""
        remaining = block["text"].strip()
        piece = 0
        while remaining:
            end = min(len(remaining), MAX_TOKENS)
            while end and count(prefix + remaining[:end]) > MAX_TOKENS:
                end //= 2
            if not end:
                raise ValueError("无法在 token 上限内容纳正文")
            # 在长度预算内，优先选择后半部分的句号、换行等边界。
            if end < len(remaining):
                boundaries = [m.end() for m in re.finditer(r"[。！？!?；;\n]|\.(?=\s)", remaining[:end])]
                if boundaries and boundaries[-1] >= end // 2:
                    end = boundaries[-1]
            body = remaining[:end]
            remaining = remaining[end:].lstrip()
            piece += 1
            content = prefix + body
            tokens = count(content)
            if tokens > MAX_TOKENS:
                raise ValueError("分块超出模型输入预算")
            chunk_id = hashlib.sha256(f"{document_id}:{block_no}:{piece}:{content}".encode()).hexdigest()
            meta = {"document_id": document_id, "source": filename, "file_sha256": sha256,
                    "block": block_no, "piece": piece, "heading": block.get("heading", ""),
                    "kind": block.get("kind", "text")}
            if block.get("page") is not None:
                meta["page"] = str(block["page"])
            result.append({"chunk_id": chunk_id, "content": content, "metadata": meta,
                           "chunk_metadata": {"document_id": document_id, "chunk_id": chunk_id,
                                              "source": filename, "content_token_count": tokens}})
    return result
