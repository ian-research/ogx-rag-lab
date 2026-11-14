# 测试与复跑范围

程序正确性、模型解释质量、页面字段显示分开检查。42项程序测试不是42道模型题，也不是回答准确率。

## 程序测试

在本目录，使用独立测试环境：

```bash
python3.12 -m venv .venv-test
.venv-test/bin/pip install --require-hashes -r requirements.lock -r requirements-test.lock
.venv-test/bin/python -m pip check
.venv-test/bin/python -m pytest tests -q
```

覆盖鉴权与角色、服务和时间窗口、Jaeger真实拓扑字段契约、微秒/毫秒换算、摘要预算、编号检查、证据复用不覆盖、运行编号唯一、后端异常、429分支、计数器重置、稀疏/过旧/缺失样本、根页面入口，以及安装失败清理和替换回退。

后端和命令使用测试替身；不会调用现有模型、操作真实systemd或停止服务。RHEL Python3.12.15原部署记录为42 passed；同一运行源码在独立macOS Python3.13环境也通过42项。弃用警告不当成模型或服务故障。

## 离线页面字段检查

```bash
node scripts/check_page.cjs
# 可选：指定另外一组相同结构的fixture目录
node scripts/check_page.cjs /path/to/fixtures
```

默认使用 `tests/fixtures/` 内的合成响应。检查阶段/窗口事实、非ok状态和“编号已登记；支持关系待核对”的标签。它是Node里的页面脚本逻辑检查，不是实际浏览器、鉴权或端到端模型测试，不需要额外npm包。

## 在线逐项检查

按部署说明连接8096。业务查询与分析分时运行，先在8094发起请求，保存其Trace编号，等待冻结窗口覆盖它。要精确指定Trace，向 `POST /api/analyze` 传入 `trace_id`，不要用自动挑选的最近Trace冒充指定案例。

`/api/analyze` 会采集证据并调用模型；`/api/explain` 用已保存的 `evidence_id` 换个问题再调用模型。两次解释保持同一份证据，仍是两个不同角度的问题，不是严格的同义改写A/B或性能对照。

| 正式案例 | 核对点 |
|---|---|
| C1 正常设备查询 | 本轮root约2.9秒，chat两次合计约2896ms；客户端往返与GPU内部、嵌套阶段分开 |
| C2 文档查询故障 | 本轮使用可逆实验开关，doc.search Error / injected_unavailable，外层HTTP200；恢复后查询成功 |
| C3b 缺字段 | 问电池更换日期和维修单号，台账不含字段；Trace不能证明业务事实 |
| C4 缺GPU数据 | 无队列/prefill/decode/GPU采样，不据接口耗时断言资源不足 |
| C5 两窗计数 | 两个5分钟窗口观测增量3/4；16条标签序列、12个路由；步长15秒，两倍是30秒 |

这些数值是2026-11-14这轮记录，不是新的环境复跑必须相等的断言。十条正式解释中，助手评阅为8可接受、1需修正、1不可接受；不是作者逐题人工评分，不换算准确率。旧电池保修C3已排除，不能混入正式十问。

C2的实验开关位于当时专用业务测试实例，不是本篇分析API提供的故障注入接口。本公开包不携带修改共享服务的探索脚本。读者复测故障应使用自己的独立业务测试实例，并保存失败与恢复证据；不要停止共享文档、OGX或vLLM来凑案例。其他场景可先使用正常服务完成。

重点保留两个反例：C1.2把没有错误Span写成“无错误发生”，且混淆chat最大值与loop合计；C5.1引用编号正确，仍把2×15写成3、把序列数当路由数。结构/引用编号检查不会验证这些结论。

## 检查已保存证据

API状态目录默认如下：

```text
article07-state/
  private/        # 管理员凭据，不能公开
  evidence/       # 完整冻结bundle
  evidence-raw/   # 原始工具返回
  runs/           # 每次解释的请求、响应和证据哈希
```

```bash
python3.12 scripts/verify_evidence.py /root/rag-lab/article07-state
```

脚本只读取数据，检查每条run记录的 `evidence_sha256`；可加 `--output /tmp/evidence-check.json` 写出校验报告。空目录会失败，不把0/0当通过。该校验确认对应关系，没有证明模型结论或业务台账正确。

原始评测包采用 `evidence/runs` 与 `evidence/frozen` 的归档布局，脚本也能识别；原始业务数据没有放进公开源码。

## 源码打包

```bash
bash scripts/package07.sh /tmp/article07-source.tar.gz
```

使用显式文件白名单，排除虚拟环境、状态目录、凭据、原始业务证据和旧探索脚本。包内包含源文件SHA256清单，脚本另写压缩包的 `.sha256`。解包后在 `rag-insight-lab` 目录可复跑程序测试及页面逻辑检查。

systemd已设置开机启用，但本轮没有整机重启、压力/双身份并发或长期留存测试。并发测试只验证429拒绝分支，不能扩大表述。
