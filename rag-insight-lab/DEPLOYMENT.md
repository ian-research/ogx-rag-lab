# 部署与配置

在已经完成第六篇的 RHEL 10 x86_64 服务器上，以 root Bash 执行。本次环境是 RHEL 10.2 / Python 3.12.15 / 单张 L40S；不修改原驱动、OGX、vLLM 或模型环境。

## 1. 前置服务

| 前置 | 检查方法 |
|---|---|
| 8094 业务 API | 健康接口可用，并实际完成一次查询 |
| 8094 `/metrics` | 有 `actionlab_http_requests_total` 计数指标 |
| Jaeger 2.23.0 | 16686 可见 `action-lab-api` Trace，原生 MCP 已启用 |
| OGX 8321 | 可以调用 `local-vllm/rag-llm` |
| Podman、Python 3.12、curl、iproute、systemd | 沿用前置环境；Python 依赖只装进本目录虚拟环境 |

```bash
curl --fail-with-body -sS http://127.0.0.1:8094/api/health
curl --fail-with-body -sS http://127.0.0.1:8094/metrics
curl --fail-with-body -sS http://127.0.0.1:8321/v1/models
```

Jaeger MCP 地址必须是 `http://127.0.0.1:16686/api/ai/mcp/`，不是另起一个模型服务。MCP 协议调用由本篇客户端完成；普通 GET 返回的状态不能代替 MCP 初始化与工具调用验证。

## 2. 获取源码与准备环境

```bash
git clone https://github.com/ian-research/ogx-rag-lab.git /root/rag-lab/ogx-rag-lab
cd /root/rag-lab/ogx-rag-lab/rag-insight-lab
git rev-parse HEAD

export STATE_DIR=/root/rag-lab/article07-state
PYTHON_BIN=python3.12 bash scripts/prepare.sh
```

已有仓库先保存修改，再更新并记录使用的 commit。本目录不要求固定在 `/root/rag-lab/rag-insight-lab`；安装器会记录实际源码绝对路径，部署以后不要移动它。

准备脚本创建 `.venv`，按 `requirements.lock` 和哈希安装依赖，执行 `pip check`，初始化状态目录。不要在 OGX/vLLM 虚拟环境中补装这些依赖。

首次初始化生成：

- `$STATE_DIR/private/admins.json`：只存运维凭据的哈希。
- `$STATE_DIR/private/credentials-for-delivery.json`：待单独分发的凭据，权限0600。

两文件不进入 Git 或源码压缩包。准备脚本遇到已有 `admins.json` 会保留，不重新生成。需要重建凭据时先自行备份和安排分发，不在正在使用的目录反复执行初始化器。

## 3. 固定 Prometheus 镜像并安装

```bash
podman pull docker.io/prom/prometheus:v3.5.0
podman image exists sha256:a3bc50fcb50fb1409287c32773d8fe799f6cad5511c4ba3e2e851784e7a05026
bash scripts/install07.sh
```

安装器默认使用本次验证的 x86_64 镜像 ID，镜像不匹配会在写配置或停服务之前失败。若仓库标签已变化，先确认平台与目标镜像，再按核实后的 ID 显式设置 `PROM_IMAGE`；不要跳过预检。

首次安装生成两个单元：

| 单元 | 用途 |
|---|---|
| `article07-api.service` | 虚拟环境的 `python -m uvicorn`，只监听127.0.0.1:8096 |
| `article07-prometheus.service` | Podman 托管 Prometheus，只监听127.0.0.1:9090 |

单元设为开机启用、异常退出重启。Prometheus 每15秒抓取8094，数据卷在 `$STATE_DIR/prometheus`，指标保留一天。不会重启已有 Jaeger、OGX、vLLM 或第六篇业务。

## 4. 持久配置

`/etc/rag-lab/article07-runtime.env` 供 API 的 systemd 单元加载，初次由模板生成；源码目录没有需要读者填写的明文密码文件。

