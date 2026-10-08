from copy import deepcopy
from pathlib import Path
import runpy

from imu_data_collector.synthetic_temporal_contract import empty_document, semantic_draft

revise = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/revise-semantic-drafts.py'))['revised']


def test_agent_replacement_retains_original_semantics_and_never_confirms():
    doc = semantic_draft(empty_document(candidate_id='clip', version_id='a'*64,
        commit_sha256='b'*64, sample_count=50, source_start_ns=0,
        input_hashes={k:'c'*64 for k in ('motion','sensors','selection')}))
    doc['segments'][0]['semantic_text']='用户的原始描述'
    original=deepcopy(doc)
    result=revise(doc,[{'start_ns':0,'stop_ns':2_000_000_000,'parts':[
        {'start_ns':0,'stop_ns':1_000_000_000,'code':'get_up','explanation':'建议起身'},
        {'start_ns':1_000_000_000,'stop_ns':2_000_000_000,'code':'standing','explanation':'建议站立'}]}])
    assert doc==original
    assert all(s['review_state']=='pending' for s in result['segments'])
    assert all(s['semantic_text']=='用户的原始描述' for s in result['segments'])
    assert result['segments'][1]['notes'][0]['stop_ns']==2_000_000_000
    assert result['source_evidence'][-1]['human_acceptance'] is False
