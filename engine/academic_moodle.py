"""Deterministic SPeCTRUM normalization; no network or authentication.

Selected course/activity/file/page algorithms adapted from moodlekit client.py
87d36d7ad000a5fd52b779f0753956c3edd7be6a (Gabriel Werneck, MIT).
See THIRD_PARTY_NOTICES.md. No upstream auth, requests client or constructors.
"""
import html
from html.parser import HTMLParser
import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urljoin, parse_qs, unquote, urlencode
from zoneinfo import ZoneInfo

ORIGIN = 'https://spectrum.um.edu.my'
MODULES = frozenset(('assign', 'quiz', 'forum', 'resource', 'folder', 'page'))


def positive(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,9}', str(value)):
        raise ValueError('Expected a positive native ID')
    return int(value)


def page_url(url, *, discussion=False):
    u = urlsplit(url)
    if u.scheme != 'https' or u.netloc != 'spectrum.um.edu.my' or u.fragment or any(ord(c) < 33 for c in url):
        raise ValueError('Only exact same-origin academic URLs are allowed')
    q = parse_qs(u.query, keep_blank_values=True)
    if any(len(v) != 1 for v in q.values()):
        raise ValueError('Duplicate URL arguments')
    if discussion and u.path == '/mod/forum/discuss.php' and set(q) == {'d'}:
        positive(q['d'][0]); return url
    m = re.fullmatch(r'/mod/(assign|quiz|forum|resource|folder|page)/view\.php', u.path)
    allowed = {'id', 'p'} if m and m[1] == 'forum' else {'id','forceview'} if m and m[1] == 'resource' else {'id'}
    if not m or not q or 'id' not in q or set(q) - allowed:
        raise ValueError('Page route/arguments not allowlisted')
    positive(q['id'][0])
    if 'forceview' in q and q['forceview'] != ['1']:
        raise ValueError('Only resource forceview=1 is allowed')
    if 'p' in q and (not re.fullmatch(r'0|[1-9][0-9]?', q['p'][0]) or int(q['p'][0]) > 99):
        raise ValueError('Forum page out of bounds')
    return url


def inventory_url(url):
    """Preserve native references, NOT a permission to fetch these routes."""
    u=urlsplit(url)
    if u.scheme != 'https' or u.netloc != 'spectrum.um.edu.my' or u.fragment or any(ord(c)<33 for c in url):
        raise ValueError('Invalid inventory URL origin')
    if not re.fullmatch(r'/mod/[a-z][a-z0-9_]*/view\.php',u.path) or not re.fullmatch(r'id=[1-9][0-9]{0,9}',u.query):
        raise ValueError('Inventory URL is not a plain native module reference')
    return url


