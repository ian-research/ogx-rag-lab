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
    assert result['collection_ok'] is True and R.exit_code(result)==0
    assert not (out/'.manifest.json.tmp').exists()
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
    assert all(row['state']=='http_error' for row in result['cases'])
    assert result['collection_ok'] is False and R.exit_code(result)==2

def test_dry_run_does_not_send_and_saves_label(tmp_path):
    def forbidden(req):raise AssertionError('must not send')
    result=R.run(SOURCE,CASES,tmp_path/'dry',remaining=0,transport=httpx.MockTransport(forbidden),dry_run=True)
    assert result['requests_sent']==0 and result['dry_run']
    assert all(row['state']=='dry_run_not_sent' for row in result['cases'])
    assert result['collection_ok'] is None and R.exit_code(result)==0

@pytest.mark.parametrize('payload,state', [
    (b'not-json','invalid_response'),
    (b'{"choices":[]}','invalid_response'),
    (b'{"choices":[{"message":null}]}','invalid_response'),
    (b'{"choices":[{"message":{"content":"   "},"finish_reason":"stop"}]}','empty_response'),
    (b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}','incomplete_response'),
    (b'{"choices":[{"message":{"tool_calls":"invalid"},"finish_reason":"tool_calls"}]}','invalid_response'),
])
def test_invalid_empty_or_truncated_response_is_not_collection_success(tmp_path,payload,state):
    out=tmp_path/'run'
    result=R.run(SOURCE,CASES,out,remaining=8,transport=httpx.MockTransport(
        lambda request:httpx.Response(200,content=payload)))
    assert all(row['state']==state for row in result['cases'])
    assert result['collection_ok'] is False and R.exit_code(result)==2
    assert (out/(R.IDS[0]+'.response.bin')).read_bytes()==payload

def test_timeout_is_recorded_without_retry_or_success(tmp_path):
    seen=[]
    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout('controlled timeout',request=request)
    out=tmp_path/'run'
    result=R.run(SOURCE,CASES,out,remaining=8,transport=httpx.MockTransport(timeout))
    assert len(seen)==8 and result['requests_sent']==8
    assert all(row['state']=='network_error' for row in result['cases'])
    assert R.exit_code(result)==2
    assert json.loads((out/'manifest.json').read_text())==result

def test_all_fixtures_are_validated_before_first_request(tmp_path):
    spec=json.loads(CASES.read_text())
    spec['cases'][-1]['seed']['result']=[]
    bad=tmp_path/'bad-cases.json';bad.write_text(json.dumps(spec))
    def forbidden(request):raise AssertionError('must not send')
    with pytest.raises(ValueError,match='fixture'):
        R.run(SOURCE,bad,tmp_path/'out',remaining=8,transport=httpx.MockTransport(forbidden))
    assert not (tmp_path/'out').exists()
