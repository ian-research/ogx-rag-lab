# RAG 文件上传与后台入库实验

独立上传 API 和 CPU Worker。复用已有 OGX http://127.0.0.1:8321/v1、Embedding cpu-embedding/bge-small-zh（512 维）、pgvector provider local-pgvector、生成模型 local-vllm/rag-llm。不修改旧实验脚本、向量库或 OGX 配置，也不启用 OGX Files API。

## 能力

- 网页多文件上传，单文件最大 50 MiB；HTTP API 单次一个文件。
- SQLite 持久任务队列；排队、解析、分块、向量化、可检索、失败状态。
- 独立单 Worker，进程锁，任务子进程，超时、失败重试、中断恢复。
- 同文件名且内容相同的重复上传返回原任务；失败任务使用 retry 重试。
- 每个上传文档使用独立向量库，整个文档写入完成才对问答可见。部分失败的入库使用冻结片段和相同 chunk ID 重试，依赖 OGX pgvector 的 upsert 行为。
- 同名新内容创建新任务；默认问答只搜索每个文件名最近一次成功入库版本。新版本未完成时旧版本仍可检索。勾选文档时允许显式比较旧版本。
- 文档列表、错误详情、片段预览、原件下载，以及限定文件的问答和引用。
- `/api/search` 只返回检索证据，不调用生成模型；`/api/ask` 在同一检索函数之上调用生成模型。后续可把 search 包装为 Agent 工具，本版不运行 Agent 决策循环。

## 文件格式

|格式|解析方式|限制|
|---|---|---|
|MD/TXT|UTF-8 / UTF-8 BOM，标题与空行分段|MD 是轻量解析，不完整解析所有 Markdown 语法|
|DOCX|python-docx，按段落/表格顺序读取|表格第一行按表头处理；没有排版引擎，不能提供可靠页码；图片中的文字不做 OCR|
|PDF|文字页：Docling 2.128.0 版面/表格解析；扫描页：独立 RapidOCR 中文模型（含英文），ONNX CPU|首次下载模型；自动判断依据是该页文字层少于 20 个字符；扫描页按识别文本入库，暂不重建复杂表格结构|
|DOC|LibreOffice headless 转为 DOCX 再解析|需额外安装 LibreOffice Writer；不存在通用 .docs 扩展名|

不完整的 PDF 转换会让任务失败，避免悄悄索引一部分。解析模型首次下载需要服务器访问相关模型站点。失败后查看日志，修复网络或依赖再重试。

文字层存在但内容损坏、或同页既有正常正文又有图片文字时，自动模式可能遗漏图片文字。可给 Worker 设置 `RAG_PDF_FORCE_OCR=1` 强制整页 OCR（应用于后续解析），再上传新版本/重试未生成片段的失败任务。已生成的片段在重试时会复用；要重新解析已成功文档，可用新文件名上传。OCR 的事实正确性仍需要人工抽检，特别是数字、金额和表格。

## Chunk 规则

DOCX 的 Word 内置 Title 样式作为文档总标题，加入后续正文与表格的标题路径，不再单独生成片段。Heading 章节按级别组织；普通正文不会仅因出现在首行而被删除。既有 Word 数据可运行 `python reindex_document.py 文档ID` 重新处理，使用新任务和新库；新任务成功后默认检索切换到同名新版本。旧版本仍保留，显式选择旧 ID 仍会查旧数据。

保留标题上下文，PDF 尽可能保留物理页码，DOCX 表格按行重复列名。每个结构块独立切片，优先在句末/换行结束；极长句子按长度拆分。实际使用本地 BGE tokenizer 校验，包含标题和特殊 token 后不超过 384 tokens。

当前没有重叠、没有跨结构块合并。PDF 大表格转换为 Markdown 后仍可能切断一行，不保证复杂表格关系完整；片段预览用于检查。metadata 保存文件名、文件原始字节 SHA256、document_id、结构块号、片段号、标题、可用页码。结构块号不等同于 Word 的可视段落号。

## 部署（AWS RHEL 10，当前 root 实验环境）

先按根目录[部署手册](../部署复现手册.md)准备 vLLM、数据库、OGX 和 BGE 模型。新部署在 `/root/rag-lab` 克隆本仓库后，将 `ogx-rag-lab/rag-upload-lab` 复制到 `/root/rag-lab/rag-upload-lab`，已有应用目录不覆盖。

```bash
cd /root/rag-lab/rag-upload-lab
bash install.sh
```

安装脚本只创建/使用 /root/rag-lab/.venv-ingest。先从官方 CPU 索引安装 torch/torchvision，再使用精确版本约束安装其余依赖；安装后执行 pip check，并写入 requirements.installed.lock。BGE tokenizer 读取已有 /root/rag-lab/models/bge-small-zh-v1.5，不下载新的 Embedding 模型。

