"""Native Hermes plugin for bounded, read-only SPeCTRUM extraction."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
NAME = 'spectrum_extraction_prepare'
DESCRIPTION = ('Return the fixed, read-only SPeCTRUM visible-page extraction expression. '
               'Does not connect to a browser. Run only in the authorized existing SPeCTRUM tab.')
SCAN = 'spectrum_extraction_scan'
SCAN_DESCRIPTION = ('Discover enrolled SPeCTRUM courses or crawl explicitly selected course IDs. '
                    'Requires the existing approved signed-in Edge SPeCTRUM tab on macOS or Windows. '
                    'Local staging only; optional downloads require user request. Never auto-retry a halt.')


def prepare(args, **_):
    if args != {}:
        return json.dumps({'status': 'invalid_arguments'})
    return json.dumps({'status': 'ready', 'origin': 'https://spectrum.um.edu.my',
                       'expression': (ROOT / 'assets' / 'capture.js').read_text(encoding='utf-8'),
                       'coverage': 'visible_page_only'})


def scan(args, **_):
    if type(args) is not dict:
        return json.dumps({'status':'invalid_arguments','reason':'invalid_options'})
    target = args.get('target_id')
    if not isinstance(target, str) or not re.fullmatch(r'[A-Fa-f0-9]{32}', target):
        return json.dumps({'status':'invalid_arguments','reason':'invalid_target'})
    if sys.platform not in ('darwin', 'win32'):
        return json.dumps({'status':'blocked','reason':'live crawler requires macOS or native Windows Microsoft Edge'})
    try:
        from hermes_constants import get_hermes_home
        from tools.browser_session_cdp import get_cdp_override_raw
        route = get_cdp_override_raw()
        if not re.fullmatch(r'(?:http://127\.0\.0\.1:9337|ws://127\.0\.0\.1:9337/devtools/browser/[A-Za-z0-9-]+)', route or ''):
            return json.dumps({'status':'blocked','reason':'approved loopback Edge route not configured'})
    except (ImportError, OSError, ValueError):
        return json.dumps({'status':'blocked','reason':'native Hermes route unavailable'})
    import os
    import subprocess
    try:
        home = get_hermes_home()
        env = {k:os.environ[k] for k in ('HOME','USERPROFILE','TZ','LANG','LC_ALL','SystemRoot','WINDIR','COMSPEC',
                                         'ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA','TEMP','TMP') if k in os.environ}
        worker = subprocess.run([sys.executable, '-E', '-s', '-B', str(ROOT/'engine'/'portable_scan.py')],
                                input=json.dumps(dict(home=str(home), route=route, options=args)),
                                cwd=ROOT, env=env, capture_output=True, encoding="utf-8", timeout=180, check=False)
        if worker.returncode != 0:
            return json.dumps({'status':'unknown','reason':'worker_failed','retry_automatically':False})
        payload = json.loads(worker.stdout)
        return json.dumps(payload, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({'status':'unknown','reason':'worker_wall_time_limit','retry_automatically':False})
    except (OSError, ValueError, TypeError):
        return json.dumps({'status':'unknown','reason':'worker_result_unavailable','retry_automatically':False})


def register(ctx):
    def tool(name, description, handler, properties, required=None):
        params = {'type':'object','properties':properties,'additionalProperties':False}
        if required: params['required'] = required
        ctx.register_tool(name=name, toolset='spectrum-extraction', description=description,
                          schema={'name':name,'description':description,'parameters':params}, handler=handler)
    tool(NAME, DESCRIPTION, prepare, {})
    tool(SCAN, SCAN_DESCRIPTION, scan, {
        'action': {'type':'string','enum':['discover','scan']},
        'target_id': {'type':'string','description':'Exact verified SPeCTRUM tab ID in the approved session'},
        'course_ids': {'type':'array','items':{'type':'integer'},'minItems':1,'maxItems':16,'uniqueItems':True,'description':'Native course IDs explicitly in scope for a scan'},
        'max_operations': {'type':'integer','minimum':1,'maximum':300,'description':'Crawl source operation cap; default 120'},
        'body_limit': {'type':'integer','minimum':0,'maximum':30,'description':'Discussion body cap; default 10'},
        'material_limit': {'type':'integer','minimum':0,'maximum':30,'description':'Material detail cap; default 8'},
        'download_limit': {'type':'integer','minimum':0,'maximum':12,'description':'Download cap; default 0. Requires explicit user request'},
        'backlog_only': {'type':'boolean','description':'Refresh forums and teaching materials without reassessing assignments and quizzes'},
        'course_calendar': {'type':'boolean','description':'Opt in to supplemental action-calendar read, staged only for selected courses'},
    }, ['action','target_id'])
    ctx.register_skill('spectrum-extraction', ROOT / 'skills' / 'spectrum-extraction' / 'SKILL.md')
