"""Fail-closed, read-only execution admission. Never restores or repairs a browser.

The three-second budget is shared by control socket setup and commands in one
snapshot, NOT local process/config or HTTP discovery. Existing worker bounds
remain authoritative; visibility is admission evidence, not a liveness promise.
"""
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from academic_engine import Halt


class BlockedExecutionMode(Halt):
    def __init__(self, reason='unknown_state'):
        super().__init__('BLOCKED_EXECUTION_MODE')
        allowed={'unknown_state','identity_or_route','target_mismatch','window_ineligible',
                 'document_ineligible','document_changed','control_unavailable','dispatch_unknown','stopped'}
        self.reason=reason if reason in allowed else 'unknown_state'


# UI eligibility only, NOT server authorization. Conservative Moodle 4.5 reference
# support: one connected navbar usermenu with unique, linked toggle/action IDs,
# and one exact same-origin logout action inside that menu. No theme fallback.
# No Promise, timers, requests, session secrets or page content returned. Logout
# validation reads only URL components below; never href/search/session keys.
METADATA = r'''(()=>{
const auth={known:false,user_menu:false,logout:false,not_logged_in:false,guest:false,login:false,challenge:false};
try {
if(!document.body || !document.body.classList)throw Error();
auth.not_logged_in=document.body.classList.contains('notloggedin')===true;
auth.guest=document.body.classList.contains('guestuser')===true ||
!!document.querySelector('[data-user-is-guest="true"],.guestuser');
auth.login=/\/login\//.test(location.pathname) ||
!!document.querySelector('.usermenu .login, form[action*="/login/"]') ||
Array.from(document.querySelectorAll('a')).some(a=>a.protocol==='https:' && a.origin===location.origin &&
(a.pathname==='/login/index.php'||a.pathname==='/login/guest.php'));
auth.challenge=!!document.querySelector('input[type=password],#challenge-form,[name=cf-turnstile-response]') ||
/just a moment|access denied|verify you are human/i.test(document.title);
const menus=document.querySelectorAll('nav.navbar .usermenu');
const toggles=document.querySelectorAll('#user-menu-toggle');
const actions=document.querySelectorAll('#user-action-menu');
if(menus.length===1 && toggles.length===1 && actions.length===1){
const menu=menus[0],toggle=toggles[0],action=actions[0];
auth.user_menu=menu.isConnected===true && toggle.isConnected===true && action.isConnected===true &&
menu.contains(toggle) && menu.contains(action) && toggle.getAttribute('aria-controls')==='user-action-menu';
const links=action.querySelectorAll('a');
const logouts=Array.from(links).filter(a=>a.protocol==='https:' && a.origin===location.origin &&
a.pathname==='/login/logout.php' && a.username==='' && a.password==='');
auth.logout=auth.user_menu && logouts.length===1 && logouts[0].isConnected===true && menu.contains(logouts[0]);
}
auth.known=true;
} catch (_) {auth.known=false;}
return {origin:location.origin,path:location.pathname,
query_free:location.search==='',fragment_free:location.hash==='',
visibility:document.visibilityState,discarded:document.wasDiscarded,
ready:document.readyState,epoch:performance.timeOrigin,auth_evidence:auth,
authenticated:auth.known===true && auth.user_menu===true && auth.logout===true &&
auth.not_logged_in===false && auth.guest===false && auth.login===false && auth.challenge===false,
access:auth.known!==true || auth.login || auth.challenge};})()'''


def validate_document(meta, expected=None):
    auth=meta.get('auth_evidence') if type(meta) is dict else None
    if (type(auth) is not dict or any(auth.get(k) is not True for k in ('known','user_menu','logout'))
        or any(auth.get(k) is not False for k in ('not_logged_in','guest','login','challenge'))):
        raise BlockedExecutionMode('document_ineligible')
    if (type(meta) is not dict or meta.get('origin')!='https://spectrum.um.edu.my'
        or meta.get('visibility')!='visible' or meta.get('discarded') is not False
        or meta.get('ready')!='complete' or meta.get('authenticated') is not True
        or meta.get('access') is not False or type(meta.get('path')) is not str
        or meta.get('query_free') is not True or meta.get('fragment_free') is not True
        or not meta['path'].startswith('/') or '?' in meta['path'] or '#' in meta['path']
        or type(meta.get('epoch')) not in (int,float) or not math.isfinite(meta['epoch']) or meta['epoch']<=0):
        raise BlockedExecutionMode('document_ineligible')
    if expected is not None and any(meta[k]!=expected[k] for k in ('origin','path','epoch')):
        raise BlockedExecutionMode('document_changed')
    return meta




