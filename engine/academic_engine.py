"""Code-first academic scan/stage/commit. Routine execution makes zero model calls."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import time
from academic_moodle import (MODULES, ORIGIN, page_url, parse_courses,
                             parse_activities, parse_page, parse_forum_posts, positive)

LIVE = 'spectrum.um.edu.my:live'
FIXTURE = 'spectrum.um.edu.my:offline_fixture'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class Halt(RuntimeError):
    """Authentication/challenge/routing failure: stop the ENTIRE scan, do not retry."""




def verified_timezone(path):
    if not path:
        return None
    evidence = json.loads(Path(path).read_text())
    value = evidence.get('timezone', {})
    if value.get('label') == 'Server timezone (Asia/Kuala_Lumpur)' and value.get('value') == '99' and evidence.get('observed_at'):
        return 'Asia/Kuala_Lumpur'
    return None






def _forum_page_identity(value, url, activity):
    """Read transport-generated outer body metadata, never arbitrary post content."""
    from academic_moodle import Document,positive
    bodies=[n for n in Document(value).root.walk() if n.tag=='body']
    names=('data-hermes-course-id','data-hermes-module-id','data-hermes-forum-id')
    if not any(any(k in b.attrs for k in names) for b in bodies):return None
    if len(bodies)!=1:raise ValueError('Ambiguous forum page metadata')
    cid,mid,fid=(str(positive(bodies[0].attrs.get(k))) for k in names)
    if cid!=activity['native_course_id'] or mid!=activity['native_module_id']:raise ValueError('Forum page course/module mismatch')
    if activity.get('instance_id') is not None and str(activity['instance_id'])!=fid:raise ValueError('Forum page/inventory instance mismatch')
    if page_url(url).split('&')[0]!=activity['source_url']:raise ValueError('Forum page source mismatch')
    return dict(native_course_id=cid,native_module_id=mid,forum_instance_id=fid,source_url=activity['source_url'],method='authenticated_forum_list')






def validate_stage(stage):
    """Validate evidence class AND every envelope before beginning a transaction.

    This is a consistency boundary for trusted local producers, not a cryptographic
    claim that arbitrary caller-forged JSON was obtained from Moodle.
    """
    from academic_moodle import positive,validate_activity_identity
    if type(stage.get('fixture')) is not bool or stage.get('namespace')!=(FIXTURE if stage['fixture'] else LIVE):
        raise ValueError('Stage namespace/evidence class mismatch')
    fixture=stage['fixture']
    def reject_markers(value):
        if isinstance(value,dict):
            if not fixture and (value.get('fixture') or value.get('verification')=='offline_fixture'):
                raise ValueError('Offline evidence marker in live stage')
            for v in value.values(): reject_markers(v)
        elif isinstance(value,list):
            for v in value:reject_markers(v)
    reject_markers(stage['records']);reject_markers(stage['tasks'])
    seen=set(); courses=set(); activities={}; discussions={}
    for r in stage['records']:
        kind=r['record_type'];nid=str(positive(r['native_id']));cid=str(positive(r['course_id']));p=r['payload']
        if (kind,nid) in seen or kind not in ('course','activity','discussion','post') or type(r['complete']) is not bool:
            raise ValueError('Duplicate or unsupported staged record')
        seen.add((kind,nid))
        for key in ('attempted_at','verified_at'):
            if r.get(key) is not None and datetime.fromisoformat(r[key].replace('Z','+00:00')).tzinfo is None:
                raise ValueError('Staged verification timestamp lacks timezone')
        if r['complete'] and not r.get('verified_at'):raise ValueError('Complete record needs real verification time')
        if fingerprint(p)!=r['fingerprint']: raise ValueError('Staged fingerprint mismatch')
        if kind=='course':
            if p['native_course_id']!=nid or cid!=nid or p['source_url']!=ORIGIN+'/course/view.php?id='+nid:
                raise ValueError('Staged course identity mismatch')
            courses.add(cid)
        elif kind=='activity':
            validate_activity_identity(p)
            if p['native_module_id']!=nid or p['native_course_id']!=cid:raise ValueError('Staged activity envelope mismatch')
            identity=(p.get('detail') or {}).get('forum_identity')
            if identity is not None or p.get('instance_source')=='authenticated_forum_list':
                expected=dict(native_course_id=cid,native_module_id=nid,forum_instance_id=p.get('instance_id'),source_url=p['source_url'],method='authenticated_forum_list')
                if p['module_type']!='forum' or identity!=expected:raise ValueError('Staged forum page identity mismatch')
            activities[nid]=r
            for d in (p.get('detail') or {}).get('discussions',[]):
                did=str(positive(d['discussion_id']))
                instance=p.get('instance_id')
                binding=(cid,nid,str(positive(instance)) if instance is not None else None)
                if p['module_type']!='forum' or 'forum_instance_id' not in d or (d.get('native_module_id'),d.get('forum_instance_id'))!=binding[1:]:
                    raise ValueError('Staged discussion forum binding mismatch')
                if instance is None and (r['complete'] or r['verified_at'] or d.get('body_status')=='read_api_no_marking'):
                    raise ValueError('Verified forum detail requires a positive instance')
                if did in discussions and discussions[did]!=binding:raise ValueError('Ambiguous discussion forum')
                discussions[did]=binding
    for r in stage['records']:
        kind=r['record_type'];p=r['payload'];nid=r['native_id'];cid=r['course_id']
        if cid not in courses:raise ValueError('Record has no staged course')
        binding=(cid,p.get('native_module_id'),p.get('forum_instance_id'))
        if kind in ('discussion','post') and ('forum_instance_id' not in p or
                (p['forum_instance_id'] is None and (kind=='post' or r['complete'] or r['verified_at']))):
            raise ValueError('Staged forum binding missing or unverified')
        if kind=='discussion' and (p['discussion_id']!=nid or discussions.get(nid)!=binding or p['source_url']!=ORIGIN+'/mod/forum/discuss.php?d='+nid):
            raise ValueError('Staged discussion identity mismatch')
        if kind=='post' and (p['native_post_id']!=nid or discussions.get(p['discussion_id'])!=binding or p['source_url']!=ORIGIN+'/mod/forum/discuss.php?d='+str(positive(p['discussion_id']))+'#p'+nid):
            raise ValueError('Staged post identity mismatch')
    for t in stage['tasks']:
        if t.get('fixture') is not fixture or t.get('source')!=LIVE:
            raise ValueError('Task evidence class mismatch')
        if fixture:
            if t.get('live_verified') or t.get('document_verified') or t.get('verification')!='offline_fixture':
                raise ValueError('Fixture cannot carry live verification')
        elif t.get('live_verified') is not True or t.get('verification')!='authenticated_live_page':
            raise ValueError('Live stage lacks authenticated page observation')
        r=activities.get(t['record_id']);p=r['payload'] if r else {}
        if not r or not r['complete'] or p.get('module_type') not in ('assign','quiz') or any(t.get(k)!=p.get(k) for k in ('native_module_id','native_course_id','module_type','source_url')):
            raise ValueError('Task is not bound to a verified native assessment')
    return stage


