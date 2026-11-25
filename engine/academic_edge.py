"""Read adapter for an existing dedicated Microsoft Edge session. Stdlib only; NEVER launches a browser.

No cookies, Moodle tokens, sesskeys, arbitrary scripts, generic HTTP, mutations,
new tabs, discussion body GETs or authentication are exposed by this interface.
"""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit
from academic_moodle import ORIGIN, page_url, plugin_url
from academic_engine import Halt, verified_timezone

ENDPOINT = 'http://127.0.0.1:9337'
MAX_RESPONSE = 48*1024*1024


def validate_call(method, args):
    schemas = {
        'core_course_get_enrolled_courses_by_timeline_classification': {'classification','offset','limit','sort'},
        'core_courseformat_get_state': {'courseid'},
        'core_calendar_get_action_events_by_timesort': {'timesortfrom','timesortto','limitnum'},
        'mod_forum_get_discussion_posts': {'discussionid','sortby','sortdirection','includeinlineattachments'},
    }
    if method not in schemas or set(args) != schemas[method]:
        raise ValueError('AJAX method/arguments not allowlisted')
    def integer(name, low, high):
        v=args[name]
        if type(v) is not int or not low <= v <= high:
            raise ValueError('AJAX integer bound: '+name)
    if method == 'core_courseformat_get_state':
        integer('courseid',1,2147483647)
    elif method == 'mod_forum_get_discussion_posts':
        integer('discussionid',1,2147483647)
        if args['sortby']!='id' or args['sortdirection']!='ASC' or args['includeinlineattachments'] is not False:
            raise ValueError('Only fixed-order non-marking post reads allowed')
    elif method.startswith('core_course_get_'):
        if args['classification'] != 'all' or args['sort'] != 'fullname':
            raise ValueError('Only bounded enrolled-course read is allowed')
        integer('offset',0,1000); integer('limit',1,100)
    else:
        integer('timesortfrom',946684800,4102444800); integer('timesortto',946684800,4102444800); integer('limitnum',1,50)
        if not 0 < args['timesortto']-args['timesortfrom'] <= 180*86400:
            raise ValueError('Calendar range bound exceeded')


