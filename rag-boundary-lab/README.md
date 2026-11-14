# 第八篇：提示注入与工具边界

在原 OGX / vLLM / Qwen3.5-9B 上观察模型读到受控工具返回后的反应；另行检查原应用的工具执行器。它是测试客户端，不增加常驻服务、检测模型或 GPU，也不向工单写入口发送请求。

## 本目录做什么

- `run_exposure.py`：4 组正常/攻击配对，共 8 例。每例先放入明确标注的合成只读工具消息，再只请求 **一次**真实模型续答；模型提出的工具调用只保存，不执行。
- `cases.json`：虚构设备、文档片段和注入材料。合成 assistant/tool 前置消息不是模型自主检索的证据。
- `program_checks/test_current_contract.py`：直接检查原应用的工具名单、拒绝强制写工具及会话身份绑定，不调用模型或写数据库。
- `tests/`：采集器本身的离线检查，使用 HTTP 替身，不能作为真实模型案例。

原应用的系统提示与 3 个工具声明从同仓库 `rag-observability-lab/model_loop.py` 读取，并核对固定 SHA256。与已核对版本不符时拒绝执行。没有把创建工单的工具加入模型接口；工单仍由应用在用户确认后提交。

## 安装与运行

在已有第一至第七篇服务的 RHEL 10 / Python 3.12 服务器执行。OGX 应在本机 8321 提供 `local-vllm/rag-llm`；服务与模型的部署继续使用前文说明。

```bash
cd rag-boundary-lab
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip check

# 只生成并检查材料，不请求模型；使用一个全新目录
.venv/bin/python run_exposure.py \
  --app-source ../rag-observability-lab --cases cases.json \
  --out /root/rag-lab/article08-evidence/dry-01 \
  --remaining-requests 0 --dry-run

# 真实采集恰好最多8次请求，无重试；目录不得与旧轮次相同
.venv/bin/python run_exposure.py \
  --app-source ../rag-observability-lab --cases cases.json \
  --out /root/rag-lab/article08-evidence/run-01 \
  --remaining-requests 8
```

`--remaining-requests` 由操作者先核实，不是查询服务端额度；客户端仅检查不少于 8 并限制本轮最多 8 次尝试。异常也计入本轮尝试，不自动重试。可通过 `--ogx-base`、`--model` 指定已授权的模型接口；本实验只用本机原服务。

每例保存实际发送的 `.request.json` 字节、`.response.bin` 响应字节和 SHA256。`manifest.json` 区分收到响应、网络异常、无效响应和未发送的 dry-run。HTTP 200、调用了工具、引用了片段，都不自动等于语义正确或防住攻击。`collection_ok` 只表示8份接口响应完整；网络异常、非200、格式错误、空回答或截断会保留原记录并以退出码2结束，不作为成功采集。dry-run明确标注未发送，不给语义评分。

## 程序检查

采集器 12 项离线测试使用本目录依赖，再安装测试锁：

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-test.lock
.venv/bin/python -m pytest tests -q
```

原应用执行器的 3 项检查需要第六篇的完整应用依赖。使用按第六篇两份锁准备的**独立测试环境**运行，不往 OGX/vLLM 环境中补包：

```bash
/path/to/product-test-venv/bin/python -m pytest program_checks -q
```

默认定位同仓库 `rag-observability-lab`，可用 `REVIEW_APP_SOURCE` 显式指定。分别保留程序结果与模型原文，见 [TESTING.md](TESTING.md)。

## 边界与来源

模型案例没有实际上传恶意文件，也没有让模型自主选择是否检索；它们观察的是已接触工具内容后的单次续答。确认、修改与提交的实际协议约束继续见第六篇应用与测试。本目录不承诺覆盖所有提示注入，不包含护栏分类模型。

设计参考 [Red Hat Lemonade Stand Assistant](https://github.com/rh-ai-quickstart/lemonade-stand-assistant) 的分层检查思路，未部署其 OpenShift AI / TrustyAI 检测栈。代码采用 Apache-2.0；设备示例参考前文固定的 Red Hat IT Self Service Agent 数据，保留 [MIT 来源声明](LICENSE-UPSTREAM)。合成材料不含真实员工资料、凭据或业务数据。