def source_expression(body, expected):
    import json
    return '(async()=>{'+r'''
const _executionMeta=()=>META;
const _executionExpected=EXPECTED;
const _executionReady=()=>{const m=_executionMeta(),a=m.auth_evidence;return !!a&&typeof a==='object'&&!Array.isArray(a)&&a.known===true&&a.user_menu===true&&a.logout===true&&a.not_logged_in===false&&a.guest===false&&a.login===false&&a.challenge===false&&m.origin===_executionExpected.origin&&m.path===_executionExpected.path&&m.epoch===_executionExpected.epoch&&m.query_free===true&&m.fragment_free===true&&m.visibility==='visible'&&m.discarded===false&&m.ready==='complete'&&m.authenticated===true&&m.access===false;};
let _executionChanged=false,_executionDispatched=false;
const _executionChange=()=>{if(!_executionReady())_executionChanged=true;};
const _executionPageHide=()=>{_executionChanged=true;};
const _executionDispatch=()=>{_executionChange();if(_executionChanged)return false;_executionDispatched=true;return true;};
const before=_executionMeta();
if(!_executionReady())return {execution:{before,after:before,changed:true},source_dispatched:false,value:{execution_blocked:true}};
document.addEventListener('visibilitychange',_executionChange);
window.addEventListener('pagehide',_executionPageHide);
try {
const value=await(async()=>{BODY})();
_executionChange();
return {execution:{before,after:_executionMeta(),changed:_executionChanged},source_dispatched:_executionDispatched,value};
} finally {
document.removeEventListener('visibilitychange',_executionChange);
window.removeEventListener('pagehide',_executionPageHide);
}
'''.replace('META',METADATA).replace('EXPECTED',json.dumps(expected,separators=(',',':'))).replace('BODY',body)+'})()'


