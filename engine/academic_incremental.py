"""Opt-in incremental-first scanner. Canonical records stay in the existing Store.

Checkpoints are checksummed, atomic, short-lived job state, never accepted source
cache. Both scan modes verify discovery and forum indexes; only the normal
mode verifies assessment detail and the supplemental calendar.
Index equality never proves that an old discussion body has not been edited.
"""
from collections import Counter
from datetime import datetime
import copy
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from academic_engine import (FIXTURE, LIVE, Halt, _forum_page_identity,
                             fingerprint, validate_stage)
from academic_moodle import (MODULES, ORIGIN, parse_courses,
                             parse_activities, parse_page, parse_forum_posts, positive)
from academic_files import _directory, _read_at, close_directory, replace_bytes

POLICY='incremental-first-v2'
MAX_CHECKPOINT_BYTES=32*1024*1024


def _local_path(path):
    """Preflight every component; actual I/O also uses no-follow dirfd opens."""
    path=Path(path)
    if '..' in path.parts:raise ValueError('Unsafe path component')
    path=Path(os.path.abspath(path))
    for component in [*reversed(path.parents),path]:
        try:mode=component.lstat().st_mode
        except FileNotFoundError:continue
        if stat.S_ISLNK(mode) or (component!=path and not stat.S_ISDIR(mode)):
            raise ValueError('Unsafe path component')
        if component==path and not stat.S_ISREG(mode):raise ValueError('Regular file required')
    return path


def _checkpoint_data(path):
    fd=_directory(path.parent,create=False)
    try:return json.loads(_read_at(fd,path.name))
    finally:close_directory(fd)


class OperationLimit(Exception):
    pass


# Moodle puts its changing countdown in the extracted assignment status table.
# Match only the rendered status-row shape, not arbitrary instructions or dates.
_COUNTDOWN_DURATION = r'\d+ (?:years?|days?|hours?|mins?|minutes?|secs?|seconds?)(?:,? \d+ (?:years?|days?|hours?|mins?|minutes?|secs?|seconds?))*'
_ASSIGN_COUNTDOWN = re.compile(
    rf'(\bTime remaining\s+)(?:{_COUNTDOWN_DURATION} remaining|Assignment is overdue by:\s*{_COUNTDOWN_DURATION})(?=\s+Last modified\b)')


def semantic(value):
    """Only content, deadlines, submissions and binding drive reasoning work."""
    noise={'unread_observed','timecreated','timemodified','body_status','detail_status',
           'next_page','reference_text','forum_identity','instance_source','body_verified_at'}
    if isinstance(value,dict):
        out={k:semantic(v) for k,v in value.items() if k not in noise}
        detail=out.get('detail')
        if out.get('module_type')=='assign' and isinstance(detail,dict) and isinstance(detail.get('text'),str):
            detail['text']=_ASSIGN_COUNTDOWN.sub(r'\1[countdown]',detail['text'])
        return out
    if isinstance(value,list):return [semantic(v) for v in value]
    return value


