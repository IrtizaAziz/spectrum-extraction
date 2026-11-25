(() => {
  const origin = 'https://spectrum.um.edu.my';
  const stop = status => ({status});
  if (location.origin !== origin) return stop('out_of_scope');
  const visible = el => el && el.getClientRects().length > 0;
  const login = document.querySelector('form#login, input[type="password"]');
  const account = document.querySelector('#user-menu-toggle, [data-region="usermenu"] .usermenu, a[data-title="logout,moodle"]');
  if (location.pathname.startsWith('/login/') || visible(login) || !visible(account)) return stop('authentication_required');
  function identity(url) {
    if (url.origin !== origin || url.username || url.password) return null;
    const path = url.pathname;
    const match = /^\/mod\/(assign|quiz|forum|resource|folder|page|book|url)\/view\.php$/.exec(path);
    const kind = path === '/course/view.php' ? 'course' : path === '/mod/forum/discuss.php' ? 'discussion' : match?.[1];
    const field = kind === 'discussion' ? 'd' : 'id';
    const ids = url.searchParams.getAll(field), id = ids[0];
    if (!kind || ids.length !== 1 || !/^[1-9]\d{0,14}$/.test(id)) return null;
    return {kind, id, url:`${origin}${path}?${field}=${id}`};
  }
  const page = identity(new URL(location.href));
  if (!page) return stop('unsupported_page');
  const clean = (text, limit) => String(text || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '').trim().slice(0,limit);
  const root = document.querySelector('#region-main');
  if (!root) return stop('content_unavailable');
  const title = clean(document.querySelector('#page-header h1, main h1, h1')?.innerText,200);
  const references = [], deadline_claims = [], submission_evidence = [], posts = [];
  const seen = new Set();
  const links = root.querySelectorAll('a[href]');
  for (const anchor of Array.from(links).slice(0,500)) {
    if (!visible(anchor) || references.length >= 40) continue;
    try {
      const ref = identity(new URL(anchor.getAttribute('href'),page.url));
      const text = clean(anchor.innerText,160);
      if (ref && ref.kind !== 'course' && ref.url !== page.url && text && !seen.has(ref.url)) {
        seen.add(ref.url); references.push({kind:ref.kind,id:ref.id,title:text,url:ref.url});
      }
    } catch { /* Invalid or unrelated references are not evidence. */ }
  }
  const assessment = ['assign','quiz'].includes(page.kind);
  const intro = assessment ? root.querySelector('#intro, .activity-description') : null;
  const instructions = visible(intro) ? clean(intro.innerText,2400) : '';
  if (assessment) for (const row of Array.from(root.querySelectorAll('table tr')).slice(0,100)) {
    if (!visible(row)) continue;
    const cells = row.querySelectorAll('th,td');
    if (cells.length !== 2) continue;
    const label = clean(cells[0].innerText,100), text = clean(cells[1].innerText,220);
    if (!text) continue;
    if (/^(due date|cut-?off date|extension due date|opens|closes|time limit)\s*:?$/i.test(label) && deadline_claims.length < 10) deadline_claims.push({label,text});
    if (/^(submission status|attempt status|grading status)\s*:?$/i.test(label) && submission_evidence.length < 10) submission_evidence.push({label,text});
  }
  if (page.kind === 'discussion') {
    for (const post of Array.from(root.querySelectorAll('[data-region="post"][data-post-id], article[data-post-id]')).slice(0,30)) {
      const id = post.getAttribute('data-post-id');
      const body = post.querySelector('[data-region="post-content"], .post-content-container');
      if (posts.length >= 5 || !visible(body) || !/^[1-9]\d{0,14}$/.test(id) || seen.has(id)) continue;
      const text = clean(body.innerText,1000);
      if (text) { seen.add(id); posts.push({id,text,url:`${page.url}#p${id}`}); }
    }
  }
  return {status:'captured',coverage:'visible_page_only',page,title,references,instructions,deadline_claims,submission_evidence,posts, possibly_truncated:links.length>500 || references.length===40 || instructions.length===2400 || posts.length===5 || posts.some(p=>p.text.length===1000)};
})()
