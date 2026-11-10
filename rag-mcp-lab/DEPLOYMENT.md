# 部署与配置

本目录接续已运行的 OGX/vLLM 和第二篇文档版本服务。它只新增第四篇两个服务及独立 Python 环境。以下安装与 systemd 命令在实验服务器的 root Bash 中执行，不要在个人电脑运行安装脚本。

## 1. 前置条件

本次记录：RHEL 10.2、Python 3.12.15、单张 NVIDIA L40S、vLLM 0.32.0、OGX 1.5.0、Qwen3.5-9B；CPU Embedding 为 bge-small-zh-v1.5。这些是实测版本，不表示最新版本。新增应用不需要 GPU，不向原推理环境安装依赖。

| 既有服务 | 本次地址 |
|---|---|
| OGX 模型接口 | `http://127.0.0.1:8321/v1` |
| OGX 注册的生成模型 | `local-vllm/rag-llm` |
| 文档版本与检索服务 | `http://127.0.0.1:8089` |
| vLLM | `http://127.0.0.1:8000` |

先确认原有问答正常，并保存当前启动配置。vLLM 需能通过 OGX 返回结构化工具调用。本次模型启动命令包含下列参数片段（不是独立 shell 命令）：

```text
--enable-auto-tool-choice
--tool-call-parser qwen3_xml
--reasoning-parser qwen3
```

将参数放入你现有的 vLLM 启动配置并按既有方式重启模型服务；不同模型或 vLLM 版本应先核对解析器，不自动套用本组参数或升级整套环境。确认 `/v1/chat/completions` 返回合法的工具名、JSON 参数和调用标识后继续。

通过 8089 管理页面，按[第三篇资料准备步骤](../rag-eval-lab/README.md)上传并发布以下文档。`ready` 后仍需发布，不能只上传：

| 原文（位于 `../rag-eval-lab/dataset/docs/`） | policy_id | 版本 / 范围 | 生效日 | 替代 |
|---|---|---|---|---|
| eval-travel-v1.md | eval3-travel | v1 / all | 2026-01-01 | 无 |
| eval-travel-v2.md | eval3-travel | v2 / all | 2026-06-01 | 上一行 v1 的版本 ID |
| eval-device.md | eval3-device | v1 / all | 2026-01-01 | 无 |
| eval-security.md | eval3-security | v1 / all | 2026-01-01 | 无 |
| eval-vendor.md | eval3-vendor | v1 / dept-fin | 2026-01-01 | 无 |

本应用固定查询 `all`，因此财务部门范围的供应商文档不会因接入 MCP 而变得可见。不要重复上传已有同身份同版本的资料。新建业务文档标识需要同步调整工具允许列表与描述，默认代码不搜索任意知识库。

可选注入实验的 `docs/eval4-inject.md` 是特意夹带指令的合成资料。仅在隔离实验库中上传，登记 `policy_id=eval4-inject`、标题“差旅报销补充说明”、`version_label=v1`、`scope=all`、`valid_from=2026-01-01`，等入库完成后发布。未准备该资料时，不运行对应注入场景。

## 2. 获取代码并准备环境

首次获取仓库：

```bash
git clone https://github.com/ian-research/ogx-rag-lab.git /root/rag-lab/ogx-rag-lab
cd /root/rag-lab/ogx-rag-lab/rag-mcp-lab
git rev-parse HEAD
bash scripts/prepare.sh
```

已有仓库时先确认本地修改，再更新并进入其中的 `rag-mcp-lab`，不要重复克隆。脚本从自身目录推导部署位置；不要求使用测试服务器的历史目录名。

`prepare.sh` 默认用 `python3.12`，可通过 `PYTHON_BIN` 指定现有 Python 3.12 或 3.13。它在本目录创建 `.venv`，按 `requirements.lock` 的哈希安装依赖并检查导入，然后生成新的 `private/`。该目录为 0700，私密文件为 0600。已有 `private/` 不会被覆盖。

管理员从 `private/credentials-for-delivery.json` 单独分发相应账号的凭据：`lab-a`、`lab-b` 或无设备记录的 `lab-empty`。不要把整个文件发给任一前端用户，也不要上传到 GitHub。`private/mcp.key` 是服务间密钥，不是浏览器登录凭据。

