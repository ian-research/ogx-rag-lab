# 第四篇：通过只读 MCP 查询设备、工单和文档

接续前三篇的 OGX RAG 实验：在同一张 L40S 上复用模型与知识库，新增 Web 查询页面、应用侧工具循环和只读 MCP 服务。查询使用固定的演示设备与合成工单数据，不连接真实业务系统。

```text
浏览器 → 查询页面 / 应用编排（8091）→ OGX（8321）→ vLLM（8000）
                    ↓ MCP 客户端
               只读 MCP（8090）
               ├─ 本人设备：上游固定样例
               ├─ 本人工单：新增合成数据
               └─ 文档检索 → 版本服务（8089）→ OGX / Embedding / pgvector
```

模型提出工具调用，应用校验并执行，将结果回填后继续生成。工具循环在本应用中，不是 OGX 原生 MCP 编排。两个新增服务只监听 `127.0.0.1`。

## 入口

- [部署与配置](DEPLOYMENT.md)：依赖准备、首次安装、迁移、配置、凭据、访问和回退。
- [测试与结果范围](TESTING.md)：程序测试、真实请求采集、人工核对与服务恢复。
- [第三篇实验文档和评测](../rag-eval-lab/README.md)：文档选版与检索的前置数据。
- [第二篇文档版本服务](../rag-version-lab/README.md)：8089 的上传、发布和检索接口。
- [第一篇基础环境与服务化](../服务化部署.md)：OGX、vLLM、Embedding 和数据库。

## 三个工具

| 工具 | 行为 |
|---|---|
| `get_employee_laptop_info` | 按服务端验证的演示身份查询本人笔记本 |
| `get_ticket` | 按编号查询本人工单；不存在或无权限均返回 `no_record` |
| `search_documents` | 在固定全员范围内，按页面/应用确定的日期检索已发布文档 |

工具参数不接受员工身份、适用日期或范围覆盖。文档标识允许列表在 `tool_contracts.py`，默认只接入 `eval3-travel`、`eval3-device`、`eval3-security`、`eval3-vendor` 和测试用 `eval4-inject`。`all` 表示全员范围，不等于跨所有部门搜索。

## 数据与回答

- 文档日期只选择适用版本；设备和工单是当前演示数据，没有历史快照。
- 购买日期不是领用日期。缺少领用记录和维修成本时，不能确认满足设备换新条件。
- 参数、调用标识、轮次、工具次数、超时、结果大小和错误状态均由应用检查。默认四轮模型调用、六次工具调用、240 秒总时限。
- 文档和备注中的可疑指令使用特征检测；文档指令原句若被模型复述，应用在最终答案中做省略替换，原始输出仍保留在受限运行记录中。这不是对所有提示注入的防护保证。
- 演示凭据按账号单独分发，不提供匿名凭据枚举或角色选择器；尚未接企业 SSO、文档 ACL、真实工单写操作。

## 目录

| 路径 | 用途 |
|---|---|
| `orchestrator.py` / `web/` | 页面、认证、应用工具循环、回答与调用过程 |
| `mcp_business_server.py` / `business_query.py` | 只读 MCP、身份归属检查、日期与业务字段 |
| `transports.py` / `tool_contracts.py` | OGX/MCP 调用和工具参数约定 |
| `scripts/prepare.sh` / `install.sh` | 独立运行环境和首次安装 |
| `scripts/switch-services.sh` / `rollback.sh` | 迁移、备份与回退 |
| `deploy/*.in` | systemd 单元与持久配置模板 |
| `requirements*.lock` | 带哈希的运行及测试依赖锁 |
| `tests/` | 控制逻辑、协议与部署脚本测试 |
| `PROVENANCE.json` / `BUILD-MANIFEST.json` | 已测源码来源与本发布目录的文件哈希 |

## 来源与许可证

本实现参考 [Red Hat IT Self Service Agent，固定提交 7fc221d](https://github.com/rh-ai-quickstart/it-self-service-agent/tree/7fc221dc7991a3d51432af4202fff6dae94b8d93)。`business_data.py` 的十四条设备数据保留上游 `MOCK_EMPLOYEE_DATA` 字段值；查询访问采用本地字典，未部署上游 ServiceNowClient/mock-service-now 链路或完整智能体。工单、应用编排和文档接入是本项目新增。

上游 MIT 声明保留在 [LICENSE-UPSTREAM](LICENSE-UPSTREAM)；本仓库新增代码与文档遵循根目录 [Apache-2.0](../LICENSE)。不分发模型权重、私密凭据、真实业务资料或服务器原始运行记录。