def plugin_url(url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.netloc != 'spectrum.um.edu.my' or u.fragment or any(ord(c) < 33 for c in url):
        raise ValueError('Attachment must be exact same-origin')
    # Only course material / assignment originals and observed submission files.
    if not re.fullmatch(r'/pluginfile\.php/[1-9][0-9]*/(?:mod_(?:assign|quiz|forum|resource|folder|page)|assignsubmission_file)/(?:introattachment|intro|content|attachment|submission_files)/.+', u.path):
        raise ValueError('Attachment route not allowlisted')
    decoded = unquote(unquote(u.path))
    if any(x in ('.', '..') for x in decoded.split('/')) or '\\' in decoded or any(ord(x) < 32 for x in decoded):
        raise ValueError('Unsafe attachment path')
    if u.query not in ('', 'forcedownload=1', 'forcedownload=0'):
        raise ValueError('Attachment arguments not allowlisted')
    return url


def parse_courses(data):
    # moodlekit courses(): preserve first-party identity and decode display strings.
    return [dict(native_course_id=str(positive(c['id'])), code=html.unescape(c['shortname']).strip(),
                 name=html.unescape(c['fullname']).strip(), source_url=c['viewurl'],
                 available=bool(c.get('visible', True)), occurrence=None,
                 occurrence_raw=plain(c.get('summary', '')), semester=None)
            for c in data['courses']]


def validate_activity_identity(activity):
    mid=str(positive(activity['native_module_id']));positive(activity['native_course_id'])
    module=activity['module_type'];url=inventory_url(activity['source_url'])
    if not re.fullmatch(r'[a-z][a-z0-9_]*',module) or url!=ORIGIN+'/mod/'+module+'/view.php?id='+mid:
        raise ValueError('Native activity identity does not match its URL/type')
    if activity.get('instance_id') is not None: positive(activity['instance_id'])
    return activity


def parse_activities(state, course_id):
    # moodlekit activities(): native cm mapping and ordered section cmlists.
    state = json.loads(state) if isinstance(state, str) else state
    if str(state['course']['id']) != str(course_id):
        raise ValueError('Course state identity mismatch')
    cms = {str(cm['id']): cm for cm in state['cm']}
    result = []
    seen = set()
    for sec in sorted(state['section'], key=lambda s: s['number']):
        for cmid in sec.get('cmlist', []):
            cm = cms.get(str(cmid))
            if not cm or not cm.get('url') or cm.get('module') == 'subsection':
                continue
            mid = str(positive(cm['id']))
            if mid in seen:
                raise ValueError('Duplicate native course module in inventory')
            seen.add(mid)
            result.append(validate_activity_identity(dict(native_course_id=str(positive(course_id)), native_module_id=mid,
                instance_id=str(positive(cm['instance'])) if cm.get('instance') else None,
                title=html.unescape(cm['name']).strip(), module_type=cm.get('module', ''),
                source_url=cm['url'], section=html.unescape(sec['title']).strip(),
                section_number=sec['number'], available=bool(cm.get('uservisible', True)),
                visible=bool(cm.get('visible', True)), groupmode=cm.get('groupmode'),
                restriction=plain(cm.get('availabilityinfo', '')))))
    return result


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []
    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()
    def text(self):
        return re.sub(r'\s+', ' ', ' '.join(c.text() if isinstance(c, Node) else c for c in self.children)).strip()
    def hasclass(self, value):
        return value in self.attrs.get('class', '').split()


class Document(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.root = Node(); self.current = self.root; self.skip = 0
        self.feed(value)
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'form', 'nav', 'template'):
            self.skip += 1
        if self.skip:
            return
        n = Node(tag, attrs, self.current); self.current.children.append(n)
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.current = n
    def handle_endtag(self, tag):
        if self.skip:
            if tag in ('script', 'style', 'form', 'nav', 'template'):
                self.skip -= 1
            return
        n = self.current
        while n.parent is not None:
            if n.tag == tag:
                self.current = n.parent; break
            n = n.parent
    def handle_data(self, data):
        if not self.skip:
            self.current.children.append(data)


def plain(value):
    return Document(str(value)).root.text()


def date_claim(raw, timezone):
    raw = re.sub(r'\s+', ' ', raw).strip().strip('.')
    formats = ('%A, %d %B %Y, %I:%M %p', '%d %B %Y, %I:%M %p', '%A, %d %B %Y, %H:%M', '%d %B %Y, %H:%M', '%Y-%m-%dT%H:%M:%S%z')
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                if not timezone:
                    return dict(raw=raw, value=None, certainty='unknown', reason='Source timezone unverified')
                dt = dt.replace(tzinfo=ZoneInfo(timezone))
            return dict(raw=raw, value=dt.isoformat(), certainty='exact')
        except ValueError:
            pass
    for fmt in ('%A, %d %B %Y', '%d %B %Y', '%Y-%m-%d'):
        try:
            claim = dict(raw=raw, value=datetime.strptime(raw, fmt).date().isoformat(), certainty='date')
            if timezone:
                claim['source_timezone'] = timezone
            return claim
        except ValueError:
            pass
    return dict(raw=raw, value=None, certainty='unknown', reason='Unrecognized date text')


DATE_LABELS = {'due': 'due_date', 'due date': 'due_date', 'cut-off date': 'cutoff_date', 'cutoff date': 'cutoff_date', 'cut-off': 'cutoff_date', 'extension due date': 'extension_date', 'extension': 'extension_date', 'opened': 'opening_date', 'opens': 'opening_date', 'closed': 'closing_date', 'closes': 'closing_date'}
LABEL_RE = re.compile(r'\b(Extension due date|Cut-off date|Cutoff date|Due date|Due|Opened|Opens|Closed|Closes):\s*', re.I)


def quiz_instruction_claims(text, timezone):
    """Explicit quiz availability range; a year may govern this range only."""
    token = r'\d{1,2}\s+[A-Za-z]+(?:\s+\d{4})?(?:\s*\(\s*\d{1,2}\s*[:.]\s*\d{2}\s*[ap]m\s*\))?'
    pattern = r'(?:\bquiz\s+will\s+be\s+open\s+from|\bDate\s*:)\s*(' + token + r')\s+until\s+(' + token + r')'
    claims = []
    for match in re.finditer(pattern, text, re.I):
        years = re.findall(r'\b\d{4}\b', match[0])
        shared_year = years[0] if len(set(years)) == 1 and match[1].split()[1].lower()==match[2].split()[1].lower() else None
        for field, raw in zip(('opening_date', 'closing_date'), match.groups()):
            normalized = raw
            if not re.search(r'\b\d{4}\b', raw) and shared_year:
                normalized = re.sub(r'^(\d{1,2}\s+[A-Za-z]+)', r'\1 ' + shared_year, normalized)
            normalized = re.sub(r'\s*\(\s*', ', ', normalized).replace(')', '')
            normalized = re.sub(r'\s*[:.]\s*(?=\d{2}\s*[ap]m)', ':', normalized, flags=re.I)
            normalized = re.sub(r'(?<=\d)\s*([ap]m)', r' \1', normalized, flags=re.I)
            claim = instruction_date_claim(normalized, timezone)
            claim.update(raw=raw, field=field, source_kind='lecturer_instruction')
            claims.append(claim)
    return claims


def instruction_date_claim(raw, timezone):
    expression = raw
    claim = date_claim(expression, timezone)
    if claim['certainty']=='unknown' and claim.get('reason') != 'Source timezone unverified':
        # Only a fully explicit date anchored immediately after the label.
        # Keep all original prose as evidence; do not search for dates in prose.
        date_token = r'(?:[A-Za-z]+,\s*)?\d{1,2}\s+[A-Za-z]+\s+\d{4}'
        time_token = r'(?:,\s*\d{1,2}:\d{2}(?:\s*[ap]m)?)?'
        match = re.match(r'^(' + date_token + time_token + r')(?=$|[\s.;])', raw, re.I)
        if match:
            tail = raw[match.end():]
            unsafe = re.search(r'\b(tentative|approximately|provisional|extended|postponed|instead|or|subject|confirmation|pending|not|may|might|could|unless|expected|TBC|TBD)\b|\d{1,2}\s+[A-Za-z]+\s+\d{4}', tail, re.I)
            # A timezone or unexplained suffix before the sentence terminator
            # is not prose. Accept only explicit instruction sentences, never
            # silently drop EST, numeric offsets, qualifiers or unknown text.
            instruction_tail = re.match(r'^\s*[.;]\s*(?:Please\s+)?(?:Submit|Upload|Use|Include|Attach|Name|Save|Ensure|Remember)\b', tail, re.I)
            if instruction_tail and not unsafe:
                expression = match[1]
                claim = date_claim(expression, timezone)
    if claim.get('reason')=='Source timezone unverified':
        calendar=expression.split(',')[0].strip()
        # A weekday prefix has its own comma, so strip only the time suffix.
        calendar = re.sub(r',\s*\d{1,2}:\d{2}.*$', '', expression)
        date_only=date_claim(calendar,None)
        if date_only['certainty']=='date':
            claim.update(value=date_only['value'],certainty='date',time_uncertain=True)
    claim['raw'] = raw
    return claim


def reconcile_dates(claims):
    """Reconcile instants; date-only compatibility uses explicit calendar provenance.

    With no calendar timezone, disagreeing local-day representations are
    conservatively ambiguous instead of letting input order choose a day.
    """
    valid = [c for c in claims if c.get('value', c.get('deadline')) and c.get('certainty') in ('date','exact')]
    if not valid:
        return None, 'unknown'
    exact = [(c.get('value', c.get('deadline')), datetime.fromisoformat(c.get('value', c.get('deadline')).replace('Z','+00:00')))
             for c in valid if c['certainty']=='exact']
    days = [c for c in valid if c['certainty']=='date']
    if exact:
        first, instant = min(exact, key=lambda item:item[0])
        if instant.tzinfo is None or any(t.tzinfo is None or t != instant for _,t in exact):
            return None, 'conflicting'
        zones = set()
        for claim in days:
            day = claim.get('value', claim.get('deadline'))
            zone = claim.get('source_timezone')
            if zone:
                zones.add(zone)
                compatible = instant.astimezone(ZoneInfo(zone)).date().isoformat() == day
            else:
                # A date has no instant or timezone. Never borrow a timezone
                # from an exact claim's serialization, even when it appears local.
                compatible = False
            if not compatible:
                return None, 'conflicting'
        if len(zones)==1:
            return instant.astimezone(ZoneInfo(next(iter(zones)))).isoformat(), 'exact'
        return first, 'exact'
    values = {c.get('value', c.get('deadline')) for c in days}
    return (next(iter(values)), 'date') if len(values)==1 else (None,'conflicting')


def instruction_text(node):
    """Exclude linked activities and navigation from lecturer date evidence."""
    if node.tag in ('a', 'nav', 'form') or node.attrs.get('role') == 'navigation':
        return ''
    return re.sub(r'\s+', ' ', ' '.join(instruction_text(c) if isinstance(c, Node) else c for c in node.children)).strip()


def in_navigation(node):
    while node:
        if node.tag in ('a', 'nav') or node.attrs.get('role') == 'navigation':
            return True
        node = node.parent
    return False


def quiz_attempt_states_from_text(value):
    """Only the explicit account attempt-summary layout, not prose mentioning Finished."""
    if not re.search(r'\bYour attempts\b',value,re.I): return []
    return [m.lower() for m in re.findall(r'\bAttempt\s+[1-9]\d*\s+summary\s+(?:Status|State)\s+(Finished|In progress|Never submitted|Overdue)\b',value,re.I)]


def inline_images(root, base):
    results = []
    seen = set()
    for node in root.walk():
        if node.tag != 'img' or any(node.hasclass(c) for c in ('userpicture', 'icon', 'emoji', 'smallicon')):
            continue
        attrs = node.attrs
        variants = attrs.get('data-srcset') or attrs.get('srcset') or ''
        choices = [part.strip().split()[0] for part in variants.split(',') if part.strip()]
        for raw in [attrs.get('data-src'), *reversed(choices), attrs.get('src')]:
            if not raw:
                continue
            url = urljoin(base, raw)
            try:
                plugin_url(url)
            except ValueError:
                continue
            if not re.search(r'\.(?:jpe?g|png)$', urlsplit(url).path, re.I):
                continue
            if url not in seen:
                results.append(url)
                seen.add(url)
            break
    return results


def parse_page(value, url, timezone=None):
    page_url(url, discussion=True)
    doc = Document(value).root
    nodes = list(doc.walk())
    main = next((n for n in nodes if n.attrs.get('id') == 'region-main'), doc)
    title = next((n.text() for n in nodes if n.tag == 'title'), '')
    mod = re.search(r'/mod/([^/]+)/', url)[1]
    result = dict(title=title, module_type=mod, text=main.text(), source_url=url, timezone=timezone,
        due_date=None, cutoff_date=None, quiz_open=None, quiz_close=None, extension_date=None,
        date_claims=[], deadline=None, deadline_certainty='unknown', source_submission_state='unknown',
        source_submission_scope='This authenticated account/activity only; group or external completion unresolved',
        submitted_files=[], files=[], external_requirements=[], discussions=[], next_page=None,quiz_attempt_states=[])
    claims = []
    # Header dates may sit OUTSIDE #region-main (stock moodlekit misses them).
    if mod in ('assign', 'quiz', 'page', 'forum'):
        images = inline_images(main, url)
        if images:
            result['inline_images'] = images
    date_nodes = [n for n in nodes if n.attrs.get('data-region') == 'activity-dates' or n.hasclass('activity-dates')]
    for n in date_nodes:
        text = n.text(); matches = list(LABEL_RE.finditer(text))
        for i, m in enumerate(matches):
            raw = text[m.end():matches[i+1].start() if i+1 < len(matches) else len(text)].strip()
            claims.append((DATE_LABELS[m[1].lower()], raw))
    for row in [n for n in nodes if n.tag == 'tr']:
        cells = [n for n in row.children if isinstance(n, Node) and n.tag in ('td', 'th')]
        if len(cells) >= 2:
            label, content = cells[0].text().strip(':').lower(), cells[1].text()
            if label in DATE_LABELS:
                claims.append((DATE_LABELS[label], content))
            if label == 'submission status':
                if 'submitted for grading' in content.lower():
                    result['source_submission_state'] = 'submitted'
                elif 'no submissions' in content.lower():
                    result['source_submission_state'] = 'no_submissions_reported'
                elif 'draft' in content.lower():
                    result['source_submission_state'] = 'draft'
                result['submission_status_raw'] = content
            if label == 'grading status':
                result['grading_status'] = content
            if mod == 'quiz' and label in ('status','state'):
                state=re.match(r'^(Finished|In progress|Never submitted|Overdue)\b',content,re.I)
                if state: result['quiz_attempt_states'].append(state[1].lower())
    if mod == 'quiz':
        for table in (n for n in nodes if n.tag=='table'):
            state_column=None
            for row in (n for n in table.walk() if n.tag=='tr'):
                cells=[n for n in row.children if isinstance(n,Node) and n.tag in ('td','th')]
                labels=[c.text().lower() for c in cells]
                if 'attempt' in labels and ('state' in labels or 'status' in labels):
                    state_column=labels.index('state' if 'state' in labels else 'status');continue
                if state_column is not None and state_column<len(cells):
                    state=re.match(r'^(Finished|In progress|Never submitted|Overdue)\b',cells[state_column].text(),re.I)
                    if state: result['quiz_attempt_states'].append(state[1].lower())
        result['quiz_attempt_states']+=quiz_attempt_states_from_text(main.text())
        result['quiz_attempt_states']=list(dict.fromkeys(result['quiz_attempt_states']))
        states=result['quiz_attempt_states']
        if 'in progress' in states:
            result['source_submission_state']='attempt_in_progress'
        elif 'finished' in states:
            result['source_submission_state']='attempt_finished'
    typed_claims = [dict(date_claim(raw, timezone), field=field, source_kind='configured_date') for field, raw in claims]
    if mod in ('quiz','assign'):
        for intro in (n for n in main.walk() if (n.attrs.get('id') in ('intro','quizintro') or n.hasclass('mod_introbox')) and not in_navigation(n)):
            text=instruction_text(intro)
            if mod=='quiz':typed_claims.extend(quiz_instruction_claims(text,timezone))
            labels=list(LABEL_RE.finditer(text))
            for i,m in enumerate(labels):
                raw=text[m.end():labels[i+1].start() if i+1<len(labels) else len(text)].strip()
                typed_claims.append(dict(instruction_date_claim(raw,timezone),field=DATE_LABELS[m[1].lower()],source_kind='lecturer_instruction'))
    for claim in typed_claims:
        field = claim['field']
        claim['source_url'] = url
        if claim not in result['date_claims']:
            result['date_claims'].append(claim)
        if claim['certainty'] == 'exact':
            target = ('quiz_open' if mod == 'quiz' else 'opening_date') if field == 'opening_date' else ('quiz_close' if mod == 'quiz' else 'closing_date') if field == 'closing_date' else field
            previous = result.get(target)
            if previous and previous != claim['value']:
                result.setdefault('date_conflicts', []).append(target)
            else:
                result[target] = claim['value']
    preferred = 'due_date' if mod != 'quiz' else 'closing_date'
    if mod == 'assign' and any(c['field']=='extension_date' and c['value'] for c in result['date_claims']):
        preferred = 'extension_date'
    result['deadline_basis'] = preferred
    dates = [c for c in result['date_claims'] if c['field'] == preferred]
    result['deadline'],result['deadline_certainty']=reconcile_dates(dates)
    result['deadline_source_kinds']=sorted({c['source_kind'] for c in dates if c.get('value')})
    seen_files, discussions = set(), {}
    for n in main.walk():
        raw = n.attrs.get('href') or n.attrs.get('data') if n.tag in ('a', 'object') else n.attrs.get('src') if n.tag in ('iframe', 'embed') else None
        if not raw:
            continue
        link = urljoin(url, html.unescape(raw)); u = urlsplit(link)
        if '/pluginfile.php/' in u.path:
            try:
                plugin_url(link)
            except ValueError:
                continue
            if link not in seen_files:
                entry = dict(name=unquote(u.path.rsplit('/', 1)[-1]), source_url=link)
                # A submitted original remains distinct from course materials.
                (result['submitted_files'] if '/assignsubmission_file/' in u.path else result['files']).append(entry)
                seen_files.add(link)
        elif u.scheme == 'https' and u.netloc in ('forms.gle', 'docs.google.com') and not u.username and ('/forms/' in u.path or u.netloc == 'forms.gle') and not u.query:
            result['external_requirements'].append(dict(source_url=link, text=n.text(), completion='unverified'))
        if u.scheme == 'https' and u.netloc == 'spectrum.um.edu.my' and u.path == '/mod/forum/discuss.php':
            q = parse_qs(u.query)
            if set(q) <= {'d', 'parent'} and len(q.get('d', [])) == 1:
                try:
                    did = str(positive(q['d'][0]))
                except ValueError:
                    continue
                if did not in discussions:
                    row = n
                    while row.parent and row.tag != 'tr':
                        row = row.parent
                    discussions[did] = dict(discussion_id=did, title=n.text(), source_url=ORIGIN+'/mod/forum/discuss.php?d='+did,
                        reference_text=row.text() if row.tag == 'tr' else n.text(), body_status='not_fetched_read_state_unproven')
        if mod == 'forum' and u.path == '/mod/forum/view.php' and ('next' in n.attrs.get('rel', '').split() or n.text().lower() in ('next', 'next page', '»')):
            try:
                page_url(link)
                if parse_qs(u.query).get('id') == parse_qs(urlsplit(url).query).get('id'):
                    result['next_page'] = link
            except ValueError:
                pass
    result['discussions'] = list(discussions.values())
    # External forms can be plain text rather than a hyperlink.
    if re.search(r'Google\s+Form', result['text'], re.I) and not result['external_requirements']:
        result['external_requirements'] = [dict(text='Google Form mentioned in source instructions', completion='unverified')]
    result['group_completion'] = 'unresolved'
    return result


def parse_forum_posts(data, discussion_id):
    """Typed canonical posts from the non-marking Moodle read API; no prose dates inferred."""
    did=str(positive(discussion_id)); seen=set(); result=[]
    if len(data.get('posts',[]))>200:
        raise ValueError('Discussion post count exceeds bound')
    for p in data.get('posts',[]):
        pid=str(positive(p['id']))
        if str(p['discussionid'])!=did:
            raise ValueError('Discussion post identity mismatch')
        if pid in seen: continue
        seen.add(pid)
        if p.get('isdeleted'): continue
        files=[]
        for a in p.get('attachments',[]):
            try: u=plugin_url(a['fileurl'])
            except (KeyError,ValueError): continue
            files.append(dict(source_url=u,name=unquote(urlsplit(u).path.rsplit('/',1)[-1])))
        row=dict(native_post_id=pid,discussion_id=did,title=plain(p.get('subject','')),
            message=plain(p.get('message','')),source_url=ORIGIN+'/mod/forum/discuss.php?d='+did+'#p'+pid,
            timecreated=p.get('timecreated'),timemodified=p.get('timemodified'),files=files,
            body_status='read_api_no_marking',unread_observed=p.get('unread'))
        candidates = p.get('inline_images', [])
        images = []
        for image in candidates:
            try:
                plugin_url(image)
                if re.search(r'\.(?:jpe?g|png)$', urlsplit(image).path, re.I):
                    images.append(image)
            except (TypeError, ValueError):
                continue
        if images:
            row['inline_images'] = list(dict.fromkeys(images))
        result.append(row)
    return result