class Checkpoint:
    def __init__(self,path,namespace,store,old,stamp,reset=False,input_identity=None):
        self.path=_local_path(path);self.stamp=stamp
        production=Path(__file__).resolve().parent/'data'
        if self.path.is_symlink() or self.path.suffix!='.json' or production in self.path.resolve().parents or self.path.resolve()==store.path.resolve():
            raise ValueError('Unsafe checkpoint destination')
        self.binding=dict(policy=POLICY,namespace=namespace,store=fingerprint(str(store.path.resolve())),
                          accepted=fingerprint(sorted((k,r['fingerprint'],r.get('verified_at')) for k,r in old.items())),input=input_identity)
        self.state='new';self.data=dict(binding=self.binding,job=uuid.uuid4().hex,created_at=stamp,finished=False,work={},rotation={},round=0,coverage={})
        if self.path.exists() and not reset:
            envelope=_checkpoint_data(self.path);saved=envelope['data']
            if envelope['checksum']!=fingerprint(saved) or not isinstance(saved['work'],dict) or not isinstance(saved['rotation'],dict):raise ValueError('Checkpoint corruption')
            age=(datetime.fromisoformat(stamp)-datetime.fromisoformat(saved['created_at'])).total_seconds()
            if saved['binding']==self.binding and 0<=age<=6*3600:
                self.data=saved;self.state='resume' if not saved['finished'] else 'rotation_only'
                if saved['finished']:self.data.update(work={},job=uuid.uuid4().hex)
            else:
                # Rotation is scheduling state, not evidence: preserve only with same
                # policy/namespace/store, even after acceptance or age invalidation.
                if all(saved['binding'].get(k)==self.binding[k] for k in ('policy','namespace','store')):
                    self.data['rotation']=saved['rotation'];self.data['round']=saved['round']
                self.state='expired_fresh_start' if not 0<=age<=6*3600 else 'incompatible_fresh_start'
        self.data.update(binding=self.binding,finished=False)
        if self.state!='resume':self.data['created_at']=stamp
        self.data['round']+=1
        self.save()
    def save(self):
        encoded=json.dumps(dict(checksum=fingerprint(self.data),data=self.data),sort_keys=True).encode()
        if len(encoded)>MAX_CHECKPOINT_BYTES:raise ValueError('Checkpoint size')
        directory=_directory(self.path.parent)
        try:
            replace_bytes(directory, self.path.name, encoded)
        finally:
            close_directory(directory)
    def cached(self,key,signature):
        row=self.data['work'].get(key)
        # Equal weak metadata is not evidence of unchanged bodies at a later observation.
        return copy.deepcopy(row) if row and row['signature']==signature and row['verified_at']==self.stamp else None
    def remember(self,key,signature,value,stamp):
        self.data['work'][key]=dict(signature=signature,value=value,verified_at=stamp)
        self.data['rotation'][key]=self.data['round'];self.save()

    @classmethod
    def finish(cls,path,job):
        """Only the supervisor calls this after authoritative successful exit."""
        cp=cls.__new__(cls);cp.path=_local_path(path)
        envelope=_checkpoint_data(cp.path);cp.data=envelope['data']
        if envelope['checksum']!=fingerprint(cp.data) or cp.data.get('job')!=job:
            raise ValueError('Checkpoint job mismatch')
        cp.data.update(finished=True,work={});cp.save()


