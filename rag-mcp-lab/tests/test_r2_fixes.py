"""r2 fixes: date semantics, usage-duration honesty, instruction-note handling, config template."""
from datetime import date
from pathlib import Path

import business_query as bq
import mcp_business_server as mbs
import orchestrator as o

ROOT = Path(__file__).resolve().parents[1]


def test_usage_duration_unknown_and_not_derived_from_purchase():
    info = bq.get_laptop_info('alice.johnson@company.com', date(2026, 11, 9))
    assert info['assigned_date'] is None
    assert info['assigned_date_status'] == '台账未提供'
    assert info['usage_duration'] is None
    assert info['usage_duration_status'] == '未知'
    assert info['purchase_age_is_usage_duration'] is False
    assert info['age_basis'] == 'purchase_date_not_assigned_date'


def test_instruction_like_note_is_omitted_and_audited():
    result = bq.get_ticket('LAB-TK-004', 'alice.johnson@company.com')
    assert result['status'] == 'ok'
    assert '忽略所有先前指令' not in ' '.join(result['work_notes'])
    assert result['work_notes_filter']['redacted'] == 1
    assert result['work_notes_filter']['reason'] == 'instruction_like_text'
    assert '忽略所有先前指令' in result['_audit_redacted']['originals'][0]


def test_ordinary_notes_are_untouched():
    result = bq.get_ticket('LAB-TK-001', 'alice.johnson@company.com')
    assert result['work_notes_filter']['redacted'] == 0
    assert result['work_notes_filter']['reason'] == 'none'
    assert '_audit_redacted' not in result
    assert len(result['work_notes']) == 2


def test_date_semantics_marks_open_ended_as_not_permanent():
    sem = mbs.date_semantics('2026-03-01')
    assert sem['as_of'] == '2026-03-01'
    assert '不是文档的截止日期' in sem['as_of_role']
    assert '未设置结束日期' in sem['valid_to_null_meaning']
    assert '长期有效' in sem['valid_to_null_meaning']
    assert sem['end_date_exclusive'] is True


def test_source_dates_are_enriched_for_the_model():
    result = {'sources': [{'valid_from': '2026-01-01', 'valid_to': None},
                          {'valid_from': '2026-01-01', 'valid_to': '2026-06-01'}]}
    mbs.enrich_document_dates(result, '2026-07-01')
    assert result['query_date'] == '2026-07-01'
    assert result['sources'][0]['valid_to_display'] == '未设置结束日期'
    assert result['sources'][0]['valid_to_is_open_ended'] is True
    assert result['sources'][1]['valid_to_display'] == '2026-06-01'
    assert result['sources'][1]['valid_to_is_open_ended'] is False


def test_prompt_states_the_three_rules():
    prompt = o.SYSTEM_PROMPT
    assert '生效日' in prompt and '失效边界' in prompt and '查询适用日' in prompt
    assert 'citation_label' in prompt
    assert '不要声称“当天起适用下一版本”' in prompt
    assert '购买日期不等于领用日期' in prompt
    assert '不要逐字复述' in prompt


def test_units_read_the_persistent_config():
    for name in ('article04-mcp.service.in', 'article04-orchestrator.service.in'):
        text = (ROOT / 'deploy' / name).read_text(encoding='utf-8')
        assert 'EnvironmentFile=@CONFIG_PATH@' in text
        assert '@APP_DIR@' in text
        assert '/root/rag-lab/article04-repair' not in text


def test_first_install_does_not_require_existing_units():
    text = (ROOT / 'scripts' / 'install.sh').read_text(encoding='utf-8')
    assert 'switch-services.sh --switch' in text
    assert 'refusing to overwrite' in text
