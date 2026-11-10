"""Application-owned read-only allowlist; identity/date are never model arguments."""
import json
from jsonschema import Draft202012Validator

POLICIES = ["eval3-travel", "eval3-device", "eval3-security", "eval3-vendor", "eval4-inject"]
SCHEMAS = {
    "get_employee_laptop_info": {"type": "object", "properties": {
        "dummy_parameter": {"type": "string", "maxLength": 0}}, "additionalProperties": False},
    "get_ticket": {"type": "object", "properties": {
        "ticket_id": {"type": "string", "minLength": 1, "maxLength": 80,
                      "pattern": "^[A-Za-z0-9_-]+$"}},
        "required": ["ticket_id"], "additionalProperties": False},
    "search_documents": {"type": "object", "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
        "policy_id": {"type": "string", "enum": POLICIES}},
        "required": ["question", "policy_id"], "additionalProperties": False},
}
DESCRIPTIONS = {
    "get_employee_laptop_info": "查当前已认证员工本人的笔记本，无需资产编号；返回当前演示台账记录。购买日期不等于领用日期，台账没有领用日期时实际使用时长未知。",
    "get_ticket": "按用户提供的编号查本人工单当前记录；缺编号先询问。不存在或无权查看均返回 no_record。",
    "search_documents": "检索已发布文档原文：eval3-travel 差旅、eval3-device 设备、eval3-security 安全、eval3-vendor 供应商；eval4-inject《差旅报销补充说明》（评估示例文档）。适用日期由页面/应用确定，不能自行修改；返回中 valid_to_display 为“未设置结束日期”表示该版本没有计划失效日。",
}
MODEL_TOOLS = [{"type": "function", "function": {"name": name,
    "description": DESCRIPTIONS[name], "parameters": schema}} for name, schema in SCHEMAS.items()]


class RunFailure(Exception):
    def __init__(self, status, message, http_status=502):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status


def validate_arguments(name, args):
    if name not in SCHEMAS:
        raise RunFailure("invalid_tool_call", "模型请求了未允许的工具，未执行。", 422)
    if list(Draft202012Validator(SCHEMAS[name]).iter_errors(args)):
        raise RunFailure("invalid_tool_call", "工具参数不符合约定，未执行。", 422)
    return args


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate JSON key")
        result[k] = v
    return result


def validate_call(call):
    try:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError()
        call_id = call["id"]
        if not isinstance(call_id, str) or not call_id or len(call_id) > 200:
            raise ValueError()
        fn = call["function"]
        name, raw = fn["name"], fn["arguments"]
        if not isinstance(name, str) or not isinstance(raw, str) or len(raw) > 10000:
            raise ValueError()
        args = json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (KeyError, TypeError, ValueError):
        raise RunFailure("invalid_tool_call", "工具调用不是有效的 JSON 对象，未执行。", 422) from None
    return call_id, name, validate_arguments(name, args)
