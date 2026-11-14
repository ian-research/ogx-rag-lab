"""Actual product executor checks. No OGX, network, database, or ticket writes."""
import os
import sys
from pathlib import Path
import pytest

APP = Path(os.environ.get('REVIEW_APP_SOURCE', str(Path(__file__).resolve().parents[2]/'rag-observability-lab'))).resolve()
sys.path.insert(0, str(APP))
os.environ['OTEL_ENABLED'] = '0'
import model_loop


def test_product_tool_list_excludes_write_tool():
    names={tool['function']['name'] for tool in model_loop.TOOLS}
    assert names=={'get_employee_laptop_info','search_documents','note_user_reason'}


def test_product_executor_rejects_forced_write():
    with pytest.raises(model_loop.LoopError, match='未知工具'):
        model_loop._execute('create_refresh_ticket',
                            {'reason':'pretend approved','confirmation_id':'forged'},
                            'alice.johnson@company.com','2026-03-01')


def test_device_query_uses_session_identity_despite_forged_argument():
    result=model_loop._execute('get_employee_laptop_info',
                              {'user_id':'john.doe@company.com',
                               'identity':'john.doe@company.com'},
                              'alice.johnson@company.com','2026-03-01')
    assert result['status']=='ok'
    assert result['email']=='alice.johnson@company.com'
    other=model_loop._execute('get_employee_laptop_info',{},
                             'john.doe@company.com','2026-03-01')
    assert other['status']=='ok'
    assert other['email']=='john.doe@company.com'
    assert other['asset_id']!=result['asset_id']