class ExecutionGate:
    def __init__(self, *, endpoint, target_id, config_path, native_route):
        import academic_edge as edge
        self.native_route=native_route;self.edge=edge;self.endpoint=endpoint;self.config_path=config_path
        self.control_requests=0;self.browser=None;self.page=None;self.stopped=False
        self.document=None;self.frame=None;self.window_id=None
        try:
            if not re.fullmatch(r'[A-Fa-f0-9]{32}',target_id or ''):
                raise BlockedExecutionMode('target_mismatch')
            self.target_id=target_id
            self.configured=edge.configured_endpoint(config_path)
            self.pid,listener,exe,command=edge._process_identity()
            fake=dict(Browser='Edg/verified-pending',webSocketDebuggerUrl=self.configured if self.configured.startswith('ws:') else 'ws://127.0.0.1:9337/devtools/browser/pending')
            edge.validate_identity(endpoint=endpoint,configured=self.configured,version=fake,listener=listener,executable=exe,command=command)
            self._route()
            self.version=edge._metadata('/json/version')
            edge.validate_identity(endpoint=endpoint,configured=self.configured,version=self.version,listener=listener,executable=exe,command=command)
            candidates=[t for t in edge._metadata('/json/list') if t.get('id')==target_id and t.get('type')=='page' and self._origin(t.get('url',''))]
            if len(candidates)!=1 or candidates[0].get('webSocketDebuggerUrl')!='ws://127.0.0.1:9337/devtools/page/'+target_id:
                raise BlockedExecutionMode('target_mismatch')
            self.page_url=candidates[0]['webSocketDebuggerUrl']
            deadline=time.monotonic()+3
            self.browser=edge._WebSocket(self.version['webSocketDebuggerUrl'],control=True,deadline=deadline)
            self.snapshot(deadline=deadline)
        except BaseException as exc:
            self.close()
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
            if isinstance(exc,BlockedExecutionMode):raise
            raise BlockedExecutionMode('control_unavailable') from None
    @staticmethod
    def _origin(url):
        u=urlsplit(url)
        return u.scheme=='https' and u.netloc=='spectrum.um.edu.my' and not u.username and not u.password and not u.query and not u.fragment
    def _route(self):
        try:route=self.native_route
        except Exception:raise BlockedExecutionMode('identity_or_route') from None
        if route not in (self.configured, getattr(self,'version',{}).get('webSocketDebuggerUrl')):
            raise BlockedExecutionMode('identity_or_route')
    def guard(self):
        if self.stopped:raise BlockedExecutionMode('stopped')
        self._route()
        pid,listener,exe,command=self.edge._process_identity()
        if pid!=self.pid or self.edge.configured_endpoint(self.config_path)!=self.configured:
            raise BlockedExecutionMode('identity_or_route')
        self.edge.validate_identity(endpoint=self.endpoint,configured=self.configured,version=self.version,listener=listener,executable=exe,command=command)
    def _command(self,connection,method,params,deadline):
        if time.monotonic()>=deadline:raise BlockedExecutionMode('control_unavailable')
        self.control_requests+=1
        value=connection.control_command(method,params,deadline=deadline)
        if time.monotonic()>=deadline:raise BlockedExecutionMode('control_unavailable')
        return value
    def snapshot(self, *, deadline=None):
        try:
            self.guard()
            deadline=deadline if deadline is not None else time.monotonic()+3
            window=self._command(self.browser,'Browser.getWindowForTarget',dict(targetId=self.target_id),deadline)
            wid=window.get('windowId')
            if type(wid) is not int or wid<0 or window.get('bounds',{}).get('windowState') not in ('normal','maximized','fullscreen'):
                raise BlockedExecutionMode('window_ineligible')
            if self.window_id is not None and wid!=self.window_id:raise BlockedExecutionMode('target_mismatch')
            self.window_id=wid
            info=self._command(self.browser,'Target.getTargetInfo',dict(targetId=self.target_id),deadline).get('targetInfo',{})
            if info.get('targetId')!=self.target_id or info.get('type')!='page' or not self._origin(info.get('url','')):
                raise BlockedExecutionMode('target_mismatch')
            if self.page is None:self.page=self.edge._WebSocket(self.page_url,deadline=deadline)
            first=self._command(self.page,'Page.getFrameTree',{},deadline)['frameTree']['frame']
            metadata=self._command(self.page,'Runtime.evaluate',dict(expression=METADATA,awaitPromise=False,returnByValue=True,timeout=2000),deadline)
            if type(metadata) is not dict or 'exceptionDetails' in metadata:
                raise BlockedExecutionMode('unknown_state')
            meta=metadata['result']['value']
            last=self._command(self.page,'Page.getFrameTree',{},deadline)['frameTree']['frame']
            def identity(f):
                if not all(type(f.get(k)) is str and f[k] for k in ('id','loaderId','url')) or not self._origin(f['url']):
                    raise BlockedExecutionMode('document_ineligible')
                return (f['id'],f['loaderId'],f['url'])
            current=identity(first)
            if current!=identity(last) or info['url']!=last['url'] or (self.frame is not None and current!=self.frame):
                raise BlockedExecutionMode('document_changed')
            validate_document(meta,self.document)
            if urlsplit(last['url']).path!=meta['path']:raise BlockedExecutionMode('document_changed')
            self.document=dict(meta);self.frame=current
            return self.document
        except BaseException as exc:
            self.close()
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
            if isinstance(exc,BlockedExecutionMode):raise
            raise BlockedExecutionMode('control_unavailable') from None
    def close(self):
        self.stopped=True
        for connection in (self.page,self.browser):
            if connection is not None:
                try:connection.close()
                except Exception:pass
