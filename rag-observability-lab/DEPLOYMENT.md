# 第六篇部署与配置

本篇在已有模型和文档环境上增加独立副本。以下命令在 **RHEL 10 x86_64 服务器的 root Bash** 中执行，Python 为 3.12，已安装 Podman、systemd。使用 Enforcing 的既有 SELinux 配置，不需要修改驱动或 OGX/vLLM 环境。

文档服务 `127.0.0.1:8089` 与 OGX `127.0.0.1:8321` 应可用；设备文档 `eval3-device` 已发布，查询日期 `2026-03-01`，生成模型别名 `local-vllm/rag-llm`。

## 1 进入源码并准备独立环境

从仓库取得本目录并记录使用的 commit。服务器示例位置：

```bash
cd /root/rag-lab/ogx-rag-lab/rag-observability-lab
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-otel.lock
.venv/bin/pip check
```

只装本篇锁，不在共享 OGX/vLLM 环境安装。完整说明也保留在 [DEPLOYMENT.txt](DEPLOYMENT.txt)。

## 2 准备镜像和状态

```bash
podman pull docker.io/jaegertracing/jaeger:2.23.0
podman image inspect docker.io/jaegertracing/jaeger:2.23.0 --format '{{.Id}}'

export STATE_DIR=/root/rag-lab/article06-state
install -d -m 700 "$STATE_DIR" "$STATE_DIR/private" "$STATE_DIR/data"
PRIVATE_DIR="$STATE_DIR/private" .venv/bin/python init_private.py
```

镜像按本次 x86_64 实测 ID 固定：`d8dac2a2526477f12f21f193e656347afb88ea58ff76a95f9b1c7fbb8966dbf1`。安装器检查本地该镜像存在，并在 unit 中使用 ID；标签变化不能改变服务实际启动的镜像。

`init_private.py` 只用于首次初始化空目录，遇到已有内容拒绝覆盖。生成的 `sessions.json`、`action.key`、`credentials-for-delivery.json` 为私有文件；管理员单独分发每人自己的实验凭据。升级时保留同一状态目录和数据库，不重新生成凭据。

## 3 安装并确认服务可用

确保 8094、8095、16686、4317、4318 未被其他进程占用：

```bash
bash scripts/selftest_install06.sh
bash scripts/install06.sh
systemctl is-enabled article06-api article06-mcp article06-jaeger
systemctl is-active article06-api article06-mcp article06-jaeger
curl --fail-with-body -sS http://127.0.0.1:8094/api/health
curl --fail-with-body -sS http://127.0.0.1:8095/health
```

三个服务应 enabled 且 active。API 健康响应包含 `status: ok`、`db: ok`，MCP 健康接口正常后，再在页面执行一次查询。

通过既有 SSH 隧道访问服务器 8094 和 16686，页面连接自己的实验凭据。准备一次草稿后，在 Jaeger 查询 `action-lab-api`；提交链路还可看到 `action-lab-mcp`。服务只监听本机，不需要开放公网端口。

## 4 持久配置

安装器渲染 `/etc/rag-lab/article06-runtime.env`（0640，SELinux `etc_t`）并生成三个 `article06-*` unit。模板为 `deploy/runtime06.env.in`：

```dotenv
OGX_BASE=http://127.0.0.1:8321
OGX_MODEL=local-vllm/rag-llm
DOC_SERVICE=http://127.0.0.1:8089
ACTION_MCP_URL=http://127.0.0.1:8095/mcp
PUBLIC_BASE=http://127.0.0.1:8094
OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_SAMPLE_RATIO=1.0
```

修改采集开关或连接配置后，重启本篇 API/MCP 对应进程使其生效。`OTEL_ENABLED=0` 在新进程中跳过 SDK/exporter 配置，保留 no-op 追踪调用。

状态在代码目录之外：`/root/rag-lab/article06-state`。新服务的自启动设置已检查，整机重启尚未实测。Jaeger 的 systemd 托管只负责服务自启，不使内存 Trace 持久化。

## 5 替换与回退

默认发现同名 unit 就拒绝覆盖。若明确升级本篇实例，使用：

```bash
ARTICLE06_REPLACE=1 bash scripts/install06.sh
```

替换模式先备份 unit、配置与 enabled/active 状态到 `STATE_DIR/install-backup-*`，只停止本篇服务，再查端口和安装；失败尝试按备份恢复。已有配置只有键完整且 `STATE_DIR` 一致时才采用，不会自动追加或改写。

隔离命令桩自检覆盖默认拒绝、替换失败后文件和服务状态恢复等场景。服务器真实替换失败的完整 before/after 原始记录未留存，不能把自检等同于所有真实故障条件均已验证。

停止并移除本篇服务、保留配置和状态：

```bash
bash scripts/rollback06.sh
```

重新安装会采用保留的完整配置。`--purge` 另移除本篇配置文件，仍不删除状态目录；仅在确实需要清除本篇配置时使用。

测试、故障注入和两个不调模型的补验入口见 [TESTING.md](TESTING.md)。
