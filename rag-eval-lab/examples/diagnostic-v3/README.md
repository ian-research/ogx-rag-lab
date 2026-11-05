# 已保存的诊断轮示例

原运行ID：`20261104T170909Z-7595b62b`。开始时间为UTC 2026-11-04 17:09，即北京时间2026-11-05。没有因发布代码而重新运行模型。

- `questions.jsonl`、`responses.jsonl`、`manifest.json`、`completed.json`保留原文件字节；`raw_body`完整保留接口返回。
- `initial-review.json`导出30题已确认评分，统一公开字段，去掉本地绝对路径和逐题个人时间。保留原评分、证据、理由；不是程序生成的新评分。
- `second-review.json`保留六题结论及方法限制，不包含内部会话记录。
- `summary.json`、`selection.json`由本次通用工具对上述保存材料重算，不是新推理结果。

本轮：RHEL 10.2、L40S、Qwen3.5-9B；具体revision及参数见manifest。不要把旧的版本ID和store_id填入自己的部署。服务提交为 `86ba3c0b3671717a3aa366f2f8c9231f37aa0b29`；发布后的采集/评审工具属于另一次代码整理。

原manifest的`rubric_version`记作v0.4，是采集当时记录，不追改历史。后续人工初评采用v0.5及已确认补记，公开整理见上级 `RUBRIC.md`。

已知问题：设备实验文档第17条误引第18条，未就地改语料；B2因为范围不匹配返回no_data正确，但固定提示只说日期，提示不准确。两者仍是本轮诊断记录，不宣称已修复。

六题是K5-a、K5-b、K4-a、X1-a、N2、B3，评分和逐事实覆盖与初评无差异。15条来源只来自差旅v1和设备两份文件。复核者已参与初评，不能据此报告独立评审一致率。

固定输入哈希：

```text
questions.jsonl  233a964ea55189ba5eba1a3dc1a24ae9db38f555eea6ef0bce0bef1aff7c36ab
responses.jsonl  e2b4bfabd3ecc9dcd57efc295ba4e0a536b32d9a02f60c39841637f1a1f20f7f
```
