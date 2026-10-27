"""只读检查接口 + 发一次生成/Embedding请求，不写向量库，不上传文件。"""
import json
import urllib.request


def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)

for port in (8000, 8321):
    models = request(f"http://127.0.0.1:{port}/v1/models")
    print(port, "模型:", [x["id"] for x in models["data"]])
vectors = request("http://127.0.0.1:8321/v1/embeddings", {
    "model": "cpu-embedding/bge-small-zh", "input": ["员工电脑使用满4年可以申请换新。"],
    "encoding_format": "float"})
assert len(vectors["data"][0]["embedding"]) == 512
print("Embedding: 512维")
answer = request("http://127.0.0.1:8321/v1/chat/completions", {
    "model": "local-vllm/rag-llm",
    "messages": [{"role": "user", "content": "请只根据规定回答：电脑使用满4年可以申请换新。用了3年是否达到年限？"}],
    "chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.7,
    "top_p": 0.8, "max_tokens": 256})
print("OGX生成:", answer["choices"][0]["message"]["content"])
health = request("http://127.0.0.1:8088/api/health")
print("上传服务:", health)
assert health.get("api") == "ok" and health.get("worker_alive") is True
print("接口检查通过；回答语义仍需人工核对，这不是 RAG 质量评测。")