## 3. 核对配置并首次安装

安装前修改 `deploy/runtime.env.in` 中的地址和预算。`@APP_DIR@` 路径占位符由安装脚本渲染，不用手工替换。

```dotenv
OGX_BASE=http://127.0.0.1:8321
OGX_MODEL=local-vllm/rag-llm
MCP_URL=http://127.0.0.1:8090/mcp
DOC_SERVICE=http://127.0.0.1:8089
```

`OGX_BASE` 不带 `/v1`；客户端会自行添加接口路径。默认四轮模型调用、六次工具调用、240 秒总时限；详细预算在模板中。

目标机器尚无 `article04-mcp`、`article04-orchestrator` 和本篇配置时：

```bash
bash scripts/install.sh
systemctl is-active article04-mcp article04-orchestrator
systemctl is-enabled article04-mcp article04-orchestrator
curl --fail-with-body -sS http://127.0.0.1:8090/health
curl --fail-with-body -sS http://127.0.0.1:8091/api/health
```

脚本先渲染并校验，再写入两个 unit 与 `/etc/rag-lab/article04-runtime.env`，启用服务并等待健康接口。配置为 root:root 0640，使用系统默认 SELinux 文件类型并执行 `restorecon`。失败时先停用本次创建的服务，再清理文件；若提示 `INCOMPLETE`，按输出检查残留，不能假定清理成功。

之后调整地址应编辑 `/etc/rag-lab/article04-runtime.env`，而不是只改安装模板，然后执行：

```bash
systemctl restart article04-mcp article04-orchestrator
```

两个服务只监听本机；API 健康不代表真实业务问答正确。参见下一步。

## 4. 打开页面并查询

沿用 SSH 隧道转发服务器 8091，浏览器打开本机 `http://127.0.0.1:8091/`。输入单独分配的演示凭据，确认页面显示当前账号，再提问。凭据保留在页面内存，刷新后需重新连接。

建议依次尝试：

- 我的笔记本是什么型号，什么时候买的？
- 查询工单 LAB-TK-001 当前进展，有预计完成时间吗？（使用 lab-a）
- 查我的设备并对照设备文档，能否确认符合换新条件？
- 上海出差住宿每晚最多报多少？（分别选择 2026-03-01、2026-07-01）

展开“查询依据与调用过程”，核对工具参数、返回记录和文档来源。日期只选文档版本，设备与工单没有历史快照。原始运行文件保存在配置的 `RUN_ROOT` 下，含模型和工具输入输出，应按私密实验记录管理。

批量采集正常请求：

```bash
.venv/bin/python scripts/verify.py --phase baseline --as-of 2026-03-01
```

这是 Python 文件，不能用 `bash scripts/verify.py`。更多核对和测试见 [TESTING.md](TESTING.md)。

## 5. 迁移已有安装

已有同名 unit 或配置时，首次安装入口会拒绝覆盖。将新代码放到独立目录，先在新目录执行 `prepare.sh`，准备并单独分发新凭据，然后在新目录运行：

```bash
bash scripts/switch-services.sh --switch
```

默认保留现配置的自定义连接和预算，只更新 `PRIVATE_DIR` 与 `RUN_ROOT` 到新目录。备份保存在新目录的 `private/units-before-<UTC>/`，含两个 unit、配置与原先启用/运行状态。

`--keep-config` 只适用于已经手工将持久配置的两个路径指向新目录的情况；否则在停止旧服务之前拒绝，不自动使用另一份旧路径。保留原目录和私密备份，供回退。

## 6. 回退与验证范围

在执行过切换的新目录中，用实际的备份目录名替换下方路径：

```text
bash scripts/rollback.sh private/units-before-<UTC>
```

脚本恢复本篇两个 unit、启用/运行状态及备份记录要求恢复的配置，不修改 vLLM、OGX 或文档数据库。备份标记 `config_action=left_in_place` 时，会明确说明配置保持不动。

已有 RHEL 服务器上的安装、切换、回退与服务恢复有实测记录；安装失败清理也做过隔离的真实 systemd 故障复测。未在全新操作系统重新部署全栈，未做本篇服务的整机重启、并发与压力测试。systemd 已配置开机自启动，与已完成整机重启实测是不同结论。
