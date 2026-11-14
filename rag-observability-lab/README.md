# 第六篇 追踪问答和申请流程

在第五篇确认后提交换新申请的副本上，增加 OpenTelemetry 追踪、Jaeger、JSON 关联日志与 Prometheus 格式的指标端点。模型仍由原来的 OGX/vLLM 提供，不增加 GPU。

这是独立的单机实验：新应用与模拟工单使用自己的状态目录；不覆盖前几篇服务，不接真实工单或审批系统。

| 端口 | 服务 |
|---|---|
| 8094 | 应用 API 与 Web 页面 |
| 8095 | 模拟工单 MCP |
| 16686 | Jaeger 页面与查询接口 |
| 4318 / 4317 | Jaeger OTLP HTTP / gRPC 接收 |

全部只监听 `127.0.0.1`。应用实际通过 OTLP HTTP 导出 Trace。

## 部署和测试

- [完整部署步骤](DEPLOYMENT.md)：RHEL 10 / Python 3.12，独立运行环境、状态初始化、systemd、健康检查、替换与回退。
- [测试与证据边界](TESTING.md)：74 项程序测试，真实模型批次与两项不调模型补验分开说明。
- [来源与版本](PROVENANCE.json)：第五篇基线、原始实验时间及发布整理的范围。
- [源码校验](BUILD-MANIFEST.json)：本目录分发文件的 SHA256，不包含运行数据。

运行环境用 `requirements-otel.lock`；独立测试环境需要同时安装 `requirements-otel.lock` 与 `requirements-test.lock`。`requirements.lock` 仅保留第五篇历史基线，不作为本篇安装入口。

## 各部分做什么

准备草稿时，设备查询和理由整理仍是应用内函数，文档查询访问已有 8089。应用通过 OGX 8321 调用 `local-vllm/rag-llm`；用户在页面确认之后，才通过新的 8095 MCP 创建或回查模拟工单。

`telemetry.py` 提供 SDK、Span 与结构化日志；FastAPI/HTTPX 与 MCP ASGI 入口采集请求链路。`metrics.py` 提供 `/metrics` 文本指标，未同时部署 Prometheus/Grafana。

不同 HTTP 请求分别创建 Trace。准备、确认和提交通过同一 `draft_id` 关联，不跨人工阅读与等待保留一个长期 Span。确认能力值不作为诊断编号写入 Trace 或结构化日志。

## 本次验证的范围

服务器 RHEL 10.2 / Python 3.12.15，OTel SDK 与 HTTP exporter 1.45.2、埋点库 0.66b2、Jaeger 2.23.0；具体版本以哈希锁和部署模板为准。

- 独立测试环境：74 passed，19 条依赖弃用警告。
- 原始真实模型批次：E1–E6、L1、M1 的 9 项脚本结果通过；完整 Trace、指标和日志已另行保留。
- 后续在线补验：两身份各读自己为 200，交叉读取为 404；改稿后复用真实旧确认返回 409且无工单。补验会创建实验草稿，不调用模型。
- 安装器的隔离契约自检通过。真实替换失败前后的完整原始日志未留存；没有据此宣称真实故障回退已完整核验，也没有进行整机重启验证。

Jaeger 使用内存后端，重启会丢已有 Trace；应用有界队列和重试不能保证完整补传。重要实验先导出 JSON。业务数据库独立保存草稿、确认和工单。

本篇只采集应用/MCP 侧及其客户端调用，尚无 OGX/vLLM 服务端 Span，不能拆出 GPU 排队、prefill 或 decode 时间。五次采集开关请求仅作功能探索，不是性能结论。仍未做全新系统首装、并发压力或真实 ITSM 接入。

## 许可证与参考

新增代码采用 [Apache-2.0](LICENSE)。上游十四条固定设备记录的 MIT 声明保留在 [LICENSE-UPSTREAM](LICENSE-UPSTREAM)。

- [Red Hat IT Self Service Agent 固定版本](https://github.com/rh-ai-quickstart/it-self-service-agent/tree/7fc221dc7991a3d51432af4202fff6dae94b8d93)
- [Red Hat OpenTelemetry 实践](https://developers.redhat.com/articles/2026/04/06/distributed-tracing-agentic-workflows-opentelemetry)
