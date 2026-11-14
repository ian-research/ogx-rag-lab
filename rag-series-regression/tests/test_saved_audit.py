import copy,hashlib,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from audit_regression import audit

@pytest.fixture
def saved():
    basis={'status':'not_retrieved','sources':[]}
    fresh={'draft_id':'D1','version':2,'asset':{'asset_id':'A1','model':'Demo'},
           'reason':'电池故障','facts':[{'label':'资产编号','value':'A1'}],
           'basis':basis,'missing':[{'item':'维修报价','status':'未知'}]}
    canon={**copy.deepcopy(fresh),'user_id':'demo-a@example.invalid'}
    raw=json.dumps(canon,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    h=hashlib.sha256(raw.encode()).hexdigest();fresh['content_hash']=h
    payload={'content_canonical':raw,'content_hash':h,'draft_ref':'D1','version':2,
             'submitted_by':'demo-a@example.invalid','asset_id':'A1','reason_text':'电池故障',
             'declared_missing':['维修报价'],'doc_snapshot':{'status':'not_retrieved'},
             'ticket_status':'pending_review'}
    ticket={'ticket_no':'T1','asset_id':'A1','reason':'电池故障','status':'pending_review','payload':payload}
    values={'ask':{'draft':{'version':1}},'revise':{'draft':fresh},'ticket':{'ticket':ticket},
            'submit':{'ticket_no':'T1'},'submit-duplicate':{'ticket_no':'T1'},'submit-old':{'detail':'stale'}}
    return {'http_log':[{'kind':k,'response':v,'status':409 if k=='submit-old' else 200} for k,v in values.items()]}

def test_consistent_saved_flow_passes(saved):
    assert audit(saved)['all_snapshot_checks_pass']

@pytest.mark.parametrize('field', ['reason_text','asset_id','declared_missing','doc_snapshot','content_hash'])
def test_corrupted_ticket_snapshot_fails(saved,field):
    payload=next(x for x in saved['http_log'] if x['kind']=='ticket')['response']['ticket']['payload']
    payload[field]={'wrong':'value'} if field=='doc_snapshot' else [] if field=='declared_missing' else 'tampered'
    assert not audit(saved)['all_snapshot_checks_pass']

def test_http_200_does_not_prove_same_ticket(saved):
    next(x for x in saved['http_log'] if x['kind']=='submit-duplicate')['response']['ticket_no']='OTHER'
    assert not audit(saved)['checks']['same_ticket_on_repeat']
