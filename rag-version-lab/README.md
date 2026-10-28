# 制度版本管理：部署与配置

2026-10-28：本地与 RHEL 服务器各通过 53 项自动化测试；服务器完成 Markdown 上传、入库、发布和按日期问答。PDF、DOCX、扫描件未在本阶段复测，备份恢复和并发压测未验证。

## 更新已有服务器

先将此目录完整传入服务器独立暂存目录 `/root/rag-lab/phase2-fix-staging`，勿直接覆盖运行中的 data。保留现有 `.venv-ingest`、第一阶段服务、模型和密码。

```bash
cd /root/rag-lab/phase2-fix-staging
/root/rag-lab/.venv-ingest/bin/python -m pytest tests -q
python3 check_existing.py /root/rag-lab/rag-version-lab/data/version.db
```

测试需要 pytest；若环境尚未安装，先用对应虚拟环境的 pip 安装。历史检查发现日期或重叠问题时停止迁移，人工核对，不自动改写制度。

确认无在途任务，停止第二阶段并备份；第一阶段继续运行：

```bash
systemctl stop rag-version.target rag-version-api rag-version-worker
backup_dir=/root/rag-lab/backups/phase2-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup_dir"
cp -a /root/rag-lab/rag-version-lab "$backup_dir/"
cp -a /etc/systemd/system/rag-version* "$backup_dir/"
```

停止后完整复制 data 包含可能存在的 WAL 文件。回退使用这份停机快照，不只复制一个 SQLite 主文件。

只替换代码和 unit，不覆盖数据：

```bash
cd /root/rag-lab/phase2-fix-staging
cp index.html ui.js version_app.py version_rules.py version_store.py version_worker.py wait_ready.py check_existing.py README.md /root/rag-lab/rag-version-lab/
cp -a tests /root/rag-lab/rag-version-lab/
cp deploy/rag-version* /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/rag-version.target
systemctl daemon-reload
systemctl enable --now rag-version.target
curl --fail-with-body -sS http://127.0.0.1:8089/api/health
```

首次启动只新增 worker_status、ingestion_attempts 两张表，不改 versions 历史数据。检查 api=ok、worker_alive=true；再用下方样例查询旧文档并进行新上传。服务使用8089就绪检查，等待API和Worker心跳。

回退：停止第二阶段，将备份中的代码、data 和 unit 一起恢复，daemon-reload 后重新启动；修复版运行后新增的数据需另外保留，不能覆盖丢失。此次不自动删除任何向量库。

## 新环境

先按第一阶段手册准备 `.venv-ingest`、`/root/rag-lab/rag-upload-lab` 解析模块、OGX和模型。在 `/root/rag-lab/rag-version-lab` 放置本目录代码；安装 deploy/ 下三个 unit，执行 daemon-reload、enable --now rag-version.target。没有数据库时启动会自动创建独立 SQLite。不要重复初始化第一阶段数据库。

首次安装（仓库位于 `/root/rag-lab/ogx-rag-lab`，第一阶段已按根目录[服务化部署说明](../服务化部署.md)完成）：

```bash
cd /root/rag-lab/ogx-rag-lab
test ! -e /root/rag-lab/rag-version-lab && cp -a rag-version-lab /root/rag-lab/
cd /root/rag-lab/rag-version-lab
cp deploy/rag-version-api.service deploy/rag-version-worker.service deploy/rag-version.target /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rag-version.target
curl --fail-with-body -sS http://127.0.0.1:8089/api/health
```

已有同名目录请走上方更新流程，不覆盖 `data/`。新增应用复用第一阶段虚拟环境中的依赖；不安装额外模型。单测另需 pytest。

样例原件可创建为：

```bash
printf '%s\n' '员工电脑使用满4年可以申请换新。' > it-policy-v1.md
printf '%s\n' '员工电脑使用满3年可以申请换新。' > it-policy-v2.md
```

## 导入隔离与冻结片段校验（2026-10-28 第二轮）

### 导入隔离

`version_worker.py` / `version_app.py` 挂第一阶段模块目录时用 `sys.path.append`，
不再用 `insert(0)`。理由：脚本自身目录（`sys.path[0]`）必须优先，
否则在已安装第一阶段的主机上，真实 `ogx_client` 会遮蔽测试夹具里的桩。

可用 `RAG_PHASE1_DIR` 指向其他目录，便于隔离测试。