def validate_identity(*, endpoint, configured, version, listener, executable, command):
    if endpoint != ENDPOINT or listener != '127.0.0.1:9337':
        raise Halt('Exact loopback 127.0.0.1:9337 listener required')
    if sys.platform == 'win32':
        from windows_edge import attest, split_command
        try:
            roots = [os.environ.get(k) for k in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
            attest(executable, split_command(command), str(Path.home()), roots)
            from windows_io import Directory
            Directory(Path.home()/'.hermes'/'spectrum-edge', create=False).close()
        except (ValueError, OSError) as exc:
            raise Halt('Windows Edge executable/profile mismatch') from exc
    else:
        expected = str(Path.home()/'.hermes/spectrum-edge')
        edge = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
        if executable != edge:
            raise Halt('Listener is not the approved Microsoft Edge executable')
        # ps args leaves the executable path unquoted; flags are still unambiguous.
        flags = command[command.find(' --'):].strip() if ' --' in command else ''
        try:
            words = shlex.split(flags)
        except ValueError:
            raise Halt('Ambiguous Edge process command') from None
        dirs = [s for s in words if s.startswith('--user-data-dir=')]
        ports = [s for s in words if s.startswith('--remote-debugging-port=')]
        if dirs != ['--user-data-dir='+expected] or ports != ['--remote-debugging-port=9337']:
            raise Halt('Listener PID/data-dir/debug port mismatch')
    if not re.search(r'\bEdg(?:e)?/', version.get('Browser','')+' '+version.get('User-Agent','')):
        raise Halt('CDP metadata does not identify Microsoft Edge')
    ws = version.get('webSocketDebuggerUrl', '')
    if not re.fullmatch(r'ws://127\.0\.0\.1:9337/devtools/browser/[A-Za-z0-9-]+', ws):
        raise Halt('Unexpected debugger endpoint')
    if configured not in (ENDPOINT, ws):
        raise Halt('Configured browser.cdp_url is stale/different; explicit reapproval required')


def configured_endpoint(path=None):
    """Read only browser.cdp_url from config; never serialize configuration/secrets."""
    text = Path(path or Path.home()/'.hermes/config.yaml').read_text(encoding='utf-8-sig')
    # YAML allows unindented comments within a mapping. Stop at the next
    # actual top-level key, not an explanatory comment or blank line.
    section = re.search(r'^browser:[ \t]*\n((?:(?:[ \t]+[^\n]*|#[^\n]*|[ \t]*)\n)*)', text, re.M)
    match = re.search(r'^  cdp_url:\s*([^\n#]+)', section[1], re.M) if section else None
    if not match:
        raise Halt('browser.cdp_url missing; no fallback/auto-launch')
    return match[1].strip().strip('\"\'')


def _metadata(path):
    if path not in ('/json/version','/json/list'):
        raise ValueError('Discovery route not allowlisted')
    con = http.client.HTTPConnection('127.0.0.1',9337,timeout=3)
    try:
        con.request('GET',path); r=con.getresponse()
        if r.status != 200:
            raise Halt('Edge discovery unavailable; no launch/fallback')
        value=r.read(1024*1024+1)
        if len(value)>1024*1024:
            raise Halt('Edge metadata limit exceeded')
        return json.loads(value)
    finally:
        con.close()


def _process_identity():
    if sys.platform == 'win32':
        from windows_edge import process_identity
        try:
            return process_identity()
        except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
            raise Halt('Windows Edge listener could not be verified; no fallback') from exc
    p=subprocess.run(['/usr/sbin/lsof','-nP','-iTCP:9337','-sTCP:LISTEN','-Fpn'],capture_output=True,text=True,timeout=3)
    pids=re.findall(r'^p([0-9]+)$',p.stdout,re.M)
    listeners=re.findall(r'^n([^\n]+)$',p.stdout,re.M)
    if len(set(pids)) != 1 or not listeners or set(listeners) != {'127.0.0.1:9337'}:
        raise Halt('Missing/ambiguous/non-loopback Edge listener; no fallback')
    pid=pids[0]
    executable=subprocess.run(['/bin/ps','-p',pid,'-o','comm='],capture_output=True,text=True,timeout=3).stdout.strip()
    command=subprocess.run(['/bin/ps','-ww','-p',pid,'-o','command='],capture_output=True,text=True,timeout=3).stdout.strip()
    return pid, listeners[0], executable, command


class CDPTimeout(TimeoutError):
    """Fixed transport metadata only; never carries remote text or expressions."""
    def __init__(self, command_id, phase, elapsed_seconds):
        super().__init__('CDP transport timeout')
        self.command_id = command_id
        self.transport_phase = phase
        self.elapsed_seconds = elapsed_seconds


class _WebSocket:
    """Minimal masked RFC6455 client for loopback CDP; no reusable public transport."""
    def __init__(self, url, *, control=False, deadline=None):
        u=urlsplit(url)
        kind='browser' if control else 'page'
        if u.scheme != 'ws' or u.netloc != '127.0.0.1:9337' or not re.fullmatch(r'/devtools/'+kind+r'/[A-Za-z0-9-]+',u.path) or u.query or u.fragment:
            raise Halt('Page websocket outside exact approved Edge endpoint')
        self._control=control
        self.serial = 0
        self._started = time.monotonic()
        self._deadline = deadline if deadline is not None else self._started+25
        connect_timeout=min(15,self._deadline-self._started) if deadline is not None else 15
        if connect_timeout<=0:raise CDPTimeout(0,'handshake_send',0)
        self.sock=socket.create_connection(('127.0.0.1',9337),timeout=connect_timeout)
        if deadline is None:
            self._started=time.monotonic();self._deadline=self._started+25
        self._phase = 'handshake_send'
        try:
            key=base64.b64encode(os.urandom(16)).decode()
            self._remaining()
            self.sock.sendall(('GET '+u.path+' HTTP/1.1\r\nHost: 127.0.0.1:9337\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+'\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            self._remaining()
            self._phase = 'handshake_read'
            response=b''
            while not response.endswith(b'\r\n\r\n'):
                response += self._exact(1)
                if len(response)>8192:
                    raise Halt('Websocket handshake bound exceeded')
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if not response.startswith(b'HTTP/1.1 101 ') or b'sec-websocket-accept: '+accept.lower() not in response.lower():
                raise Halt('CDP websocket handshake rejected')
        except (socket.timeout, TimeoutError) as exc:
            self.close()
            if type(exc) is CDPTimeout:
                raise
            raise CDPTimeout(self.serial, self._phase, time.monotonic()-self._started) from None
        except BaseException:
            self.close()
            raise
    def _remaining(self):
        remaining = self._deadline-time.monotonic()
        if remaining <= 0:
            raise CDPTimeout(self.serial, self._phase, time.monotonic()-self._started)
        self.sock.settimeout(remaining)
    def _exact(self,n):
        out=bytearray()
        while len(out)<n:
            self._remaining()
            data=self.sock.recv(min(n-len(out),65536))
            self._remaining()
            if not data:
                raise Halt('CDP socket closed')
            out.extend(data)
        return bytes(out)
    def _send(self, data, opcode=1):
        mask=os.urandom(4); n=len(data)
        header=bytes([0x80|opcode])+(bytes([0x80|n]) if n<126 else bytes([0xfe])+struct.pack('!H',n) if n<65536 else bytes([0xff])+struct.pack('!Q',n))
        self._remaining()
        self.sock.sendall(header+mask+bytes(c^mask[i%4] for i,c in enumerate(data)))
        self._remaining()
    def _receive(self):
        data=bytearray()
        while True:
            a,b=self._exact(2); opcode=a&15; size=b&127
            if size==126: size=struct.unpack('!H',self._exact(2))[0]
            elif size==127: size=struct.unpack('!Q',self._exact(8))[0]
            if size+len(data)>MAX_RESPONSE or b&128:
                raise Halt('CDP response bound/protocol violation')
            payload=self._exact(size)
            if opcode==8: raise Halt('CDP connection closed')
            if opcode==9:
                self._send(payload,10); continue
            if opcode==10: continue
            if opcode not in (0,1): raise Halt('Unexpected CDP frame type')
            data.extend(payload)
            if a&128: return json.loads(data)
    def evaluate(self, expression):
        if getattr(self,'_control',False):
            raise ValueError('Control command not allowlisted')
        result=self._command('Runtime.evaluate',dict(expression=expression,awaitPromise=True,returnByValue=True,timeout=20000))
        return result['result'].get('value')
    def control_command(self, method, params, *, deadline):
        from academic_execution import METADATA
        browser=getattr(self,'_control',False)
        allowed=(method in ('Browser.getWindowForTarget','Target.getTargetInfo') and browser
                 and set(params)=={'targetId'} and type(params['targetId']) is str
                 and re.fullmatch(r'[A-Fa-f0-9]{32}',params['targetId']))
        allowed=allowed or (not browser and method=='Page.getFrameTree' and params=={})
        allowed=allowed or (not browser and method=='Runtime.evaluate' and params==dict(expression=METADATA,awaitPromise=False,returnByValue=True,timeout=2000))
        if not allowed:raise ValueError('Control command not allowlisted')
        return self._command(method,params,deadline=deadline)
    def _command(self, method, params, *, deadline=None):
        self.serial+=1
        message=dict(id=self.serial,method=method,params=params)
        self._started = time.monotonic()
        self._deadline = min(self._started+25,deadline) if deadline is not None else self._started+25
        self._phase = 'command_send'
        try:
            self._send(json.dumps(message,separators=(',',':')).encode())
            self._phase = 'command_read'
            for _ in range(1000):
                self._remaining()
                out=self._receive()
                self._remaining()
                # Both direct-page and browser-control transports are sessionless.
                # Presence matters even for null/empty sessions; bool/float IDs
                # must not alias the integer serial through Python equality.
                if (type(out) is not dict or 'sessionId' in out
                        or ('id' in out and type(out['id']) is not int)):
                    raise Halt('CDP response protocol violation')
                if out.get('id')!=self.serial: continue
                if 'error' in out or out.get('result',{}).get('exceptionDetails'):
                    raise Halt('CDP evaluation failed (details withheld to prevent secret export)')
                return out['result']
            raise Halt('CDP event limit exceeded')
        except BaseException as exc:
            # A failed command cannot safely reuse a partially consumed frame.
            self.close()
            if isinstance(exc, (socket.timeout, TimeoutError)) and type(exc) is not CDPTimeout:
                raise CDPTimeout(self.serial, self._phase, time.monotonic()-self._started) from None
            raise
    def close(self):
        self.sock.close()


# This prelude runs INSIDE the existing SPeCTRUM page; no secret crosses CDP.
_PRELUDE = r'''
if (location.origin !== 'https://spectrum.um.edu.my') return {halt:'Existing tab origin changed'};
if (/\/login\//.test(location.pathname) || document.querySelector('input[type=password],#challenge-form,[name=cf-turnstile-response]') || /just a moment|access denied|verify you are human/i.test(document.title)) return {halt:'Login/challenge detected'};
const origin=location.origin;
const safeURL=(value)=>{
 try { const u=new URL(value,origin); if(u.protocol!=='https:'||u.username||u.password||u.hash) return null;
 if(u.origin!==origin) return ((u.hostname==='forms.gle'||(u.hostname==='docs.google.com'&&u.pathname.startsWith('/forms/')))&&!u.search)?u.href:null;
 if(/^\/pluginfile\.php\/\d+\/(mod_(assign|quiz|forum|resource|folder|page)|assignsubmission_file)\/(introattachment|intro|content|attachment|submission_files)\//.test(u.pathname) && (!u.search||/^\?forcedownload=[01]$/.test(u.search))) return u.href;
 if(/^\/mod\/(assign|quiz|forum|resource|folder|page)\/view\.php$/.test(u.pathname)&&/^\?id=[1-9]\d*(&p=\d{1,2})?$/.test(u.search))return u.href;
 if(u.pathname==='/mod/forum/discuss.php'&&/^\?d=[1-9]\d*(&parent=\d+)?$/.test(u.search))return u.href;
 return null;
 }catch{return null;}
};
const cleanText=(s)=>{const d=new DOMParser().parseFromString(String(s??''),'text/html');d.querySelectorAll('script,style,form,input,button,template').forEach(n=>n.remove());return d.body.textContent.replace(/\s+/g,' ').trim();};
const inventoryURL=(value)=>{try{const u=new URL(value,origin);return u.origin===origin&&!u.hash&&!u.username&&!u.password&&/^\/mod\/[a-z][a-z0-9_]*\/view\.php$/.test(u.pathname)&&/^\?id=[1-9]\d*$/.test(u.search)?u.href:null;}catch{return null;}};
'''

_INLINE = r'''
const imageURL=node=>{
 if(node.matches('.userpicture,.icon,.emoji,.smallicon'))return null;
 const attrs=node.getAttribute('data-srcset')||node.getAttribute('srcset')||'';
 const choices=attrs.split(',').map(v=>v.trim().split(/\s+/)[0]).reverse();
 for(const raw of [node.getAttribute('data-src'),...choices,node.getAttribute('src')]){
  const safe=raw&&safeURL(raw);
  if(safe&&/\/pluginfile\.php\//.test(safe)&&/\.(?:jpe?g|png)(?:\?|$)/i.test(safe))return safe;
 }return null;
};
'''
_FETCH = r'''
const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),12000);
let bytes;
let mime;
let metadata={};
try {
 const headers={};if(typeof validators!=='undefined'&&validators){if(validators.etag)headers['If-None-Match']=validators.etag;if(validators.last_modified)headers['If-Modified-Since']=validators.last_modified;}
 if(!_executionDispatch())return {execution_blocked:true};
 const response=await fetch(url,{method:'GET',headers,credentials:'same-origin',redirect:'error',cache:'no-store',signal:controller.signal});
 metadata={etag:(response.headers.get('etag')||'').slice(0,512),last_modified:(response.headers.get('last-modified')||'').slice(0,512)};
 if(response.status===304){if(typeof validators==='undefined'||!validators)return {halt:'Unexpected conditional response'};return {not_modified:true,mime:response.headers.get('content-type')||'',metadata};}
 if(response.status===401||response.status===403)return {halt:'Access/login/challenge response'};
 if(response.redirected||response.url!==url)return {halt:'Unexpected response origin or redirect'};
 if(!response.ok)return {error:'Non-success source response'};
 mime=(response.headers.get('content-type')||'').split(';')[0].trim().toLowerCase();
 const length=Number(response.headers.get('content-length')||0);if(length>limit)return {error:'Response too large'};
 const reader=response.body.getReader();const parts=[];let total=0;
 while(true){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>limit){await reader.cancel();return {error:'Response too large'};}parts.push(value);}
 bytes=new Uint8Array(total);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
} catch {return {halt:'Redirect/network/timeout uncertainty; stop all source requests'};} finally{clearTimeout(timer);}
'''


_FORUM_CONTEXT = r'''
let forumAttrs='';
if(new URL(url).pathname==='/mod/forum/view.php'){
 if(doc.body.id!=='page-mod-forum-view')return {error:'Unexpected forum page identity'};
 const mid=new URL(url).searchParams.get('id');
 const lists=[...doc.querySelectorAll('#region-main [id^="discussion-list-"][data-cmid]')].filter(n=>!n.closest('#intro,.forumintro'));
 if(lists.length>1)return {error:'Ambiguous native forum list'};
 if(lists.length===1){
  const list=lists[0];if(list.getAttribute('data-cmid')!==mid)return {error:'Forum list module mismatch'};
  const ids=new Set();let invalid=false;
  for(const a of list.querySelectorAll('a[data-action="toggle"][data-forumid][data-discussionid]')){
   const fid=a.getAttribute('data-forumid'),did=a.getAttribute('data-discussionid');
   const row=a.closest('tr[data-region="discussion-list-item"]');
   if(!/^[1-9][0-9]{0,9}$/.test(fid)||!row||row.getAttribute('data-discussionid')!==did){invalid=true;break;}
   const canonical=[...row.querySelectorAll('a[href]')].some(link=>{try{const u=new URL(link.getAttribute('href'),url);return u.origin===origin&&u.pathname==='/mod/forum/discuss.php'&&u.searchParams.getAll('d').length===1&&u.searchParams.get('d')===did;}catch{return false;}});
   if(!canonical){invalid=true;break;}ids.add(fid);
  }
  const courses=new Set([...doc.querySelectorAll('nav[aria-label="Breadcrumb"] a[href],.breadcrumb a[href]')].flatMap(a=>{try{const u=new URL(a.getAttribute('href'),url);const id=u.searchParams.get('id');return u.origin===origin&&u.pathname==='/course/view.php'&&u.searchParams.getAll('id').length===1&&/^[1-9][0-9]{0,9}$/.test(id)?[id]:[];}catch{return [];}}));
  if(invalid||ids.size>1||courses.size>1)return {error:'Conflicting forum metadata'};
  if(ids.size===1&&courses.size===1)forumAttrs=' data-hermes-course-id="'+[...courses][0]+'" data-hermes-module-id="'+mid+'" data-hermes-forum-id="'+[...ids][0]+'"';
 }
}
'''


class EdgeTransport:
    fixture=False
    def __init__(self, *, endpoint=ENDPOINT, target_id=None, config_path=None, timezone_evidence=None, native_route=None):
        from academic_execution import ExecutionGate
        self.requests=0;self.unknown_dispatches=0;self.source_attempts=0
        self.endpoint=endpoint;self.config_path=config_path
        self._gate=ExecutionGate(endpoint=endpoint,target_id=target_id,config_path=config_path,native_route=native_route)
        self.target_id=self._gate.target_id
        self.pid=self._gate.pid;self.configured=self._gate.configured;self.version=self._gate.version
        try:
            self.timezone=verified_timezone(timezone_evidence)
            self.timezone_evidence=str(timezone_evidence) if timezone_evidence else None
            self.observed_at=None
            # Admission completes before a source transport socket is constructed.
            self._ws=_WebSocket(self._gate.page_url)
        except BaseException:
            self.close();raise
    @property
    def control_requests(self):
        return self._gate.control_requests
    def _guard(self):
        pid,listener,exe,command=_process_identity()
        if pid!=self.pid or configured_endpoint(self.config_path)!=self.configured:
            raise Halt('Edge PID/configuration changed; stop without fallback')
        validate_identity(endpoint=self.endpoint,configured=self.configured,version=self.version,listener=listener,executable=exe,command=command)
    def _evaluate(self, body):
        from academic_execution import BlockedExecutionMode, source_expression, validate_document
        try:
            expected=dict(self._gate.snapshot())
            self.source_attempts+=1
            try:
                envelope=self._ws.evaluate(source_expression(_PRELUDE+_INLINE+body,expected))
            except Exception:
                # Lost ACK cannot establish whether the page started a request.
                self.requests+=1;self.unknown_dispatches+=1
                raise BlockedExecutionMode('dispatch_unknown') from None
            if (type(envelope) is not dict or type(envelope.get('source_dispatched')) is not bool):
                self.requests+=1;self.unknown_dispatches+=1
                raise BlockedExecutionMode('dispatch_unknown')
            if envelope['source_dispatched']:self.requests+=1
            execution=envelope.get('execution')
            if type(execution) is not dict or execution.get('changed') is not False:
                raise BlockedExecutionMode('document_changed')
            validate_document(execution.get('before'),expected)
            validate_document(execution.get('after'),expected)
            self._gate.snapshot()
            if not envelope['source_dispatched']:
                raise BlockedExecutionMode('document_ineligible')
            value=envelope.get('value')
            if type(value) is not dict:raise BlockedExecutionMode('unknown_state')
            if value.get('halt'):raise Halt(value['halt'])
            if value.get('error'):raise OSError(value['error'])
            return value
        except BaseException:
            self.close()
            raise
    def call(self,method,**args):
        validate_call(method,args)
        body='const method='+json.dumps(method)+';const args='+json.dumps(args)+';'+r'''
const raw=await new Promise((resolve)=>{let settled=false;let timer;const finish=x=>{if(!settled){settled=true;clearTimeout(timer);resolve(x);}};timer=setTimeout(()=>finish({halt:'AJAX timeout; stop all source requests'}),12000);require(['core/ajax'],ajax=>{if(settled)return;if(!_executionDispatch()){finish({execution_blocked:true});return;}ajax.call([{methodname:method,args}])[0].then(data=>finish({data})).catch(()=>finish({halt:'Moodle AJAX failed; authentication or server state unverified'}));},()=>finish({error:'core/ajax unavailable'}));});
if(raw.error||raw.halt||raw.execution_blocked)return raw;
let d=raw.data; if(typeof d==='string'){try{d=JSON.parse(d);}catch{return {error:'Invalid course state JSON'};}}
if(method==='core_course_get_enrolled_courses_by_timeline_classification')return {data:{courses:(d.courses||[]).map(c=>({id:c.id,shortname:cleanText(c.shortname),fullname:cleanText(c.fullname),viewurl:c.viewurl,visible:c.visible,summary:cleanText(c.summary)})),nextoffset:d.nextoffset}};
if(method==='core_courseformat_get_state')return {data:{course:{id:d.course.id},section:(d.section||[]).map(s=>({id:s.id,number:s.number,title:cleanText(s.title),cmlist:s.cmlist||[]})),cm:(d.cm||[]).map(c=>({id:c.id,instance:c.instance,module:c.module,name:cleanText(c.name),url:inventoryURL(c.url),uservisible:c.uservisible,visible:c.visible,availabilityinfo:cleanText(c.availabilityinfo),groupmode:c.groupmode}))}};
if(method==='mod_forum_get_discussion_posts'){if((d.posts||[]).length>200)return {error:'Discussion post limit exceeded; incomplete'};return {data:{courseid:d.courseid,forumid:d.forumid,posts:(d.posts||[]).map(p=>{const t=document.createElement('template');t.innerHTML=String(p.message||'');return {id:p.id,discussionid:p.discussionid,subject:cleanText(p.subject),message:cleanText(p.message),timecreated:p.timecreated,timemodified:p.timemodified,isdeleted:p.isdeleted,unread:p.unread,inline_images:[...t.content.querySelectorAll('img')].map(imageURL).filter(Boolean),attachments:(p.attachments||[]).map(a=>({fileurl:safeURL(a.fileurl)})).filter(a=>a.fileurl)};})}};}
return {data:{events:(d.events||[]).map(e=>({id:e.id,name:cleanText(e.name),timesort:e.timesort,timestart:e.timestart,course:{id:e.course?.id}}))}};
'''
        return self._evaluate(body)['data']
    def page(self,url):
        page_url(url)  # discussion bodies intentionally fail BEFORE any CDP call.
        body='const url='+json.dumps(url)+';const limit=2*1024*1024;'+_FETCH+r'''
if(mime!=='text/html'&&mime!=='application/xhtml+xml')return {error:'Expected an HTML activity page; file redirects not followed'};
const text=new TextDecoder().decode(bytes);const doc=new DOMParser().parseFromString(text,'text/html');
if(doc.querySelector('input[type=password],#login,#challenge-form,[name=cf-turnstile-response]')||/just a moment|access denied|verify you are human|log in|login/i.test(doc.title))return {halt:'Fetched login/challenge detected'};
const region=doc.querySelector('#region-main');if(!region)return {error:'Missing activity main region; coverage incomplete'};
'''+_FORUM_CONTEXT+r'''
const out=doc.createElement('div');out.append(region.cloneNode(true));for(const date of doc.querySelectorAll('[data-region="activity-dates"],.activity-dates'))out.append(date.cloneNode(true));
// Preserve pagination pointers outside nav before stripping interactive chrome.
for(const a of region.querySelectorAll('nav a[rel="next"],nav a[aria-label="Next page"]')){const safe=safeURL(a.href);if(safe){const b=doc.createElement('a');b.href=safe;b.rel='next';b.textContent='Next page';out.append(b);}}
out.querySelectorAll('script,style,form,input,button,textarea,select,template,meta,link,nav').forEach(n=>n.remove());
for(const node of out.querySelectorAll('img')){const url=imageURL(node);if(url)node.setAttribute('src',url);else node.removeAttribute('src');}
for(const node of out.querySelectorAll('*')){for(const attr of [...node.attributes]){if(['href','src','data'].includes(attr.name)){const safe=safeURL(attr.value);if(safe)node.setAttribute(attr.name,safe);else node.removeAttribute(attr.name);}else if(!['id','class','data-region','rel','scope'].includes(attr.name)){node.removeAttribute(attr.name);}}}
return {html:'<html><head><title>'+cleanText(doc.title).replaceAll('&','&amp;').replaceAll('<','&lt;')+'</title></head><body'+forumAttrs+'>'+out.innerHTML+'</body></html>'};
'''
        return self._evaluate(body)['html']
    def download(self,url,*,max_bytes=32*1024*1024,validators=None):
        plugin_url(url)
        if type(max_bytes) is not int or not 1<=max_bytes<=32*1024*1024:
            raise ValueError('Attachment byte bound must be 1..32MiB')
        if validators is not None:
            if not isinstance(validators,dict) or not validators or set(validators)-{'etag','last_modified'}:
                raise ValueError('Only source cache validators allowed')
            if any(not isinstance(v,str) or not 0<len(v)<=512 or any(ord(c)<32 or ord(c)>126 for c in v) for v in validators.values()):
                raise ValueError('Invalid source cache validator')
        body='const url='+json.dumps(url)+';const limit='+str(max_bytes)+';const validators='+json.dumps(validators)+';'+_FETCH+r'''
if(['text/html','application/xhtml+xml','text/javascript','application/javascript','text/xml','application/xml','image/svg+xml'].includes(mime))return {halt:'Attachment returned active/login content'};
let binary='';for(let i=0;i<bytes.length;i+=16384)binary+=String.fromCharCode(...bytes.subarray(i,i+16384));
return {base64:btoa(binary),mime,size:bytes.length,metadata};
'''
        result=self._evaluate(body)
        self.last_download_metadata=result.get('metadata',{})
        if result.get('not_modified'):
            if not validators: raise Halt('Unsolicited conditional file response')
            return None,result['mime']
        data=base64.b64decode(result['base64'],validate=True)
        if len(data)!=result['size'] or len(data)>max_bytes: raise Halt('Binary transfer size mismatch')
        return data,result['mime']
    def close(self):
        if hasattr(self,'_ws'): self._ws.close()
        if hasattr(self,'_gate'): self._gate.close()
