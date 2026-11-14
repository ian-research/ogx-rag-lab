import copy,hashlib,json,os,sys
from pathlib import Path
import httpx,pytest
CODE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE))
import run_exposure as R
SOURCE=Path(os.environ.get('REVIEW_APP_SOURCE', str(CODE.parent/'rag-observability-lab')))
CASES=Path(os.environ.get('REVIEW_CASES_SOURCE', str(CODE/'cases.json')))

def test_exact_payloads_and_no_model_tool_execution(tmp_path):
    seen=[]
    def reply(req):
        seen.append(bytes(req.content))
        return httpx.Response(200,content=b'{"choices":[{"message":{"tool_calls":[{"function":{"name":"create_refresh_ticket","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}')
    out=tmp_path/'run'
    result=R.run(SOURCE,CASES,out,remaining=8,transport=httpx.MockTransport(reply))
    assert result['requests_sent']==8 and len(seen)==8
    assert result['tool_execution'] is False
    for item,payload in zip(result['cases'],seen):
        assert (out/f"{item['case_id']}.request.json").read_bytes()==payload
        assert R.digest(payload)==item['request_sha256']
        body=json.loads(payload)
        assert body['tools']==R.contract(SOURCE)['TOOLS']
        assert [m['role'] for m in body['messages']]==['system','user','assistant','tool','tool']
        assert 'create_refresh_ticket' not in [t['function']['name'] for t in body['tools']]

def test_reject_overwrite_and_insufficient_budget_before_request(tmp_path):
    def forbidden(req):raise AssertionError('must not send')
    with pytest.raises(ValueError,match='remaining'):
        R.run(SOURCE,CASES,tmp_path/'no-budget',remaining=7,transport=httpx.MockTransport(forbidden))
    out=tmp_path/'existing';out.mkdir()
    with pytest.raises(FileExistsError):
        R.run(SOURCE,CASES,out,remaining=8,transport=httpx.MockTransport(forbidden))

def test_error_is_preserved_and_never_retried(tmp_path):
    sent=[]
    def reply(req):
        sent.append(req)
        return httpx.Response(503,content=b'{"error":"unavailable"}')
    result=R.run(SOURCE,CASES,tmp_path/'run',remaining=8,transport=httpx.MockTransport(reply))
    assert len(sent)==8
    assert all(row['http_status']==503 for row in result['cases'])
    assert all(row['tool_proposals']==[] for row in result['cases'])

def test_dry_run_does_not_send_and_saves_label(tmp_path):
    def forbidden(req):raise AssertionError('must not send')
    result=R.run(SOURCE,CASES,tmp_path/'dry',remaining=0,transport=httpx.MockTransport(forbidden),dry_run=True)
    assert result['requests_sent']==0 and result['dry_run']
    assert all(row['state']=='dry_run_not_sent' for row in result['cases'])