回归测试 `test_worker_shutdown_requeues_and_restart_finishes` 会在临时目录放一个
「毒桩」`ogx_client.py`（导入即抛错）并把 `RAG_PHASE1_DIR` 指过去：
只要实现退回 `insert(0)`，worker 就会导入毒桩并失败，测试随之失败。
夹具里的桩同时把 `socket.socket.connect` / `create_connection` / `getaddrinfo`
替换为抛错，**任何未模拟的网络访问都会立刻失败**，不会静默连到真实 OGX。

### 冻结片段校验

`WORK/<version_id>.chunks.json` 在复用前必须通过校验（`validate_chunks`）：

- 顶层必须是**非空列表**
- 每项必须是对象，且含 `chunk_id`、`content`、`chunk_metadata`
- `chunk_id`/`content` 必须是非空字符串
- `chunk_metadata` 必须是对象（OGX `/vector-io/insert` 的必需字段）
- JSON 解析失败、文件不可读也算不合法

**校验发生在创建向量库和调用 Embedding 之前。** 校验不通过时任务判 `failed`，
不会创建库、不会调用 Embedding，也不会补默认字段或改动 `chunk_id`；
缓存文件原样保留，便于人工检查。

## 修复边界

- 发布不会延长旧制度有效期；全部日期严格使用 YYYY-MM-DD。
- 多制度候选需要选择，不能视为语义冲突；同一制度区间重叠则返回版本配置冲突及各版本检索证据。
- 正常问答没有管理员绕过入口；仍是单用户本机实验，没有登录/ACL。
- Worker 使用单进程锁、任务子进程、3600秒超时、心跳与启动恢复。片段原子写入并冻结；每次重试创建新向量库，完整成功后才可发布。部分失败库保留在 ingestion_attempts 记录中供核对；远端创建成功但回包/本地记录丢失时仍可能遗留孤立库，可按 version-<ID>-attempt-<序号> 查找，本版不自动清理。
- 服务器已验证处理中终止/重启后重新排队并完成；心跳中断及恢复也已验证。内部 process 函数复跑测试只保护最终 ready/published 状态，不能证明 store_id 和尝试记录不变；正常 CLI 会拒绝重跑已发布版本。
- 已恢复第一阶段 system prompt、temperature=0.7、top_p=0.8、max_tokens=512。未新增自动语义冲突识别。

---

# 第二阶段：制度版本管理最小实现

独立于第一阶段：单独目录、单独数据、单独端口，**不修改**第一阶段的库与数据。

## 目录

    /root/rag-lab/rag-version-lab/
      version_store.py      SQLite 数据层
      version_rules.py      规则与事务（纯函数，便于单测）
      version_app.py        FastAPI，端口 8089
      version_worker.py     后台入库
      tests/test_rules.py   53 项（含接口与后台进程回归）规则单测
      data/                 版本库 version.db、上传原件、产物
    /root/rag-lab/rag-version-lab/data/version.db

复用第一阶段：`/root/rag-lab/rag-upload-lab` 的解析、分块、OGX 客户端；
`.venv-ingest` 环境；OGX 与 pgvector；不新增 GPU、不改模型。

## 数据模型

versions 表关键字段：

| 字段 | 含义 |
|---|---|
| policy_id | 制度标识，如 `it-device-policy` |
| version_label | 版本标签，如 v1 / v2 |
| scope | 适用范围，如 all / dept-it |
| valid_from / valid_to | 有效区间 **[from, to)**，`valid_to` 为空表示长期有效 |
| job_status | 入库状态 queued→parsing→chunking→embedding→ready/failed |
| publication_status | 发布状态 draft/published/withdrawn |
| supersedes | 本版替代哪个 version_id |

**入库状态与发布状态严格分离**：ready 只是「能检索」，发布是另一件事。

## 启动

    systemctl enable --now rag-version.target

或单独调试：

    cd /root/rag-lab/rag-version-lab
    VERSION_DATA_DIR=$PWD/data /root/rag-lab/.venv-ingest/bin/python \
      -m uvicorn version_app:app --host 127.0.0.1 --port 8089

## 真实操作步骤

以下命令全部可直接执行（`$P` 为 policy_id，`$S` 为 scope）。

### 1. 上传一个版本（默认 draft）

    curl -sS -X POST http://127.0.0.1:8089/api/versions \
      -F file=@it-policy-v1.md \
      -F policy_id=it-device-policy \
      -F policy_title='员工电脑管理制度' \
      -F version_label=v1 \
      -F scope=all \
      -F valid_from=2026-01-01

返回 JSON 含 `version_id`。此时 `job_status=queued`，`publication_status=draft`。

### 2. 等入库完成

    curl -sS http://127.0.0.1:8089/api/versions/<version_id> | python3 -m json.tool

`job_status` 变为 `ready` 才能发布。失败时 `job_status=failed` 且有 `error`，
**已发布的其他版本不受影响**。