| 配置项 | 默认值/含义 |
|---|---|
| `STATE_DIR` | `/root/rag-lab/article07-state` |
| `PRIVATE_DIR` / `EVIDENCE_DIR` / `RUN_DIR` | 状态目录下的身份、冻结证据、解释记录 |
| `RAW_DIR` | 可选，默认 `$STATE_DIR/evidence-raw` |
| `JAEGER_MCP_URL` | `http://127.0.0.1:16686/api/ai/mcp/` |
| `PROM_URL` | `http://127.0.0.1:9090` |
| `OGX_BASE` | `http://127.0.0.1:8321`，不要额外加 `/v1` |
| `OGX_MODEL` | `local-vllm/rag-llm` |
| `ALLOWED_SERVICES` | `action-lab-api,action-lab-mcp` |
| `COUNTER_METRIC` / `COUNTER_SERVICE` | `actionlab_http_requests_total` / `action-lab-api` |
| `TOOL_TIMEOUT` / `MODEL_TIMEOUT` | 30秒 / 120秒 |
| `REQUEST_BUDGET` / `MAX_CONCURRENCY` | 可选，整体90秒 / 每进程最多2个分析；整体预算可以先于模型超时生效 |
| `MAX_EVIDENCE_BYTES` | 24,000，模型事实摘要的字节预算，不是完整请求大小 |
| `MAX_TOOL_BYTES` | 120,000，Jaeger 单次工具输出预算 |

修改 API 配置后重启 `article07-api`。现有 runtime.env 不会因重新安装自动覆盖。`JAEGER_QUERY_URL`、`DEFAULT_WINDOW_MINUTES`、`LOG_LEVEL` 为模板保留项，不用它们改变 MCP 路径或网页选择；请求中的窗口及工具地址以实际客户端配置为准。

端口变更需要同步调整对应单元或用安装器重新渲染；仅改 env 里的 `API_PORT` 不会替换现有单元的启动参数。Prometheus 端口变更也要同步 `PROM_URL`。本实验保持默认端口。

## 5. 验证与访问

```bash
systemctl is-enabled article07-api article07-prometheus
systemctl is-active article07-api article07-prometheus
curl --fail-with-body -sS http://127.0.0.1:8096/api/health
curl --fail-with-body -sS http://127.0.0.1:9090/-/ready
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8096/
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8096/api/whoami
```

两个单元应是 `enabled`、`active`；健康接口 `status=ok`；页面200；无凭据whoami401。健康接口不检查全部后端。

沿用原来的 SSH 隧道，将本机端口转发到服务器8096。浏览器打开转发后的页面，输入管理员单独分发的凭据。Prometheus、Jaeger 等保持回环监听；不要为此打开公网端口。

随后在8094问设备问题，等它落入冻结时间窗口，再在8096选择 `latency_breakdown` 分析，检查阶段行和证据。指标比较需积累两窗采样，Trace 与指标都不是启动后凭空补出来的历史。

## 6. 已有安装的替换与卸载

默认遇同名服务/配置会拒绝覆盖。确认是本篇安装且已备份状态后：

```bash
export STATE_DIR=/root/rag-lab/article07-state
ARTICLE07_REPLACE=1 bash scripts/install07.sh
```

替换路径备份单元、runtime.env、Prometheus配置及原active/enabled状态，只停止本篇两个服务。后续失败时恢复备份单元、Prometheus配置和原服务状态；原runtime.env保持沿用。备份位于状态目录的 `install-backup-*`。这不是全栈业务数据备份。

```bash
# 卸载本篇两个单元，保留配置、指标、凭据与证据
bash scripts/rollback.sh

# 另删除本篇 runtime.env；状态数据仍然保留
bash scripts/rollback.sh --purge
```

保留配置后再次安装仍须显式选择替换/沿用；`--purge` 不删除状态目录，不影响前六篇服务。

## 7. 常见响应

| 状态 | 先检查 |
|---|---|
| 401 / 403 | 凭据、ops角色、服务白名单或Trace所属时间范围 |
| 404 | 窗口无可用Trace、Jaeger内存已清空，或证据编号不存在 |
| 429 | 本进程分析并发已满，稍后重试 |
| 502 | 模型接口不可用或返回非200 |
| 503 | Jaeger / Prometheus 后端查询不可用 |
| 504 | 整体分析预算超时 |
| HTTP200且 `empty_answer/truncated` | 查看状态与原始模型响应，不当作完整解释 |

详细日志：`journalctl -u article07-api -n 80 --no-pager` 与 `journalctl -u article07-prometheus -n 80 --no-pager`。

本轮已验证服务部署、配置与程序测试，没有做整机重启或生产压测。Jaeger内存存储重启会丢Trace；本篇不会将其升级为持久存储。
