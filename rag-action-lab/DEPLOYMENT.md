# 部署与配置

以下 systemd 安装命令在实验服务器的 root Bash 中执行。新增应用使用独立虚拟环境；已有 OGX/vLLM、文档服务和第四篇服务继续运行。

## 1. 前置条件

实测基线为 RHEL 10.2、Python 3.12.15、单张 L40S、Qwen3.5-9B、vLLM 0.32.0、OGX 1.5.0。新应用和模拟工单使用 CPU，不需要额外 GPU。Python 检查允许现有 3.12 或 3.13；运行依赖由本目录锁文件固定。

基础环境尚未搭建时，先阅读仓库的[基础安装与服务化部署](../服务化部署.md)、[第二篇文档服务](../rag-version-lab/README.md)和[第四篇工具调用配置](../rag-mcp-lab/DEPLOYMENT.md)。初始化脚本参考 [rhel10_vllm_env_install.sh](https://github.com/ian-research/vllm_test/blob/main/rhel10_vllm_env_install.sh)，安装前核对当前适用的驱动和依赖版本。

| 既有组件 | 本次配置 |
|---|---|
| OGX 根地址 | `http://127.0.0.1:8321`，本程序自动拼接 `/v1` |
| OGX 生成模型 | `local-vllm/rag-llm` |
| 文档版本与检索 | `http://127.0.0.1:8089` |
| 新应用与新 MCP | `127.0.0.1:8092` / `127.0.0.1:8093` |

先验证现有模型可返回结构化工具调用。第四篇实测模型使用的 vLLM 参数片段为 `--enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3`；沿用已验证的模型服务配置，不为安装本目录升级推理栈。

## 2. 准备实验文档

在 8089 管理页面上传、入库并发布以下资料。不要重复上传已存在的同版本；`ready` 后仍需要发布。

| 原文件 | 文档身份 | 版本 / 范围 | 生效日 |
|---|---|---|---|
| [eval-device.md](../rag-eval-lab/dataset/docs/eval-device.md) | `eval3-device` | `v1 / all` | `2026-01-01` |
| [eval4-inject.md](../rag-mcp-lab/docs/eval4-inject.md)，仅注入实验需要 | `eval4-inject` | `v1 / all` | `2026-01-01` |

默认文档适用日为 `2026-03-01`。允许的文档标识目前仅这两项；模型并不搜索任意知识库。第二项是刻意夹带指令的合成材料，只放隔离实验库。全量 27 用例需要它；普通页面体验只需设备文档。

## 3. 获取代码与创建环境

首次获取仓库：

```bash
git clone https://github.com/ian-research/ogx-rag-lab.git \
  /root/rag-lab/ogx-rag-lab
cd /root/rag-lab/ogx-rag-lab/rag-action-lab
git rev-parse HEAD
export STATE_DIR=/root/rag-lab/rag-action-state
PYTHON_BIN=python3.12 bash scripts/prepare.sh
```

已有仓库时先保存本地修改，再更新并进入 `rag-action-lab`，不必重复克隆。记录实际 commit，后续按该版本复现。

脚本在代码目录创建 `.venv`，按 `requirements.lock` 校验哈希安装。状态目录独立保存 `private/`、`data/action.db` 和 `evidence/runs/`，已有凭据会复用。不要将状态目录设成某次代码版本目录，也不要复制整个旧代码目录充当状态迁移。

上面的 `export STATE_DIR` 也供后面的命令行脚本使用。代码位于仓库子目录时，省略该变量可能使 CLI 查找另一处默认状态目录；systemd 则读取自己的持久配置。

## 4. 核对配置并首次安装

首次安装前核对 `deploy/runtime.env.in`。`@STATE_DIR@` 由脚本替换，模板默认如下：

```dotenv
OGX_BASE=http://127.0.0.1:8321
OGX_MODEL=local-vllm/rag-llm
DOC_SERVICE=http://127.0.0.1:8089
ACTION_MCP_URL=http://127.0.0.1:8093/mcp
PUBLIC_BASE=http://127.0.0.1:8092
STATE_DIR=@STATE_DIR@
PRIVATE_DIR=@STATE_DIR@/private
DB_PATH=@STATE_DIR@/data/action.db
DEFAULT_POLICY=eval3-device
DEFAULT_AS_OF=2026-03-01
```

端口在 systemd 模板中固定为 8092/8093；若自行改端口，需同时改对应模板、连接地址和健康检查，不能只改 `API_PORT` 环境变量。

```bash
bash scripts/install.sh
systemctl is-active article05-action-api article05-action-mcp
systemctl is-enabled article05-action-api article05-action-mcp
curl --fail-with-body -sS http://127.0.0.1:8092/api/health
curl --fail-with-body -sS http://127.0.0.1:8093/health
```

脚本渲染 unit 与持久配置，检查后安装并 `enable --now`。持久配置位于 `/etc/rag-lab/article05-runtime.env`；私有文件为 0600、目录为 0700，配置为 0640。后续只改持久配置再重启两项服务，修改模板不会自动更新已安装配置。

默认会话和服务密钥在 `$STATE_DIR/private/`。管理员从 `credentials-for-delivery.json` 单独取出对应账号的凭据；不要把完整文件给任一用户，也不要复制到公开代码。`action.key` 是服务间证明密钥，不是页面凭据。

## 5. 页面使用

在个人电脑建立已有 SSH 连接的隧道：

```bash
ssh -N -L 18092:127.0.0.1:8092 <已有服务器SSH连接目标>
```

浏览器打开 `http://127.0.0.1:18092/`，输入单独分发的演示凭据并连接。

1. 输入「我的笔记本电池鼓包，想申请更换，帮我准备申请」，文档日期保持 `2026-03-01`。
2. 核对设备、理由、来源和缺项。改理由时使用草稿下方输入框，点“保存并重新预览”。在问题框重新查询会开始另一份请求。
3. 确认这份草稿后提交。页面会依次请求 `/confirm` 和 `/submit`，结果应为待审核。
4. 根据返回的原标识回查，记录同一单号。未检索到来源时可以形成带缺项的待审核申请；这属于本模拟受理规则，不代表设备符合换新条件。
5. 另建草稿后取消，核对没有新增对应草稿的工单。演示账号已有历史单时按草稿/单号检查，不比较全库总数。

页面确认 10 分钟、草稿 15 分钟、提交超时 20 秒、恢复租约 120 秒均为实验设置。页面重复点确认可能显示“未完成／已提交且冻结”，这是确认接口拦截；回查可显示原单。

## 6. 切换、回退与状态保留

已有同名服务时，首次安装脚本会拒绝覆盖。进入准备好的新代码目录，沿用原状态目录再切换：

```bash
export STATE_DIR=/root/rag-lab/rag-action-state
bash scripts/prepare.sh
bash scripts/switch-services.sh --switch
```

脚本保留状态路径，备份 unit/配置并输出 `private/units-before-<UTC时间>/`。默认保留既有自定义配置；`--keep-config` 则保留原配置文件原样。需要回退时，用实际备份的绝对路径执行：

```bash
bash scripts/rollback.sh /实际新代码目录/private/units-before-实际时间
```

若此前版本把状态放在旧代码目录，先停第五篇两项服务，在保留原副本的前提下使用 `scripts/migrate_state.py <旧状态根目录> <新的独立状态根目录>`，检查完整性、逐表计数及密钥后再切换。目标库必须不存在。迁移脚本使用 SQLite backup API 包含 WAL；停写期间进行最终切换，避免迁移后的旧库继续接收新记录。

旧未决记录缺授权时保持冻结，等待可信回查或人工核对；不要为了恢复按钮清空数据库或签名密钥。脚本回退针对本实验的两项已运行服务；特殊的 disabled/inactive 部署状态应先审阅脚本，不宣称任意自定义状态均已覆盖。

## 7. 测试与检查

见 [TESTING.md](TESTING.md)。健康接口只证明进程/数据库可用，模型、文档和实际受理还要用页面或联调脚本验证。

本轮有服务重启、进程中断恢复与旧状态升级的实测；没有重新安装整套操作系统，也没有执行新增服务的整机重启或企业真实工单接入验证。
