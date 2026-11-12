# 测试与验证范围

本目录的运行文件来自服务器实测 r4。对应关系记录在 [PROVENANCE.json](PROVENANCE.json)，公开文件的校验清单为 [BUILD-MANIFEST.json](BUILD-MANIFEST.json)。这不是模型回答正确率报告。

## 本地程序测试

使用独立的 Python 3.12/3.13 环境，不需 GPU、OGX 或远端服务器：

```bash
python3.12 -m venv /tmp/article05-tests-venv
/tmp/article05-tests-venv/bin/pip install --require-hashes -r requirements-test.lock
/tmp/article05-tests-venv/bin/python -m pytest -q
```

预期 58 项通过。包含草稿/确认/身份绑定、事务授权、实际字段校验、并发/幂等、未决恢复、旧记录保护，以及真实本地 HTTP MCP 协议测试。协议测试启动本地临时端口，运行环境必须允许本机端口监听。测试用临时状态，不要把正式凭据或数据库配置带入测试环境。

## 页面手动检查

按 [DEPLOYMENT.md](DEPLOYMENT.md) 启动隔离实验服务，在页面检查：

| 操作 | 应核对的结果 |
|---|---|
| 只问规定 | 不创建工单 |
| 准备草稿但不确认 | 可以预览设备、理由、来源与缺项，没有工单 |
| 修改理由并重新预览 | 草稿版本变化；落单理由与当前版本一致 |
| 确认提交 | 一张 `pending_review` 模拟申请，不表示资格审核通过 |
| 按原提交标识回查 | 返回同一张工单，理由和缺项保持一致 |
| 取消另一份未提交草稿 | 该草稿没有工单，不能继续提交 |

页面再次点“确认提交”先请求 `/confirm`；已提交草稿在此返回 409，不会到达 `/submit`。这可以观察重复页面操作被拦住，但不能代替提交接口的幂等测试。页面可能显示“未完成”，用原标识回查已有工单，勿据此新建草稿重试。

2026-11-12 作者实操中：A 草稿取回第十七条，有实际使用年限、维修成本两处缺项；另一份 B 草稿未取回条款，有三处缺项。B 修改为 v2 后确认，工单 `RT-20261112-0041` 为待审核，保留用户原话和全部缺项。按同一草稿检查，只有一张单。取消的另一个草稿没有工单。

## 在线联调批次

在线脚本会创建模拟工单、暂停/恢复 MCP 并重启实验服务，只在独立实验环境运行。完整批次还需要已发布的注入实验文档；它可能多次调用模型，所以不能预先保证每次取回相同来源。

在服务器 root Bash 进入本目录，沿用部署时同一 `STATE_DIR`。如持久配置有自定义值，先加载该配置，再运行脚本：

```bash
set -a
source /etc/rag-lab/article05-runtime.env
set +a
RUN_DIR="$STATE_DIR/evidence/check-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python scripts/cases.py --out "$RUN_DIR"
.venv/bin/python scripts/fieldcheck.py
```

`cases.py` 写出各项原始请求/响应、运行编号和机器判定。请继续人工检查模型理由与来源。`fieldcheck.py` 只读检查当前库中的字段关联；不同历史数据可能影响输出，按原草稿和提交标识核对。

服务器 r4 批次 `20261112T032002Z`：27 项在线用例机器判定通过，58 项程序测试通过。在线用例的 `semantic_review=pending` 保留原意；程序计数不能写成模型语义正确率。针对作者手动场景另有人工作答与只读字段核对，不能替代所有模型输出评审。

## 中断恢复演练

`scripts/fault_recovery.py` 启动临时 API/MCP 和独立状态，在三个边界使 API 进程真实退出，再恢复：授权已保存、请求已标记发出、后台已建单但应用尚未收尾。脚本需要已有 OGX/文档服务及依赖环境。

只使用新建的专用临时目录。脚本会清理其阶段子目录，绝不能把真实 `STATE_DIR` 或已有工作目录传给它：

```bash
DRILL_DIR=$(mktemp -d /tmp/article05-fault.XXXXXX)
.venv/bin/python scripts/fault_recovery.py \
  --workdir "$DRILL_DIR/state-stages" \
  --evidence "$DRILL_DIR/evidence"
```

服务器已实测三个边界。请求已发出但结果未定时，未到 120 秒租约会返回处理中；之后回查或重放原已授权请求，每个测试草稿最终一张单。这里只覆盖本地模拟后台的约束与事务；接真实工单系统后必须重新检查其创建、幂等和回查语义。

`scripts/legacy_recovery.py` 保留历史 r2 → r4 演练，需要服务器封存的 `/root/rag-lab/rag-action-lab-r2`，并同样清理自己的测试状态。它不是新克隆仓库即可直接执行的通用测试；没有旧封存版本时，用程序测试中的旧记录样例检查保护逻辑。`state_migration_check.py` 和 `regression_prev.py` 也是既有服务/目录的现场检查工具，先阅读路径再使用。

## 已验证与未验证

- 已验证：页面人工确认、取消、本人范围；程序级并发与幂等；真实进程中断恢复；既有服务切换、配置保留、状态迁移和旧记录冻结。
- 尚未验证：真实 ServiceNow/Zammad、企业 SSO、多用户生产负载、整套环境冷启动，以及本篇两项新增服务的整机重启。
- `systemd enable` 已配置开机启动，不将其等同于本轮做过整机重启。

程序和页面的已知限制见 [README.md](README.md)。运行记录、会话凭据与数据库均含演示身份及完整操作信息，留在私有状态目录，不提交到 GitHub。
