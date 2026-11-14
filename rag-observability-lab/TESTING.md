# 第六篇测试与复跑

程序测试、原始真实模型实验、后续不调模型的补验分开统计。它们不表示模型回答准确率。

## 独立测试环境

在本目录、RHEL 10/Python 3.12 中执行，勿在 OGX/vLLM 的环境里补包：

```bash
python3.12 -m venv .venv-test
.venv-test/bin/pip install --require-hashes \
  -r requirements-otel.lock -r requirements-test.lock
.venv-test/bin/pip check
MCP_TEST_PORT=18095 .venv-test/bin/python -m pytest tests -q
```

只装测试锁会缺 OTel SDK/exporter。标准指标解析器 `prometheus-client` 在测试锁中，所以端到端脚本 `e06r2.py` 也用 `.venv-test` 运行。本次服务器输出为 **74 passed, 19 warnings in 5.82s**；弃用警告来自 FastAPI/TestClient 与 MCP 客户端接口。

## 原始模型实验

本篇服务与前置文档/模型服务已启动时：

```bash
.venv-test/bin/python scripts/e06r2.py
.venv/bin/python scripts/e07_collect.py
```

脚本使用本次固定实验路径 `/root/rag-lab/article06-state`、8094/8095，以及临时测试端口 18094/18096/18097；程序测试另用 18095。**只在这些专用实验实例上运行**：它会创建模拟草稿/工单，停止或重启本篇 MCP/Jaeger，不能用于共享或真实业务系统。

每轮结果写到状态目录的 `evidence/`。读取 `summary.json` 和逐项结果；脚本退出码本身不代表全部判据通过。Jaeger 为内存模式，停机前先导出重要记录。

| 原始检查 | 覆盖范围 |
|---|---|
| E1 | 正常问答完整 Trace，模型轮次、Token 与结束原因 |
| E2 | 改稿、确认、提交、工单回读和同键重复请求 |
| E3 | 临时实例的文档连接不可达，Span 分类 |
| E4a / E4b | 工单写前不可达、写后丢回包及回查去重 |
| E5 | 双身份并发的不同草稿、不同 Trace 与记录归属 |
| E6 | Jaeger 不可用时业务继续，恢复后新请求可见 |
| L1 / M1 | 日志按草稿关联、标准指标解析与变化 |

2026-11-13 的 `e06r2-20261113T110927Z` 有上述 9 项脚本结果。其 9 份完整 Trace 独立结构检查通过，指标原文标准解析为 8 个指标族、231 个样本，直方图累计与 `+Inf/count` 一致。

E7 另为 `e07-20261113T111642Z`：开/关各 5 个业务有效请求，响应 Trace ID 分别为 5/0。小样本不建立性能、加速或统计等价结论，也不把返回 ID 等同于逐条导出入库的证据。

## 后续两项补验

发布的 `e06r2.py` 已收紧 E5 的预期 owner、Trace 一致和交叉读取判据，以及 E2 旧确认必须有效且改稿后精确返回 409 的判据。这是**原始模型批次之后**的脚本修订，原始批次不据此追加已执行项。

2026-11-14 的两项上机补验不调用模型。它们通过 store 创建合法实验草稿，再走本篇 HTTP 接口，因此会增加实验草稿、确认与审计记录，不能称为纯只读检查。

root Bash 中先加载本篇持久配置，确保操作同一个状态目录：

```bash
set -a
. /etc/rag-lab/article06-runtime.env
set +a
CHECK_OUT="$STATE_DIR/evidence/live-checks" \
  .venv-test/bin/python scripts/check_isolation.py \
  --credentials "$PRIVATE_DIR/credentials-for-delivery.json"
CHECK_OUT="$STATE_DIR/evidence/live-checks" \
  .venv-test/bin/python scripts/check_confirmation.py \
  --credentials "$PRIVATE_DIR/credentials-for-delivery.json"
```

实测结果：

- 两身份先各读自己：200/200；再交叉读取：404/404，排除缺失或过期草稿造成的假阳性。
- 确认200、改稿200且版本1到2、使用真实旧确认提交精确409，该草稿没有工单。空串导致的422不算旧确认失效证据。

## 安装器与打包检查

```bash
bash scripts/selftest_install06.sh
python3.12 scripts/manifest.py
bash scripts/package06.sh /tmp/article06-source.tar.gz
```

安装自检使用临时目录与命令桩，不接触运行服务。它不替代真实 OS 故障或整机重启验证。源码包只含白名单文件，排除私有状态、证据、虚拟环境与生成物；不打包实验凭据或数据库。

本目录公开源码、测试与配置；原始业务数据、实验日志、Trace 和凭据不随代码分发。服务器只读实装元数据可用 `scripts/server_manifest.py` 重新采集，它不会读取或哈希私有凭据和业务数据库。