若要处理 .doc，先在服务器的可用软件源检查 LibreOffice Writer/headless 包，按该 RHEL 镜像的软件源安装；无法安装时请另存为 DOCX。这部分不由 install.sh 自动修改系统软件源。

启动两个 systemd 服务：

```bash
cp deploy/rag-upload-api.service deploy/rag-upload-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rag-upload-api rag-upload-worker
curl --fail-with-body http://127.0.0.1:8088/api/health
```

期望 api=ok、worker_alive=true。服务仅监听 127.0.0.1，没有登录和 ACL；只供当前单用户实验，通过 SSH 隧道访问，不直接开放公网端口。当前 unit 沿用你的 root 实验目录；多人或生产部署需要独立低权限账户、认证、授权和上传隔离。

Mac 本地终端使用你现有的 SSH 登录方式增加转发：

```bash
ssh -i /实际路径/密钥.pem -N -L 8088:127.0.0.1:8088 ec2-user@服务器公网地址
```

保持 SSH 连接，在本地浏览器打开 http://127.0.0.1:8088。若已经使用 ssh config，可使用 `ssh -N -L 8088:127.0.0.1:8088 你的主机别名`。

## 验收顺序

1. 上传 examples/it-policy.md，状态到 ready，检查来源和三个政策片段。
2. 提问“申请软件许可证需要谁审批？”，确认答案与引用。
3. 上传 examples/it-policy.docx，检查标题和段落；再用自己的简单表格检查列名与行内容。
4. 上传一个 1–2 页 PDF；首次解析模型下载较慢，检查任务日志。再验证你自己的中文扫描件。
5. 重复上传同一份文件，确认返回相同任务 ID。
6. 上传修改后的同名文件，成功前旧版仍可检索，成功后默认查询切到新版。

## HTTP API

```bash
curl --fail-with-body -sS -F 'file=@examples/it-policy.md' http://127.0.0.1:8088/api/documents
curl --fail-with-body -sS http://127.0.0.1:8088/api/documents
curl --fail-with-body -sS http://127.0.0.1:8088/api/documents/任务ID
curl --fail-with-body -sS http://127.0.0.1:8088/api/documents/任务ID/chunks
curl --fail-with-body -sS -X POST http://127.0.0.1:8088/api/documents/任务ID/retry
curl --fail-with-body -sS http://127.0.0.1:8088/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"申请软件许可证需要谁审批？"}'
```

把 `/api/ask` 改成 `/api/search` 可单独验收检索证据，参数格式相同。

问答可附加 `document_ids: ["任务ID"]` 限定范围；空列表会拒绝。每次最多检索 20 个文档，每个库取 3 条候选，按相同 pgvector provider 返回分数合并，最终取 3 条。分数不是概率，也没有配置通用相关性阈值。库多时会逐库查询，延迟上升；后续扩展为共享索引+文档过滤。

## 日志、文件与备份

```bash
journalctl -u rag-upload-api -u rag-upload-worker -n 100 --no-pager
systemctl status rag-upload-api rag-upload-worker --no-pager
```

- data/jobs.sqlite：任务与文档登记；备份时注意 SQLite WAL 或停止两个服务后复制。
- data/uploads/：原始文件。
- data/artifacts/任务ID/blocks.json：解析后的结构块。
- data/artifacts/任务ID/chunks.json：冻结的入库片段，不保存向量副本。
- data/artifacts/任务ID.log：该任务的详细处理日志。
- data/models/rapidocr/：OCR 模型缓存，可通过 RAG_OCR_CACHE 修改位置。
- OGX 自身状态目录及 PostgreSQL 数据需要同时备份；单独保存 SQLite 不包含向量。

默认每个任务 3600 秒上限（systemd 环境变量 RAG_TASK_TIMEOUT）。只运行一个 Worker；需停用手工 Worker 后再启用 systemd。正常重启会恢复中断任务。每文件独立库和原始文件会持续占用空间；这一版没有删除接口与保留期清理。创建远端库成功但尚未记录 ID 时发生异常中断，可能留下孤立空库，后续需要人工核查清理。

## 本地测试

```bash
/root/rag-lab/.venv-ingest/bin/python -m pip install pytest
/root/rag-lab/.venv-ingest/bin/python -m pytest tests -q
```

测试中的 OGX 为模拟接口，验证任务与重试流程，不代替真实服务器联调。实际解析诊断：

```bash
/root/rag-lab/.venv-ingest/bin/python check_environment.py --parse examples/it-policy.docx
/root/rag-lab/.venv-ingest/bin/python check_environment.py --parse /实际路径/测试.pdf
```

## 参考

- Docling 官方 PDF/OCR 配置：https://docling-project.github.io/docling/reference/pipeline_options/
- FastAPI 关于重计算后台任务的说明：https://fastapi.tiangolo.com/tutorial/background-tasks/