def run_incremental(transport, store, *, checkpoint, now=None, max_operations=300, verify_limit=10, material_limit=8, body_limit=10, reset=False, defer_checkpoint_finish=False, course_ids, backlog_only=False, calendar=False):
    for value,upper in ((max_operations,1000),(verify_limit,200),(material_limit,200),(body_limit,200)):
        if type(value) is not int or not 0<=value<=upper:raise ValueError('Operation bounds')
    if max_operations<1:raise ValueError('Operation bounds')
    started=time.monotonic();initial_requests=transport.requests
    stamp=now or (transport.observed_at if transport.fixture else datetime.now().astimezone().isoformat())
    if datetime.fromisoformat(stamp).tzinfo is None:raise ValueError('Offset required')
    namespace=FIXTURE if transport.fixture else LIVE
    old={(r['record_type'],r['native_id']):r for r in store.scan_records(namespace)}
    # Fixture source content is immutable evidence except for synthetic fault
    # scheduling. A timestamp/weak index alone never identifies the input job.
    # Live sources offer no immutable snapshot identity: fail safe to fresh reads.
    input_identity=(fingerprint([getattr(transport,'fixture_path',None),
                    {k:v for k,v in transport.data.items() if k!='faults'}])
                    if transport.fixture and isinstance(getattr(transport,'data',None),dict)
                    else uuid.uuid4().hex)
    cp=Checkpoint(checkpoint,namespace,store,old,stamp,reset,input_identity)
    stage=dict(version=1,fixture=transport.fixture,namespace=namespace,mode='backlog-only' if backlog_only else 'incremental-first',observed_at=stamp,
               records=[],tasks=[],courses=[],failures=[],changes=[],status='success',model_calls=0,model_tokens=None,
               llm_queue=[],attachments=[],committed=False,verification_queue=[],critical_failures=[],removal_evidence=[],
               cache_state='accepted_scan_records' if old else 'cold_no_accepted_scan_records',checkpoint_state=cp.state,checkpoint_job=cp.data['job'])
    counts=Counter();activities=[];forums=[];discussions=[];coverage={};processed=set();unresolved=set();exhausted=False;changed=[];calendar_complete=False
    def fail(scope,reason,critical=False):
        row=dict(scope=scope,reason=reason);stage['failures'].append(row)
        if critical:stage['critical_failures'].append(row)
    def request(fn,*args,**kw):
        if counts['source_operations']>=max_operations:raise OperationLimit()
        counts['source_operations']+=1
        return fn(*args,**kw)
    def previous_payload(kind,nid):
        row=old.get((kind,str(nid)))
        return json.loads(row['payload']) if row else None
    def add(kind,nid,cid,payload,invfp=None,complete=True,verified=stamp,attempted=stamp):
        previous=old.get((kind,str(nid)));fp=fingerprint(payload)
        change='new' if not previous else 'unchanged' if previous['fingerprint']==fp else 'changed'
        counts[change]+=1
        record=dict(record_type=kind,native_id=str(nid),course_id=cid,payload=payload,fingerprint=fp,
                    inventory_fingerprint=invfp or fp,complete=complete,attempted_at=attempted,verified_at=verified)
        stage['records'].append(record)
        # Changes and the reasoning queue ignore polling and unread churn.
        if kind in ('activity','post') and semantic(previous_payload(kind,nid))!=semantic(payload):
            stage['changes'].append(dict(record_type=kind,native_id=str(nid),course_id=cid,change=change))
            if kind=='post' or payload.get('module_type') in ('assign','quiz'):
                stage['llm_queue'].append(dict(record_type=kind,native_id=str(nid),reason='New/changed content, deadline or submission'))
        return record
    def files(detail,a):
        for f in detail.get('files',[]):stage['attachments'].append(dict(f,native_module_id=a['native_module_id'],native_course_id=a['native_course_id']))
    def page(a):
        url=a['source_url']+('&forceview=1' if a['module_type']=='resource' and not transport.fixture else '')
        value=request(transport.page,url);detail=parse_page(value,a['source_url'],transport.timezone)
        if a['module_type']=='forum':
            identity=_forum_page_identity(value,a['source_url'],a)
            if identity:
                a.update(instance_id=identity['forum_instance_id'],instance_source='authenticated_forum_list');detail['forum_identity']=identity
            visited={a['source_url']}
            while detail['next_page']:
                nxt=detail['next_page']
                if nxt in visited or len(visited)>=20:raise ValueError('Forum page bound')
                visited.add(nxt);value=request(transport.page,nxt)
                if identity and _forum_page_identity(value,nxt,a)!=identity:raise ValueError('Forum context changed')
                other=parse_page(value,nxt,transport.timezone)
                detail['discussions']+=other['discussions'];detail['next_page']=other['next_page']
            seen=set()
            for d in detail['discussions']:
                if d['discussion_id'] in seen:raise ValueError('Duplicate discussion')
                seen.add(d['discussion_id']);d.update(native_module_id=a['native_module_id'],forum_instance_id=a.get('instance_id'),body_status='current_body_unverified')
        return detail
    def detail_record(a,d,verified=stamp):
        add('activity',a['native_module_id'],a['native_course_id'],dict(a,detail=d,detail_status='parsed'),fingerprint(a),verified=verified,attempted=verified)
        files(d,a)
    def scope(a,did=None):return a['native_course_id']+'/'+a['native_module_id']+('/'+did if did else '')
    def signature(a,d):return fingerprint([a,{k:v for k,v in d.items() if k!='body_status'}])
    def stale_posts(a,d):
        for (kind,nid),r in old.items():
            if kind=='post':
                p=json.loads(r['payload'])
                if (r['course_id'],p.get('native_module_id'),p.get('forum_instance_id'),p.get('discussion_id'))==(a['native_course_id'],a['native_module_id'],a.get('instance_id'),d['discussion_id']):
                    # Preserve the last actual body observation, not today's index time.
                    stage['records'].append(dict(r,payload=p,complete=bool(r['complete'])))
    def read_discussion(a,d):
        did=d['discussion_id'];key=scope(a,did);sig=signature(a,d)
        cached=cp.cached(key,sig)
        if cached:
            posts=cached['value']['posts'];deleted=cached['value']['deleted'];verified=cached['verified_at'];counts['resumed_details']+=1
        else:
            data=request(transport.call,'mod_forum_get_discussion_posts',discussionid=int(did),sortby='id',sortdirection='ASC',includeinlineattachments=False)
            if str(data.get('courseid'))!=a['native_course_id'] or positive(data.get('forumid'))!=positive(a.get('instance_id')):raise ValueError('Forum binding')
            posts=parse_forum_posts(data,did)
            # The shared parser intentionally excludes deleted bodies. Retain
            # that evidence here without changing the original full scanner.
            deleted=[str(positive(p['id'])) for p in data.get('posts',[]) if p.get('isdeleted')]
            for p in posts:
                # Retain body modification provenance but don't queue timestamp churn.
                p.pop('unread_observed',None);p.update(native_module_id=a['native_module_id'],forum_instance_id=a['instance_id'])
            verified=stamp;cp.remember(key,sig,dict(posts=posts,deleted=deleted),verified)
        for p in posts:add('post',p['native_post_id'],a['native_course_id'],p,verified=verified,attempted=verified);files(p,a)
        d['body_status']='read_api_no_marking';d['body_verified_at']=verified
        seen={p['native_post_id'] for p in posts}
        missing={}
        for (kind,nid),r in old.items():
            if kind!='post':continue
            p=json.loads(r['payload'])
            if (r['course_id'],p.get('native_module_id'),p.get('forum_instance_id'),p.get('discussion_id'))==(a['native_course_id'],a['native_module_id'],a.get('instance_id'),did) and nid not in seen:
                missing[nid]=(r,p)
        for nid in sorted(set(deleted)|set(missing)):
            evidence=dict(native_course_id=a['native_course_id'],native_module_id=a['native_module_id'],forum_instance_id=a['instance_id'],discussion_id=did,native_post_id=nid,
                          reason='explicit_deleted' if nid in deleted else 'absent_from_fresh_read',observed_at=verified,resolution='unresolved_history_retained')
            stage['removal_evidence'].append(evidence)
            if nid in missing:
                r,p=missing[nid];p['removal_evidence']=evidence
                stage['records'].append(dict(r,payload=p,fingerprint=fingerprint(p),complete=False,attempted_at=verified))
        if missing or deleted:
            unresolved.add(key);d['body_status']='read_api_removals_unresolved'
            fail('discussion:'+did,'Post deletion/absence observed; retained history is not current content',True)
            stage['verification_queue'].append(dict(native_course_id=a['native_course_id'],native_module_id=a['native_module_id'],discussion_id=did,freshness='post_removal_unresolved',reason='Fresh full-thread read omitted or explicitly deleted posts; historical bodies retained, not current'))
        processed.add(key)
    try:
        courses=[]
        for offset in range(0,1000,100):
            rows=parse_courses(request(transport.call,'core_course_get_enrolled_courses_by_timeline_classification',classification='all',offset=offset,limit=100,sort='fullname'))
            courses+=rows
            if len(rows)<100:break
        else:raise ValueError('Course pagination bound')
        ids=[c['native_course_id'] for c in courses]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate course')
        courses=[c for c in courses if int(c['native_course_id']) in course_ids]
        if set(course_ids)-{int(c['native_course_id']) for c in courses}:fail('courses','Requested courses not found in enrollment inventory',True)
        # Global barriers: all discovery, then all assessments, then all indexes.
        for c in courses:
            cid=c['native_course_id']
            if c['source_url']!=ORIGIN+'/course/view.php?id='+cid:raise ValueError('Course binding')
            stage['courses'].append(dict(id='spectrum.um.edu.my:course:'+cid,code=c['code'],occurrence='Authenticated course '+cid+'; personal occurrence/subgroup unresolved',url=c['source_url']))
            add('course',cid,cid,c);coverage[cid]=dict(discovery=False,assessments=False,forum_indexes=False,history=False)
            try:
                activities+=parse_activities(request(transport.call,'core_courseformat_get_state',courseid=int(cid)),cid);coverage[cid]['discovery']=True
            except Halt:raise
            except (ValueError,KeyError,OSError):fail('course:'+cid,'Course state unavailable',True)
            cp.data['coverage']=coverage;cp.save()
        for a in activities:
            if not a['available'] or a['module_type'] not in MODULES:
                add('activity',a['native_module_id'],a['native_course_id'],dict(a,detail=None,detail_status='unavailable_not_requested' if not a['available'] else 'unsupported_module_not_requested'),fingerprint(a),False,None)
        observed_assessments=set()
        for a in (a for a in activities if not backlog_only and a['available'] and a['module_type'] in ('assign','quiz')):
            try:
                d=page(a);detail_record(a,d)
                cp.remember(scope(a),fingerprint(a),d,stamp)
                observed_assessments.add(a['native_module_id'])
            except Halt:raise
            except (ValueError,KeyError,OSError):fail('module:'+a['native_module_id'],'Assessment unavailable',True)
        for cid in coverage:
            expected={a['native_module_id'] for a in activities if a['native_course_id']==cid and a['available'] and a['module_type'] in ('assign','quiz')}
            coverage[cid]['assessments']=not backlog_only and coverage[cid]['discovery'] and expected<=observed_assessments
        cp.data['coverage']=coverage;cp.save()
        for a in (a for a in activities if a['available'] and a['module_type']=='forum'):
            try:
                d=page(a);forums.append((a,d))
                for row in d['discussions']:discussions.append((a,row))
            except Halt:raise
            except (ValueError,KeyError,OSError):fail('forum:'+a['native_module_id'],'Forum index unavailable',True)
        for cid in coverage:
            coverage[cid]['forum_indexes']=coverage[cid]['discovery'] and not any(f['scope']=='forum:'+a['native_module_id'] for f in stage['failures'] for a in activities if a['native_course_id']==cid)
        cp.save()
        # Match the original supplemental calendar window/cap; never use it as inventory truth.
        try:
            if calendar and not backlog_only:
                dt=datetime.fromisoformat(stamp)
                events=request(transport.call,'core_calendar_get_action_events_by_timesort',timesortfrom=int(dt.timestamp())-14*86400,timesortto=int(dt.timestamp())+70*86400,limitnum=50)
                scoped = {int(c['native_course_id']) for c in courses}
                stage['calendar_events'] = [e for e in events.get('events',[]) if type(e.get('course',{}).get('id')) is int and e['course']['id'] in scoped]
                counts['calendar_events'] = len(stage['calendar_events'])
                calendar_complete=len(events.get('events',[]))<50
                if not calendar_complete:fail('calendar','Supplemental calendar capped at 50',True)
        except Halt:raise
        except (ValueError,KeyError,OSError):fail('calendar','Supplemental calendar unavailable',True)
        periodic=[]
        for a,d in discussions:
            prev=old.get(('discussion',d['discussion_id']))
            # Stable native identity + full sanitized row fingerprint triggers old
            # replies and visible edits. Missing edit metadata remains UNKNOWN.
            inv=signature(a,d)
            if not prev or prev['inventory_fingerprint']!=inv or cp.cached(scope(a,d['discussion_id']),inv):changed.append((a,d))
            else:periodic.append((a,d))
        periodic.sort(key=lambda row:(cp.data['rotation'].get(scope(row[0],row[1]['discussion_id']),0),int(row[0]['native_course_id']),int(row[1]['discussion_id'])))
        changed.sort(key=lambda row:(cp.data['rotation'].get(scope(row[0],row[1]['discussion_id']),0),int(row[0]['native_course_id']),int(row[1]['discussion_id'])))
        # Reserve one periodic slot during priority bursts so invisible edits cannot starve.
        reserve=1 if periodic and verify_limit and body_limit>1 else 0
        priority=changed[:max(0,body_limit-reserve)]
        selected=priority+periodic[:min(verify_limit,body_limit-len(priority))]
        for a,d in selected:
            try:read_discussion(a,d)
            except Halt:raise
            except (ValueError,KeyError,OSError):fail('discussion:'+d['discussion_id'],'Discussion unavailable')
        materials=[a for a in activities if a['available'] and a['module_type'] in MODULES-{'assign','quiz','forum'}]
        materials.sort(key=lambda a:(0 if not old.get(('activity',a['native_module_id'])) or old[('activity',a['native_module_id'])]['inventory_fingerprint']!=fingerprint(a) else 1,cp.data['rotation'].get(scope(a),0),int(a['native_module_id'])))
        for a in materials[:material_limit]:
            try:
                cached=cp.cached(scope(a),fingerprint(a))
                if cached:d=cached['value'];verified=cached['verified_at'];counts['resumed_details']+=1
                else:d=page(a);verified=stamp;cp.remember(scope(a),fingerprint(a),d,verified)
                detail_record(a,d,verified)
            except Halt:raise
            except (ValueError,KeyError,OSError):fail('module:'+a['native_module_id'],'Material unavailable')
        for a in materials[material_limit:]:
            fail('module:'+a['native_module_id'],'Material verification backlog')
    except OperationLimit:
        exhausted=True;fail('budget','Source operation cap reached; checkpoint retained',True)
    except Halt:
        stage.update(status='blocked',halted=True);fail('transport','Transport halted; no retry',True)
    except (ValueError,KeyError,OSError):
        stage.update(status='failed',halted=True);fail('inventory','Discovery unavailable',True)
    # Include every fresh index even if body budget expires midway. Never promote
    # index observation time to body verification time.
    for a,d in discussions:
        key=scope(a,d['discussion_id']);current=key in processed;verified=d.get('body_verified_at') if current else None
        inv=signature(a,{k:v for k,v in d.items() if k!='body_verified_at'})
        add('discussion',d['discussion_id'],a['native_course_id'],d,inv,current and key not in unresolved,verified)
        if not current:
            stale_posts(a,d)
            stage['verification_queue'].append(dict(native_course_id=a['native_course_id'],native_module_id=a['native_module_id'],discussion_id=d['discussion_id'],freshness='current_body_unverified',reason='Index does not expose reliable old-post edits; bounded rotating body verification required'))
    for a,d in forums:
        complete=all(scope(a,row['discussion_id']) in processed and scope(a,row['discussion_id']) not in unresolved for row in d['discussions']) and a.get('instance_id') is not None
        add('activity',a['native_module_id'],a['native_course_id'],dict(a,detail=d,detail_status='parsed' if complete else 'current_bodies_unverified'),fingerprint(a),complete,stamp if complete else None);files(d,a)
    visible_inventory_complete=bool(coverage) and all(c['discovery'] for c in coverage.values()) and not any(f['scope']=='courses' for f in stage['failures'])
    seen_activities={a['native_module_id'] for a in activities}
    seen_discussions={d['discussion_id'] for a,d in discussions}
    # Missing historical inventory is never interpreted as deletion or success.
    missing_activity=visible_inventory_complete and any(kind=='activity' and nid not in seen_activities for kind,nid in old)
    missing_course=any(kind=='course' and nid not in coverage for kind,nid in old)
    indexes_complete=bool(coverage) and all(c['forum_indexes'] for c in coverage.values())
    missing_discussion=indexes_complete and any(kind=='discussion' and nid not in seen_discussions for kind,nid in old)
    if missing_activity or missing_course or missing_discussion:fail('inventory','Previously observed inventory missing; retained, not deleted',True)
    priority_complete=all(scope(a,d['discussion_id']) in processed for a,d in changed)
    if stage['verification_queue']:fail('history','Current discussion-body verification backlog')
    if stage['status']=='success' and stage['failures']:stage['status']='partial'
    for cid in coverage:
        local_failures={'module:'+a['native_module_id'] for a in activities if a['native_course_id']==cid}
        local_failures.update('discussion:'+d['discussion_id'] for a,d in discussions if a['native_course_id']==cid)
        coverage[cid]['history']=not any(q['native_course_id']==cid for q in stage['verification_queue']) and coverage[cid]['forum_indexes'] and not exhausted and not any(f['scope'] in local_failures for f in stage['failures'])
    # Historical coverage is measured only within the currently enumerated index.
    # A known old body is not evidence of current freshness.
    indexed = {(a['native_course_id'],a['native_module_id'],d['discussion_id']):(a,d) for a,d in discussions}
    historically_verified = 0
    for (cid, mid, did), (a,d) in indexed.items():
        previous = old.get(('discussion',did)) or {}
        payload = json.loads(previous['payload']) if previous else {}
        if ((scope(a,did) in processed and scope(a,did) not in unresolved) or
            (previous.get('verified_at') and previous.get('complete') is True and previous.get('course_id')==cid
             and payload.get('native_module_id')==mid and payload.get('forum_instance_id')==a.get('instance_id'))):
            historically_verified += 1
    stage['inventory'] = activities
    stage['counts']=dict(counts, activities_indexed=len(activities), discussion_bodies_read=len(processed),
                         discussion_bodies_resolved=len(processed-unresolved), discussion_indexed=len(indexed),
                         discussion_historically_verified=historically_verified,
                         materials_unverified=sum(f['reason'] in ('Material verification backlog','Material unavailable') for f in stage['failures']))
    stage['coverage']=dict(visible_inventory_complete=visible_inventory_complete,daily_priority_complete=visible_inventory_complete and indexes_complete and calendar_complete and priority_complete and not stage['critical_failures'] and all(c['assessments'] for c in coverage.values()) and stage['status'] not in ('blocked','failed'),index_edit_metadata='unknown',supplemental_calendar_complete=calendar_complete,courses=coverage,current_bodies_complete=not stage['verification_queue'] and not exhausted and stage['status']=='success',academic_scope_complete=False,personal_occurrences='unresolved',off_platform='not_scanned')
    stage['request_count']=transport.requests;stage['source_operation_count']=transport.requests-initial_requests;stage['elapsed_seconds']=round(time.monotonic()-started,6)
    stage['workload']=dict(operation_cap=max_operations,periodic_discussion_cap=verify_limit,total_discussion_cap=body_limit,material_cap=material_limit,observed_source_operations=counts['source_operations'],runtime_estimate='unknown; operation caps are not a wall-time forecast')
    validate_stage(stage)
    cp.data['coverage']=coverage;cp.data['finished']=not defer_checkpoint_finish and not exhausted and stage['status'] not in ('blocked','failed')
    if cp.data['finished']:cp.data['work']={}
    cp.save()
    return stage






