# 开发阶段验证记录

> 本文件保留开发时的验证范围。此后服务器上传/问答已得到用户输出确认；本次阶段归档的最新范围请读上级《验收记录与已知限制》，其中仍区分 Word 标题修复的本地验证与未收到的远端重建验收。

验证日期：2026-10-27。执行环境：本地 macOS / Python 3.14；目标部署环境是用户的 AWS RHEL 10 / Python 3.12，尚未在目标服务器执行。

## 已通过

DOCX 标题修复后：9 项 pytest 测试通过。示例 it-policy.docx 实际解析为 3 个正文结构块，分别保留 IT 政策 > 电脑换新 / 软件许可证 / 密码重置。新增测试覆盖总标题不独立索引、跨级章节与表格的标题路径，以及重建任务保留旧版本、使用全新分块目录和向量库。

- 7 项 pytest 测试：上传与去重、无效文件拒绝、队列领取/恢复/重试、标题与 token 预算、DOCX 段落/表格顺序、部分入库失败后复用 store 与 chunk ID、最新成功文件选择、纯检索接口不调用生成模型（部分检查组合在同一测试中）。
- 安装依赖后 pip check，无依赖冲突。
- 安装脚本 bash -n；页面 JavaScript node --check。
- 实际 Transformers AutoTokenizer 本地加载与分块 token 预算检查。该检查使用本地小型测试词表；实际 BGE tokenizer 从目标服务器已有模型目录加载。
- 实际解析 examples/it-policy.md 和 examples/it-policy.docx，正文政策正确提取。
- 实际解析 examples/it-policy.pdf：英文文字 PDF，正确提取 4 年和直属经理的政策，保留标题与页码。
- 实际解析 examples/it-policy-scan.pdf：仅图片的英文扫描 PDF，RapidOCR 正确识别相同政策并保留页码。

## 实现调整

Docling 2.128.0 内置 RapidOCR 适配层与 rapidocr 3.10.0 语言接口实测不匹配。最终实现使用 Docling 处理文字页，扫描页直接调用 RapidOCR，避免该适配层。分辨率使用 216 DPI。

## 仍需目标服务器验收

- 与现有 OGX、vLLM、pgvector 的真实联调。自动化测试中的远端接口是模拟接口。
- RHEL systemd 启动与重启。
- 实际中文扫描件、复杂表格、图文混排和大文件的识别质量。
- LibreOffice 的旧版 DOC 转换（本地没有 LibreOffice）。
- 这一版是单用户实验服务，没有企业登录、ACL、多机队列及自动删除清理。
