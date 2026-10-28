# OGX RAG Lab

在 AWS RHEL 10 与单张 L40S 上搭建标准 RAG：vLLM 提供本地生成，OGX 接入 CPU Embedding 和 pgvector，自建上传应用完成 PDF、DOCX、Markdown、TXT 的后台解析、分块、入库及带来源问答。

这是单用户实验，服务只监听本机，通过 SSH 隧道访问；尚无登录、文档 ACL 和并发压测。第二阶段新增显式制度发布、有效期与适用范围管理。

![部署架构](figures/architecture.png)

## 从这里开始

- [第二篇：制度版本管理](rag-version-lab/README.md)：独立 8089 服务与 Web 管理页面，先按范围和日期选版，再通过 OGX 检索问答。

- [基础安装与服务化部署](服务化部署.md)：原初始化脚本补正、依赖等待和重启自启动。

- [完整实操文章](article-integrated.md)：架构、部署、配置、脚本与验收。
- [部署复现手册](部署复现手册.md)：逐层安装和排障。
- [验收记录与已知限制](验收记录与已知限制.md)：已验证能力和未验证项。
- [脚本与配置说明](脚本与配置说明.md)：代码入口和参数。
- [上传应用说明](rag-upload-lab/README.md)：文件格式、接口及分块行为。

## 获取代码

```bash
git clone https://github.com/ian-research/ogx-rag-lab.git
cd ogx-rag-lab
git rev-parse HEAD
```

记录 commit，后续实验使用同一版本。服务器部署路径沿用 `/root/rag-lab`，详见手册。不要在开发电脑执行服务器安装脚本。

|目录|内容|
|---|---|
|rag-version-lab/|制度版本 API、后台 Worker、发布规则、独立数据及 53 项测试|
|rag-upload-lab/|API、网页、CPU Worker、解析分块、示例和测试|
|configs/|OGX 参考配置，密码通过环境变量提供|
|scripts/|vLLM / OGX 启动、健康检查和版本采集|
|figures/|架构与任务状态图|

## 实验基线

2026-10-27 记录：RHEL 10、L40S、vLLM 0.32.0、OGX 1.5.0、PostgreSQL 16.15、pgvector 0.8.7；CPU Embedding 为 bge-small-zh-v1.5（512 维）。版本号描述实验，不代表最新版本。

仓库不含模型权重、数据库、真实业务文档、凭据或完整远端依赖锁。模型 revision 已由 DSH 复现记录补齐。全栈服务已完成服务器重启验证；第二阶段 Markdown 联调及 53 项自动化测试通过。Word Title 修复通过本地测试，远端重新入库仍需验收。

## 测试

按上传应用说明安装独立环境后，在 `rag-upload-lab` 目录执行：

```bash
/root/rag-lab/.venv-ingest/bin/python -m pip install pytest
/root/rag-lab/.venv-ingest/bin/python -m pytest tests -q
```

测试使用模拟 OGX，不代替实际模型、数据库和 OCR 联调。

## 许可证与来源

代码与自建文档采用 [Apache License 2.0](LICENSE)。依赖软件、模型及引用的上游项目适用各自许可证；模型不随仓库分发。

参考 [Red Hat AI Quickstarts RAG](https://github.com/rh-ai-quickstart/RAG)，并非其 Compose 原样部署；本仓库不含 OGX 或上游 RAG 的源码副本。
