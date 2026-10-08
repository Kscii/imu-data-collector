"""Apply a reviewed correction plan as agent drafts, never human acceptance.

The plan binds each candidate to its current immutable draft hash. Dry-run is the
 default; --apply writes new revisions via the same CAS service as the UI.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_temporal import TemporalReviewService
from imu_data_collector.synthetic_temporal_contract import (
    activity_rules_evidence, document_sha256, validate_document,
)


def revised(document, changes):
    result = deepcopy(document)
    for number, change in enumerate(changes):
        a, b = change['start_ns'], change['stop_ns']
        touched = [s for s in result['segments'] if s['start_ns'] < b and s['stop_ns'] > a]
        if not touched or touched[0]['start_ns'] != a or touched[-1]['stop_ns'] != b:
            raise ValueError('Replacement must cover complete existing intervals')
        notes = {n['note_id']: n for s in touched for n in s['notes']}
        for s in touched:
            if s['semantic_text'].strip():
                key = 'original-' + s['segment_id']
                notes[key] = dict(note_id=key, start_ns=s['start_ns'], stop_ns=s['stop_ns'], text='原语义：' + s['semantic_text'])
        new = []
        for j, p in enumerate(change['parts']):
            part = deepcopy(touched[0])
            code = p.get('code')
            part.update(segment_id=f'agent-rules-{number}-{j}', start_ns=p['start_ns'], stop_ns=p['stop_ns'],
                        primary_code=code, resolution='identified' if code else p.get('resolution', 'uncovered'),
                        reason=None if code else p['explanation'], review_state='pending',
                        pending_reasons=p.get('pending_reasons', ['class','boundary'] if code else ['vocabulary','boundary']),
                        fall_event_id=None, notes=list(notes.values()),
                        semantic_text='\n'.join(s['semantic_text'] for s in touched if s['semantic_text'].strip()))
            part['notes'].append(dict(note_id=f'agent-explanation-{number}-{j}', start_ns=p['start_ns'], stop_ns=p['stop_ns'], text='自动复核建议：' + p['explanation']))
            new.append(part)
        if new[0]['start_ns'] != a or new[-1]['stop_ns'] != b:
            raise ValueError('Replacement extent changed')
        result['segments'] = [s for s in result['segments'] if s['stop_ns'] <= a] + new + [s for s in result['segments'] if s['start_ns'] >= b]
    result['source_evidence'].append(activity_rules_evidence())
    result['source_evidence'].append({'kind':'agent-semantic-audit','actor':'codex-activity-audit','original_document_sha256':document_sha256(document),'changes':changes,'human_acceptance':False})
    return validate_document(result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.receipt.exists():
        raise ValueError('Receipt must be a new immutable file')
    plan=json.loads(args.plan.read_text())
    store=LocalFilesystemStore(args.store)
    service=TemporalReviewService(store,SimpleNamespace(prefix='synthetic-motion/prod/v1'))
    run=service.get_run(plan['run_id'])
    pending=[]
    for item in plan['cases']:
        entry=next(e for e in run['candidates'] if e['candidate']['candidate_id']==item['candidate_id'])
        prior,_=service.draft(run,entry)
        if not prior or prior['revision_sha256']!=item['expected_draft_sha256']:
            raise ValueError('Current user draft differs; re-audit before writing')
        doc=revised(prior['document'],item['changes']) if item['changes'] else prior['document']
        service.check_semantic_document(entry,doc)
        pending.append((entry,prior,doc,item))
    receipt={'schema':'imu.agent_semantic_audit.v1','run_id':run['run_id'],'applied':args.apply,'plan_sha256':document_sha256(plan),'cases':[]}
    for entry,prior,doc,item in pending:
        saved=service.save_draft(run,entry,{'document':doc,'expected_draft_sha256':prior['revision_sha256']},'codex-activity-audit') if args.apply and item['changes'] else prior
        receipt['cases'].append({'candidate_id':item['candidate_id'],'previous':prior,'after_document':doc,'current_revision_sha256':saved['revision_sha256'],'changed':bool(item['changes']),'human_review_written':False})
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    with args.receipt.open('x') as out:
        json.dump(receipt,out,ensure_ascii=False,indent=2)
    print(json.dumps({'applied':args.apply,'reviewed':len(pending),'changed':sum(bool(p[3]['changes']) for p in pending),'human_reviews_written':0,'receipt':str(args.receipt)}))


if __name__=='__main__':
    main()
