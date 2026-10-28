# P1–P6 修复与部署说明

仅修改 rag-version-lab；不改变第一阶段、模型、密码或服务 Requires 关系。

- P1：新发布替代关系保存旧有效期（含 NULL）。撤回新版时，在同一事务恢复前一版并写 restore 审计。前一版须仍 published，截止日须仍为本次截断值，之后没有其他发布/撤回/替代/恢复操作，且恢复不与已发布版本重叠。否则返回 restoration.status=skipped 和原因，页面提示检查空档。旧记录没有快照时不猜测，不追溯修复 ui-test-02。撤回版本自身仍从所有日期检索中排除。多级替代链采取保守策略，不跨越已变化的前一版本恢复。
- P2：sources 保留 kind，页面区分 OCR识别、表格、文本。
- P3：前端和接口提前拒绝 .doc，提示另存为 DOCX。不安装 LibreOffice。
- P4：只有 published 且 ready 行显示问答按钮。
- P5：修改输入或更换文件时清除旧上传提示。
- P6：created_at、published_at、withdrawn_at 输出为 ISO8601 UTC（+00:00）；新审计记录也带 UTC。旧行不批量重写；旧无时区时间按原 RHEL 的 UTC 解释。其他主机可设 RAG_LEGACY_TIMESTAMP_TZ=Asia/Shanghai 等原时区。业务日期 valid_from/valid_to/as_of 仍按上海时区解释，不转换。

## 升级与回退

确认无在途任务，停止第二阶段，备份整个应用目录（含 data/WAL）和 unit。只替换代码、index.html、ui.js、migrate_db.py、测试及文档，不覆盖 data。

```bash
systemctl stop rag-version.target rag-version-api rag-version-worker
# 先完成上述离线备份，再替换代码
cd /root/rag-lab/rag-version-lab
/root/rag-lab/.venv-ingest/bin/python migrate_db.py --services-stopped
/root/rag-lab/.venv-ingest/bin/python check_existing.py data/version.db
systemctl start rag-version.target
curl --fail-with-body -sS http://127.0.0.1:8089/api/health
```

迁移只新增 replacement_snapshots 表。旧 schema 首次迁移前使用 SQLite backup API 保存 version.db.pre-restore-<UTC时间>.bak（包含已提交WAL数据），不改历史 versions 行。迁移可重复执行，API/Worker init 同样保护旧数据库。

回退：停止第二阶段，将旧代码和完整 data 快照一起恢复。也可在替换回旧代码前运行新迁移工具：

```bash
python migrate_db.py --services-stopped --restore /绝对路径/version.db.pre-restore-时间.bak
```

工具先保留当前库为 before-rollback 备份，再恢复指定快照；之后必须部署匹配的旧代码才可重启。--services-stopped 是操作者确认标记，不代替实际停止服务。升级后新增原件及向量库保留，不自动清理，数据库回退不撤销远端向量写入。

## 验证

本地 Python 68 项通过；前端脚本模拟DOM测试3项通过。覆盖迁移备份/回退、有限/无限期恢复、后继重叠和后续修改防护、事务回滚、重新发布、kind、DOC拒绝与时区。前端模拟DOM不代替浏览器验证。

```bash
python -m pytest tests -q
node --test tests/test_ui.cjs
```

Node 仅开发测试使用，服务器运行不需要。实际部署、浏览器、真实扫描件OCR引用由DSH复测；不得将本地通过称为线上通过。
