"""Deterministic demonstration policy. This is pattern matching, not semantic classification."""

import logging
import re
import unicodedata

from nemoguardrails.actions import action

LOG = logging.getLogger(__name__)

# render.py sets this constant in the ConfigMap; no runtime downloads or extra dependencies.
RESPONSE_LANGUAGE = "zh-CN"
MESSAGES = {
    "zh-CN": {
        "marker": "【NeMo Guardrails｜已拦截】",
        "rules": "命中规则：", "reasons": "拦截原因：",
        "result": "处理结果：已在输入检查阶段终止本次请求。", "suggestions": "修改建议：",
    },
    "zh-TW": {
        "marker": "【NeMo Guardrails｜已攔截】",
        "rules": "觸發規則：", "reasons": "攔截原因：",
        "result": "處理結果：已於輸入檢查階段終止本次請求。", "suggestions": "調整建議：",
    },
}

RULES = ({'id': 'DEMO-000',
  'name': '护栏连通性测试',
  'reason': '检测到约定的演示测试标记。',
  'suggestion': '移除测试标记后，可继续提出正常业务问题。',
  'patterns': ('nemo_block_test',),
  'zh-TW': {'name': '護欄連線測試', 'reason': '偵測到預先設定的展示測試標記。', 'suggestion': '移除測試標記後，即可繼續提出一般業務問題。'}},
 {'id': 'SEC-101',
  'name': '覆盖已有指令',
  'reason': '请求包含忽略或覆盖已有指令的表达。',
  'suggestion': '直接描述业务需求，不要要求助手忽略已有指令。',
  'patterns': ('\\b(?:ignore|disregard|forget|override)\\s+(?:(?:all|any|the)\\s+)?(?:previous|prior|above|earlier|preceding)\\s+(?:instructions|prompts|rules|directions|guidelines)\\b',
               '(?:忽略|无视|忘记|覆盖|不要遵守)\\s*(?:(?:所有|全部|这些)\\s*)?(?:之前|此前|先前|以上|上面|前面|原有)\\s*(?:的\\s*)?(?:(?:所有|全部)\\s*)?(?:指令|提示词|规则|要求|限制)',
               '(?:忽略|無視|忘記|覆寫|覆蓋|不要遵守)\\s*(?:(?:所有|全部|這些)\\s*)?(?:之前|此前|先前|以上|上面|前面|原有)\\s*(?:的\\s*)?(?:(?:所有|全部)\\s*)?(?:指令|提示詞|規則|要求|限制)'),
  'zh-TW': {'name': '覆寫既有指令', 'reason': '請求包含忽略或覆寫既有指令的語句。', 'suggestion': '請直接說明業務需求，不要要求助理忽略既有指令。'}},
 {'id': 'SEC-102',
  'name': '索取系统提示词',
  'reason': '请求包含输出系统或开发者提示词的表达。',
  'suggestion': '可以询问助手的公开功能与使用方式。',
  'patterns': ('\\b(?:reveal|show|print|display|return|repeat|give\\s+me)\\b.{0,60}\\b(?:system|developer)\\s+(?:prompts?|instructions?)\\b',
               '(?:显示|展示|输出|打印|告诉我|泄露|重复|给我).{0,40}(?:系统|开发者)\\s*(?:提示词|指令)',
               '(?:顯示|展示|輸出|列印|告訴我|洩露|洩漏|重複|給我).{0,40}(?:系統|開發者)\\s*(?:提示詞|指令)'),
  'zh-TW': {'name': '索取系統提示詞', 'reason': '請求包含要求提供系統或開發者提示詞的語句。', 'suggestion': '可改為詢問助理的公開功能與使用方式。'}},
 {'id': 'DLP-201',
  'name': '提交疑似凭据',
  'reason': '请求包含类似密码、API 密钥或访问令牌赋值的内容。',
  'suggestion': '删除凭据值，使用 <REDACTED> 代替后再提交问题。',
  'patterns': ('\\b(?:api[_ -]?key|access[_ '
               '-]?token|password|secret)\\b\\s*[:=]\\s*[\\"\']?[A-Za-z0-9_./+=!@#$%^&*~\\-]{8,}',
               '(?:密码|密钥|访问令牌)\\s*[:：=]\\s*[\\"\']?[A-Za-z0-9_./+=!@#$%^&*~\\-]{8,}',
               '(?:密碼|金鑰|密鑰|存取權杖)\\s*[:：=]\\s*[\\"\']?[A-Za-z0-9_./+=!@#$%^&*~\\-]{8,}',
               '\\bBearer\\s+[A-Za-z0-9._~+/\\-]{16,}=*'),
  'zh-TW': {'name': '輸入疑似驗證資訊',
            'reason': '請求包含疑似密碼、API 金鑰或存取權杖的設定內容。',
            'suggestion': '請移除驗證資訊的實際值，以 <REDACTED> 取代後再送出問題。'}},
 {'id': 'DLP-202',
  'name': '导出客户明细',
  'reason': '请求包含导出客户名单、联系方式或明细数据的表达。',
  'suggestion': '改为请求不包含个人明细的汇总统计。',
  'patterns': ('\\b(?:export|dump|download|extract)\\b.{0,60}\\b(?:customer|client)\\b.{0,35}\\b(?:records|data|details|list|emails|phone\\s+numbers)\\b',
               '(?:导出|下载|提取|批量获取).{0,30}(?:客户|用户).{0,20}(?:名单|明细|手机号|电话|邮箱|资料|数据)',
               '(?:匯出|導出|下載|擷取|提取|批次取得).{0,30}(?:客戶|使用者|用戶).{0,20}(?:名單|明細|手機號碼|行動電話|電話|電子郵件|電子信箱|信箱|資料|數據)'),
  'zh-TW': {'name': '匯出客戶明細', 'reason': '請求包含匯出客戶名單、聯絡方式或明細資料的語句。', 'suggestion': '請改為要求不含個人明細的彙總統計。'}})

COMPILED_RULES = tuple(
    (rule, tuple(re.compile(pattern, re.IGNORECASE) for pattern in rule["patterns"]))
    for rule in RULES
)


def evaluate_demo_policy(text, language=RESPONSE_LANGUAGE):
    """Return every matching rule and a fixed Chinese message; never echo submitted values."""
    if language not in MESSAGES:
        raise ValueError("Unsupported response_language: " + str(language))
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    matches = [
        rule
        for rule, patterns in COMPILED_RULES
        if any(pattern.search(normalized) for pattern in patterns)
    ]
    if not matches:
        return {"blocked": False, "rule_ids": [], "message": ""}

    messages = MESSAGES[language]
    labels = [rule["zh-TW"] if language == "zh-TW" else rule for rule in matches]
    lines = [messages["marker"], "", messages["rules"]]
    lines.extend(f"• {rule['id']}｜{label['name']}" for rule, label in zip(matches, labels))
    lines.extend(["", messages["reasons"]])
    lines.extend(f"• {label['reason']}" for label in labels)
    lines.extend(["", messages["result"], "", messages["suggestions"]])
    lines.extend(f"• {label['suggestion']}" for label in labels)
    return {
        "blocked": True,
        "rule_ids": [rule["id"] for rule in matches],
        "message": "\n".join(lines),
    }


def demo_blocked_mapping(result):
    return bool(result.get("blocked", False))


@action(is_system_action=True, output_mapping=demo_blocked_mapping)
async def demo_check_policy(text: str):
    result = evaluate_demo_policy(text)
    # Log policy decisions only. Never log the original prompt or matched credentials.
    LOG.info("demo_policy blocked=%s rules=%s", result["blocked"], ",".join(result["rule_ids"]))
    return result