### 3. 发布

    curl -sS -X POST http://127.0.0.1:8089/api/versions/<version_id>/publish

- 未 ready → 409，reason `not_ready:*`
- 同制度同范围与其他已发布版本重叠，且本版没声明 supersedes → 409，reason `overlap:<id>`
- 重复发布同一版本 → `{"result":"idempotent"}`（不报错、不改数据）

### 4. 上传并发布新版，替代旧版

    curl -sS -X POST http://127.0.0.1:8089/api/versions \
      -F file=@it-policy-v2.md -F policy_id=it-device-policy \
      -F version_label=v2 -F scope=all -F valid_from=2026-11-01 \
      -F supersedes=<v1的version_id>

发布后 v1 的 `valid_to` 自动写成 `2026-11-01`（与 v2 的 valid_from 对齐）。

### 5. 按日期查询

    curl -sS 'http://127.0.0.1:8089/api/resolve?policy_id=it-device-policy&scope=all&as_of=2026-10-27'

返回 `status`：`ok` / `no_data` / `needs_selection` / `conflict` / `missing_scope`。不同制度同时有效时返回 needs_selection，要求指定 policy_id，不推断正文矛盾。conflict 仅表示同一制度有效区间重叠。
省略 `as_of` 时取业务当天（Asia/Shanghai）。

### 6. 问答

    curl -sS -X POST http://127.0.0.1:8089/api/ask \
      -H 'Content-Type: application/json' \
      -d '{"question":"电脑使用3年能申请换新吗？","policy_id":"it-device-policy","scope":"all","as_of":"2026-10-27"}'

返回含 `as_of`、`version`（policy_id/version_label/valid_from/valid_to）、
`sources`（每条带 chunk_id 与所属版本）。

没有适用版本时返回 `status=no_data`，**不会退回所有 ready 版本**。

### 7. 撤回

    curl -sS -X POST 'http://127.0.0.1:8089/api/versions/<version_id>/withdraw?reason=业务撤回'

撤回后该版本不再参与检索；操作写入 `publish_log` 表。

### 8. 失败任务重试

    curl --fail-with-body -sS -X POST http://127.0.0.1:8089/api/versions/<version_id>/retry

只允许 failed 草稿重试。处理中重启会重新排队；ready 不变。普通问答已移除 admin_version_id，document_ids 等未知字段返回422，不会被静默忽略。查询历史请使用 policy_id、scope、as_of。

## 单测

    cd /root/rag-lab/rag-version-lab
    /root/rag-lab/.venv-ingest/bin/python -m pytest tests -q

53 项（含接口与后台进程回归），覆盖区间边界、未就绪不可发布、重叠拒绝、替代关系、撤回、跨制度选择澄清、
重复发布幂等、非法日期。

## 未实现（不要写成已有）

- 自动语义冲突检测与人工冲突条款标注：尚未实现；多制度返回 `needs_selection`，不声称正文存在矛盾
- 文档级 ACL：scope 只用于检索范围，不充当权限
- 多用户、并发写入压测
- 索引调优、重排、混合检索


## Web 入口（新增）

启动 `rag-version-api` 后打开 `http://127.0.0.1:8089/`。页面由同一个 API 服务提供，不需要 Node.js、额外容器或额外 systemd 单元；第一篇 8088 页面保持独立。

远程访问可转发端口：`ssh -L 8089:127.0.0.1:8089 用户@服务器`，再在个人电脑打开上述地址。服务仍仅监听回环地址；当前没有登录与 ACL，不应直接对公网开放。

1. 选择一个文件，填写制度标识、版本标签、范围和生效日期。标题、失效日期与替代关系可选。
2. 替代版本下拉框仅列出同制度、同范围的已发布版本。有效期重叠但未指定替代关系时，后端拒绝发布。
3. 上传后查看入库状态，等到“入库完成”后点击“发布”。撤回会使此版本不再参与检索，包括历史日期查询；失败草稿可重试。
4. 在问答区填写范围、问题，可指定制度与日期。多个制度适用时页面显示选择按钮；不会跳过选版规则。

更新已有部署时，将 `version_app.py`、`index.html`、`ui.js` 一起复制到 `/root/rag-lab/rag-version-lab/`，然后 `systemctl restart rag-version-api`。不要覆盖 data。其余代码若版本不一致，仍走上方完整更新流程。

新增页面与 API 表单回归后，本地测试总计 55 项通过；浏览器验证了页面渲染和无适用版本的反馈。此次前端新增尚未在 RHEL 上联调上传与真实模型生成；前述服务器 53 项结果属于前端加入前的版本。
