/* Mini Me web app (Dev 7). Plain JavaScript, no build step. Nothing the user shared is ever stored in the browser
   (nothing is kept in browser storage): shared and pasted text lives in memory only, for as long as the screen needs it.
   Text is always put on screen with textContent (never as HTML). */
'use strict';
(() => {
  const root = document.getElementById('app');
  const boot = (() => { try { return JSON.parse(document.getElementById('boot').textContent) || {}; } catch (e) { return {}; } })();
  const navEntry = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || null;
  const reloaded = !!(navEntry && navEntry.type === 'reload');   // a reload must not re-draft the shared text

  const S = {
    session: null, shared: (!reloaded && boot.shared) || null, screens: [], idx: -1,
    item: null, last: null, incoming: null, tags: null, copyNo: {}, queue: new Map(), timer: null,
    nonce: Math.random().toString(36).slice(2), pending: null, steps: 0, stepTimers: [], selTag: null, tagNote: '',
  };
  const TEST = 'Test mode — made-up or own messages only';

  // ------------------------------------------------------------------ tiny DOM helper
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const btn = (label, cls, onclick, extra) => h('button', Object.assign({ type: 'button', class: 'mm-btn ' + cls, onclick }, extra || {}), label);
  const link = (label, onclick) => h('button', { type: 'button', class: 'mm-link', onclick }, label);
  const chip = (label, cls, icon) => h('span', { class: 'mm-chip ' + (cls || '') }, icon ? h('span', { 'aria-hidden': 'true' }, icon) : null, label);

  // Masked tokens ([PHONE], [EMAIL], Person A ...) are shown as dashed chips inside the text.
  function masked(text) {
    const box = h('p', { class: 'mm-incoming' });
    const parts = String(text || '').split(/(\[[A-Za-z]+(?: \d+)?(?:: [^\]]+)?\])/);
    parts.forEach((p) => { if (/^\[.+\]$/.test(p)) box.append(h('span', { class: 'mm-mask' }, p)); else if (p) box.append(document.createTextNode(p)); });
    return box;
  }

  // ------------------------------------------------------------------ api
  async function api(path, opts) {
    opts = opts || {};
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || (opts.body ? 'POST' : 'GET'), credentials: 'same-origin', keepalive: !!opts.keepalive,
        headers: opts.body ? { 'Content-Type': 'application/json' } : {}, body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) { throw { kind: 'network' }; }
    if (res.status === 401) throw { kind: 'auth' };
    let data = null; try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok && !(data && data.action)) throw { kind: 'http', status: res.status, detail: data && data.detail };
    return data;
  }

  // ------------------------------------------------------------------ navigation (Android Back moves between screens)
  function go(name, data, opts) {
    const entry = { name, data: data || {} };
    if (opts && opts.replace && S.idx >= 0) { S.screens[S.idx] = entry; history.replaceState({ i: S.idx }, ''); }
    else { S.idx += 1; S.screens = S.screens.slice(0, S.idx); S.screens[S.idx] = entry; history.pushState({ i: S.idx }, ''); }
    render(entry);
  }
  window.addEventListener('popstate', (e) => {
    const i = e.state && typeof e.state.i === 'number' ? e.state.i : 0;
    const entry = S.screens[i];
    S.idx = i;
    render(entry && !['drafting'].includes(entry.name) ? entry : { name: 'home', data: {} });
  });
  function leave() {   // "Back to WhatsApp": ask Android to open WhatsApp; if the phone stays here, say how to switch
    let left = false;
    const mark = () => { if (document.hidden) left = true; };
    document.addEventListener('visibilitychange', mark, { once: true });
    try { window.location.href = 'intent://send/#Intent;scheme=whatsapp;package=com.whatsapp;end'; } catch (e) { /* ignore */ }
    setTimeout(() => { if (!left && !document.hidden) toast('Could not open WhatsApp. Use your recent apps button to switch back.'); }, 900);
  }
  function toast(msg) {
    const t = h('div', { role: 'status', class: 'mm-card', style: 'position:fixed;left:18px;right:18px;bottom:96px;z-index:9' }, msg);
    document.body.append(t); setTimeout(() => t.remove(), 4000);
  }

  // ------------------------------------------------------------------ layout
  function frame(opts) {
    const where = opts.where === false ? null : h('span', { class: 'mm-where' }, opts.where || '');
    const main = h('main', { class: 'mm-main' },
      S.session && S.session.test_mode ? h('div', { class: 'mm-test' }, h('span', { 'aria-hidden': 'true' }, 'ⓘ'), TEST) : null,
      opts.body);
    return h('div', { class: 'mm-phone' },
      h('header', { class: 'mm-head' }, h('span', { class: 'mm-logo' }, h('span', { class: 'mm-dot', 'aria-hidden': 'true' }), 'Mini Me'), where),
      main, opts.bar ? h('div', { class: 'mm-bar' }, opts.bar) : null);
  }
  function show(node, headingFocus) {
    root.replaceChildren(node);
    const hd = node.querySelector('h1,h2');
    if (hd && headingFocus !== false) { hd.setAttribute('tabindex', '-1'); try { hd.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    window.scrollTo(0, 0);
  }
  function conf(label) {
    const n = label === 'High' ? 3 : label === 'Medium' ? 2 : 1;
    return h('span', { class: 'mm-conf' }, 'Confidence: ' + label,
      h('span', { class: 'mm-seg', 'aria-hidden': 'true' }, [0, 1, 2].map((i) => h('i', { class: i < n ? 'on' : '' }))));
  }

  // ------------------------------------------------------------------ boot
  async function start() {
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
    try { S.session = await api('/app/api/session'); } catch (e) { S.session = { signed_in: false, test_mode: true }; if (e.kind === 'network') { S.session.offline = true; } }
    history.replaceState({ i: 0 }, ''); S.idx = 0; S.screens = [{ name: 'home', data: {} }];
    if (!S.session.signed_in) {
      if (S.session.offline) return go('offline', {}, { replace: true });
      return go('login', {}, { replace: true });
    }
    logSession(S.shared ? 'share' : 'home');
    if (S.shared) { const p = S.shared; S.shared = null; return routeIncoming(p, 'share', true); }
    go('home', {}, { replace: true });
  }
  function logSession(entry) { api('/app/api/session', { body: { entry, nonce: S.nonce } }).catch(() => {}); }

  // ------------------------------------------------------------------ screens
  function render(entry) {
    const fn = SCREENS[entry.name] || SCREENS.home;
    fn(entry.data || {});
  }
  const SCREENS = {};

  // ---- login (5e): no part of a shared message is shown here
  SCREENS.login = (d) => {
    const pw = h('input', { type: 'password', id: 'pw', autocomplete: 'current-password', 'aria-label': 'Password' });
    const err = h('p', { class: 'mm-small', role: 'alert' });
    const submit = async () => {
      err.textContent = '';
      const fd = new URLSearchParams({ password: pw.value, device_type: 'mobile' });
      let res;
      try { res = await fetch('/login', { method: 'POST', body: fd, credentials: 'same-origin' }); } catch (e) { return go('offline', {}, { replace: true }); }
      if (!res.ok) { err.textContent = 'That password did not work. Try again.'; return; }
      S.session = await api('/app/api/session');
      if (S.pending) { const p = S.pending; S.pending = null; return routeIncoming(p.payload, p.entry, true); }
      go('home', {}, { replace: true });
    };
    pw.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    show(frame({ where: false, body: [
      h('h1', null, 'Sign in'),
      h('p', { class: 'mm-muted' }, S.pending || S.shared ? 'Your shared message is waiting. Sign in and I will draft it.' : 'Sign in to see your drafts.'),
      h('label', { class: 'mm-label', for: 'pw' }, 'Password'), pw, err,
      h('p', { class: 'mm-small' }, 'You stay signed in on this phone.')],
      bar: btn('Sign in', 'mm-primary', submit) }));
    if (S.shared) { S.pending = { payload: S.shared, entry: 'share' }; S.shared = null; }   // kept in memory only
  };

  // ---- home: Paste message + waiting list (the home for confirming)
  SCREENS.home = async () => {
    const listBox = h('div', null, h('p', { class: 'mm-muted' }, 'Loading your waiting drafts…'));
    show(frame({ where: false, body: [
      h('h1', null, 'Mini Me'),
      h('p', { class: 'mm-muted' }, 'In WhatsApp: long-press the message, tap Copy. Then come back here.'),
      btn('Paste message', 'mm-primary', pasteMessage, { id: 'paste-btn' }),
      h('div', { style: 'height:18px' }), listBox] }));
    await loadList(listBox, false);
  };

  async function loadList(box, fromDraft) {
    let p;
    try { p = await api('/app/api/pending'); } catch (e) {
      if (e.kind === 'auth') return go('login', {}, { replace: true });
      box.replaceChildren(h('p', { class: 'mm-muted' }, 'I cannot reach the app right now. Your waiting drafts are safe.')); return;
    }
    S.list = p;
    renderList(box, p);
  }

  function countChip(p, handled) {
    const open = p.items.filter((i) => !handled.has(i.message_id));
    if (!p.items.length) return chip('Nothing waiting', '', '○');
    if (!open.length) return chip('All caught up', 'ok', '✓');
    const c = open.filter((i) => i.kind === 'copied').length, hd = open.filter((i) => i.kind === 'held').length;
    return chip((c ? c + (c === 1 ? ' copied draft waiting' : ' copied drafts waiting') : '') + (c && hd ? ' · ' : '') + (hd ? hd + ' held draft' : ''), '', '◴');
  }

  function renderList(box, p) {
    const handled = new Set(S.queue.keys());
    const kids = [h('h2', null, 'Waiting for you'), h('p', { class: 'mm-muted' }, 'Drafts you copied. Tell me what happened, one tap each.'),
      h('div', { style: 'margin-bottom:12px' }, countChip(p, handled))];
    if (!p.items.length) {
      kids.push(h('div', { class: 'mm-empty' }, h('span', { class: 'mm-dot', 'aria-hidden': 'true' }), h('h3', null, 'All caught up'),
        h('p', { class: 'mm-muted' }, 'Nothing is waiting. When you copy a draft, it shows up here so you can tell me what happened.'),
        h('p', { class: 'mm-small' }, 'Drafts you do not confirm within 24 hours are left out of your numbers.')));
    }
    p.items.forEach((it) => kids.push(handled.has(it.message_id) ? doneRow(it, box, p) : card(it, box, p)));
    if (S.queue.size) kids.push(btn('Done', 'mm-primary', async () => { await flush(); go('home', {}, { replace: true }); }));
    box.replaceChildren(...kids);
  }

  function card(it, box, p) {
    const decide = (action, text) => { S.queue.set(it.message_id, { message_id: it.message_id, action, text, kind: it.kind, group: it.group }); schedule(); renderList(box, p); };
    const incoming = it.incoming ? masked(it.incoming) : h('p', { class: 'mm-small' }, '(' + (it.incoming_note || 'message not kept in test mode') + ')');
    const head = h('div', { class: 'mm-row', style: 'justify-content:space-between' },
      h('strong', null, it.group || 'Shared message'),
      h('span', { class: 'mm-small' }, (it.kind === 'held' ? 'Shared ' : 'Copied ') + it.when + (it.clock ? ' · ' + it.clock : '')));
    if (it.kind === 'held') {
      return h('section', { class: 'mm-card' }, head, h('p', { class: 'mm-small' }, 'Held draft, waiting for your reply'), incoming,
        h('div', { class: 'mm-hold' }, chip('Draft held', 'twin', '◔'),
          h('p', { style: 'margin:8px 0 0' }, 'Reply in WhatsApp, then paste your reply here. I will show you mine.')),
        h('p', { class: 'mm-small' }, 'Confirm within ' + it.confirm_within_hours + ' h'),
        btn('Paste my reply', 'mm-outline', () => replyToHeld(it.message_id, 'paste')),
        link('Not replying', () => decide('not_replying')));
    }
    return h('section', { class: 'mm-card' }, head, incoming,
      h('div', { class: 'mm-card mm-twin', style: 'margin:10px 0' }, h('span', { class: 'mm-label' }, 'Your twin wrote this'), h('p', { style: 'margin:0;white-space:pre-wrap' }, it.draft)),
      h('p', { class: 'mm-small' }, 'Confirm within ' + it.confirm_within_hours + ' h'),
      h('div', { class: 'mm-outcomes' },
        btn('Sent as is', 'mm-outline', () => decide('sent_as_is')),
        btn('I edited it → paste', 'mm-outline', () => go('pasteSent', { item: it })),
        btn('Not replying', 'mm-outline', () => decide('not_replying'))));
  }

  function doneRow(it, box, p) {
    const q = S.queue.get(it.message_id);
    const msg = q.action === 'sent_as_is' ? '✓ Logged as sent as is' : q.action === 'edited' ? '✓ Saved your version' : '– Marked not replying';
    return h('div', { class: 'mm-done' }, h('span', null, msg, it.group ? ' · ' + it.group : ''),
      link('Undo', () => { S.queue.delete(it.message_id); schedule(); renderList(box, p); }));
  }

  // Outcomes are held ~10 s so Undo can happen before anything is logged (events are append-only, design C-7).
  function schedule() { clearTimeout(S.timer); if (S.queue.size) S.timer = setTimeout(() => flush(), 10000); }
  async function flush() {
    clearTimeout(S.timer);
    if (!S.queue.size) return;
    const items = [...S.queue.values()].map((q) => ({ message_id: q.message_id, action: q.action, text: q.text }));
    try {
      const r = await api('/app/api/confirm', { body: { items }, keepalive: true });
      (r.results || []).forEach((x) => { if (x.ok || x.status === 409 || x.status === 404) S.queue.delete(x.message_id); });
    } catch (e) { if (e.kind === 'auth') go('login', {}, { replace: true }); /* otherwise keep them for the next try */ }
  }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(); });
  window.addEventListener('pagehide', () => { flush(); });

  // ---- "I edited it": paste what you sent (4b)
  SCREENS.pasteSent = (d) => {
    const it = d.item;
    const ta = h('textarea', { id: 'sent', 'aria-label': 'What you sent' });
    const save = btn('Save', 'mm-primary', () => {
      if (!ta.value.trim()) return;
      S.queue.set(it.message_id, { message_id: it.message_id, action: 'edited', text: ta.value, kind: 'copied', group: it.group });
      schedule(); history.back();
    }, { disabled: true });
    ta.addEventListener('input', () => { if (ta.value.trim()) save.removeAttribute('disabled'); else save.setAttribute('disabled', ''); });
    const fillBtn = btn('Paste from clipboard', 'mm-outline', async () => { try { ta.value = await navigator.clipboard.readText(); ta.dispatchEvent(new Event('input')); } catch (e) { toast('Press and hold in the box, then tap Paste.'); } });
    show(frame({ where: false, body: [
      link('‹ Waiting for you', () => history.back()), h('h2', null, 'Paste what you sent'),
      h('p', { class: 'mm-muted' }, 'In WhatsApp, long-press your message, tap Copy, then paste it here.'),
      h('div', { class: 'mm-card mm-twin' }, h('span', { class: 'mm-label' }, 'My draft was'), h('p', { style: 'margin:0;white-space:pre-wrap' }, it.draft)),
      h('label', { class: 'mm-label', for: 'sent' }, 'What you sent'), ta, h('div', { style: 'height:8px' }), fillBtn,
      h('p', { class: 'mm-small' }, 'Personal details are hidden before saving.')],
      bar: [save, link('Cancel', () => history.back())] }));
  };

  // ------------------------------------------------------------------ incoming text: paste or share
  async function pasteMessage() {
    let text = '';
    try { text = await navigator.clipboard.readText(); } catch (e) { return go('pasteManual', { reason: (e && e.name) || 'blocked' }); }
    if (!text || !text.trim()) return go('pasteManual', { reason: 'empty' });
    routeIncoming({ text }, 'paste', false);
  }
  SCREENS.pasteManual = (d) => {
    const ta = h('textarea', { id: 'manual', 'aria-label': 'The message' });
    const go2 = btn('Draft a reply', 'mm-primary', () => { if (ta.value.trim()) routeIncoming({ text: ta.value }, 'paste', false); });
    show(frame({ where: false, body: [h('h2', null, d.reason === 'empty' ? 'Nothing on the clipboard' : 'Paste it here'),
      h('p', { class: 'mm-muted' }, d.reason === 'empty' ? 'Copy the message in WhatsApp first (long-press, Copy), then tap Paste message again. Or paste it below.'
        : 'Your phone did not let me read the clipboard. Press and hold in the box below, tap Paste, then tap Draft a reply.'),
      h('label', { class: 'mm-label', for: 'manual' }, 'The message'), ta],
      bar: [go2, link('Back', () => history.back())] }));
  };

  // A held (blind) draft may be waiting for the user's reply: ask before treating this text as a new message.
  async function routeIncoming(payload, entry, replace) {
    let p = null;
    try { p = await api('/app/api/pending'); } catch (e) { if (e.kind === 'auth') { S.pending = { payload, entry }; return go('login', {}, { replace: true }); } }
    const held = p ? p.items.filter((i) => i.kind === 'held') : [];
    if (held.length) { S.incoming = { payload, entry }; return go('intercept', { held }, { replace }); }
    startDraft(payload, entry, null, replace);
  }
  SCREENS.intercept = (d) => {
    const inc = S.incoming;
    const one = d.held[0];
    show(frame({ where: false, body: [h('h2', null, 'Is this your reply?'),
      h('p', { class: 'mm-muted' }, 'I am holding a draft until you reply (' + (one.group ? one.group + ', ' : '') + 'shared ' + one.when + '). Is this what you sent?'),
      h('div', { class: 'mm-card mm-you' }, h('span', { class: 'mm-label' }, 'What you pasted'), h('p', { class: 'mm-incoming' }, inc.payload.text.length > 220 ? inc.payload.text.slice(0, 220) + '…' : inc.payload.text))],
      bar: [btn('Yes, that is my reply', 'mm-primary', () => replyToHeld(one.message_id, inc.entry === 'share' ? 'share' : 'paste', inc.payload.text)),
        btn('It is a new message', 'mm-outline', () => startDraft(inc.payload, inc.entry, null, true))] }));
  };

  // ------------------------------------------------------------------ drafting
  function startSteps() {
    S.steps = 0; S.stepTimers.forEach(clearTimeout); S.stepTimers = [setTimeout(() => { S.steps = 1; paintSteps(); }, 900), setTimeout(() => { S.steps = 2; paintSteps(); }, 2600)];
    return { stop() { S.stepTimers.forEach(clearTimeout); } };
  }
  function stepList() {
    const names = ['Hiding personal details', 'Reading your voice and rules', 'Writing your reply'];
    return h('ol', { class: 'mm-steps', id: 'steps' }, names.map((n, i) => h('li', null, h('span', null, n), h('span', { class: 'mm-small' }, i < S.steps ? '✓ Done' : i === S.steps ? 'In progress' : 'Next'))));
  }
  function paintSteps() { const el = document.getElementById('steps'); if (el) el.replaceWith(stepList()); }
  SCREENS.drafting = () => {
    show(frame({ where: 'Shared message', body: [
      h('div', { class: 'mm-card' }, h('span', { class: 'mm-label' }, 'Message you shared'), h('p', { class: 'mm-muted' }, 'Hiding personal details, then drafting…')),
      h('div', { class: 'mm-card mm-twin', 'aria-live': 'polite' }, h('h3', null, 'Drafting in your voice'), stepList(), h('div', { class: 'mm-prog', role: 'progressbar', 'aria-label': 'Working' }),
        h('p', { class: 'mm-small', style: 'margin-top:10px' }, 'Usually a few seconds. The first one of the day can take a little longer.'))],
      bar: link('Cancel', () => { S.cancelled = true; history.back(); }) }));
  };

  async function startDraft(payload, entry, retryOf, replace) {
    S.last = { payload, entry }; S.cancelled = false; S.tagNote = ''; S.selTag = null; S.item = null;
    if (navigator.onLine === false) return go('offline', {}, { replace: !!replace });
    go('drafting', {}, { replace: !!replace });
    const steps = startSteps();
    let r;
    try { r = await api('/app/api/drafts', { body: Object.assign({ entry }, payload, retryOf ? { retry_of: retryOf } : {}) }); }
    catch (e) {
      steps.stop();
      if (e.kind === 'auth') { S.pending = { payload, entry }; return go('login', {}, { replace: true }); }
      return go(navigator.onLine === false || e.kind === 'network' ? 'offline' : 'failed', { message: 'The app did not answer.' }, { replace: true });
    }
    steps.stop();
    if (S.cancelled) return;
    S.item = r; S.copyNo = {};
    const name = { draft: 'draft', held: 'held', ask_first: 'askfirst', failed: 'failed', copy_first: 'copyfirst' }[r.action] || 'failed';
    go(name, r, { replace: true });
  }

  // ------------------------------------------------------------------ From: row (tag -> server-side re-run, 5.21 pt 1)
  async function fromRow(item, onTagged) {
    const wrap = h('div', { class: 'mm-card' }, h('span', { class: 'mm-label' }, 'From:'), h('p', { class: 'mm-small' }, 'Optional. Helps me tell known from new.'));
    const row = h('div', { class: 'mm-scroll' });
    const note = h('p', { class: 'mm-small', role: 'status' }, S.tagNote);
    wrap.append(row, note);
    let tags = S.tags;
    if (!tags) { try { tags = (await api('/app/api/tags')).tags; S.tags = tags; } catch (e) { tags = []; } }
    const apply = async (body, key) => {
      try {
        const r = await api('/app/api/items/' + item.message_id + '/tag', { body });
        S.selTag = body.clear ? null : key; S.tags = null;
        S.tagNote = body.clear ? 'Tag cleared.' : (item.action === 'ask_first' ? 'Tagged. This one still needs you: the reason is in what the message says, not who sent it.' : 'Tagged. I looked again; this draft stays as it is.');
        if (onTagged) onTagged();
      } catch (e) { note.textContent = 'I could not save that tag. Try again.'; }
    };
    const mk = (label, key, body) => h('button', { type: 'button', class: 'mm-chipbtn', 'aria-pressed': S.selTag === key ? 'true' : 'false',
      onclick: () => (S.selTag === key ? apply({ clear: true }, null) : apply(body, key)) }, (S.selTag === key ? '✓ ' : '') + label);
    tags.forEach((t) => row.append(mk(t.label, t.tag_id, { tag_id: t.tag_id })));
    row.append(mk('Someone new', 'new', { someone_new: true }));
    row.append(h('button', { type: 'button', class: 'mm-chipbtn', onclick: () => addForm(wrap, apply) }, '+ Add'));
    return wrap;
  }
  function addForm(wrap, apply) {
    if (wrap.querySelector('.mm-add')) return;
    const name = h('input', { type: 'text', maxlength: '40', 'aria-label': 'Who it is from', placeholder: 'A short label, not a number' });
    let kind = 'group';
    const g = h('button', { type: 'button', class: 'mm-chipbtn', 'aria-pressed': 'true' }, 'Group'), p = h('button', { type: 'button', class: 'mm-chipbtn', 'aria-pressed': 'false' }, 'Person');
    g.onclick = () => { kind = 'group'; g.setAttribute('aria-pressed', 'true'); p.setAttribute('aria-pressed', 'false'); };
    p.onclick = () => { kind = 'individual'; p.setAttribute('aria-pressed', 'true'); g.setAttribute('aria-pressed', 'false'); };
    wrap.append(h('div', { class: 'mm-add' }, name, h('div', { class: 'mm-row' }, g, p),
      btn('Save tag', 'mm-outline', () => { if (name.value.trim()) apply({ label: name.value, kind }, 'new-' + name.value); })));
  }

  // ---- draft screen (1b-1d)
  SCREENS.draft = async (d) => {
    S.item = d;
    const low = d.confidence_label === 'Low';
    const status = h('p', { class: 'mm-small', role: 'status' });
    const draftEl = h('p', { class: 'mm-draft', id: 'draft-text' }, d.draft);
    const banner = h('div', { id: 'banner' });
    const body = [
      h('h2', null, 'Your draft'),
      h('div', { class: 'mm-card' }, h('span', { class: 'mm-label' }, 'Message you shared'),
        d.masked_count ? chip(d.masked_count + (d.masked_count === 1 ? ' detail hidden' : ' details hidden'), '', '⌀') : null,
        d.masked_text ? masked(d.masked_text) : h('p', { class: 'mm-small' }, '(message not kept in test mode)')),
      await fromRow(d, () => SCREENS.draft(d)),
      h('div', { class: 'mm-card mm-twin' }, h('div', { class: 'mm-row', style: 'justify-content:space-between' }, h('span', { class: 'mm-label' }, 'Your twin wrote this'), conf(d.confidence_label)),
        low ? h('div', { class: 'mm-note' }, h('strong', null, '! Check this one. '), 'I am not sure about this reply. Read it before you paste it.') : null,
        draftEl, d.why ? h('p', { class: 'mm-why' }, 'Why: ' + d.why) : null,
        d.unrestored_tokens && d.unrestored_tokens.length ? h('p', { class: 'mm-small' }, 'Some details stayed hidden in this draft. Fill them in when you paste.') : null),
      banner, status];
    show(frame({ where: 'Shared message', body, bar: [
      btn('Copy draft', 'mm-primary', () => copyDraft(d, status), { id: 'copy-btn' }),
      link('Not replying', () => notReplying(d))] }));
    api('/app/api/pending').then((p) => {
      const n = p.copied; const el = document.getElementById('banner');
      if (el && n) el.replaceChildren(h('div', { class: 'mm-done' }, h('span', null, '◴ ' + n + (n === 1 ? ' copied draft waiting' : ' copied drafts waiting')), link('Review', () => go('home'))));
    }).catch(() => {});
  };

  async function writeClipboard(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall back */ }
    try {
      const ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;opacity:0;top:0';
      document.body.append(ta); ta.select(); const ok = document.execCommand('copy'); ta.remove(); return !!ok;
    } catch (e) { return false; }
  }
  async function copyDraft(d, status) {
    const ok = await writeClipboard(d.draft);
    if (!ok) { status.textContent = 'Could not copy. Press and hold the draft to copy it.'; return; }   // no draft_copied
    const n = (S.copyNo[d.message_id] || 0) + 1; S.copyNo[d.message_id] = n;
    api('/app/api/items/' + d.message_id + '/copied', { body: { copy_no: n } }).catch(() => api('/app/api/items/' + d.message_id + '/copied', { body: { copy_no: n } }).catch(() => {}));
    go('copied', d);
  }
  SCREENS.copied = (d) => {
    const again = btn('Copy again', 'mm-outline', async function () { if (await writeClipboard(d.draft)) { const n = (S.copyNo[d.message_id] || 1) + 1; S.copyNo[d.message_id] = n; api('/app/api/items/' + d.message_id + '/copied', { body: { copy_no: n } }).catch(() => {}); again.textContent = 'Copied again'; } });
    show(frame({ where: 'Shared message', body: [
      h('div', { class: 'mm-big' }, h('div', { class: 'mm-tick mm-chip ok', 'aria-hidden': 'true' }, '✓'), h('h2', null, 'Copied')),
      h('p', { class: 'mm-muted', style: 'text-align:center' }, 'Paste it into your WhatsApp reply. Edit it if you like. Nothing leaves until you decide.'),
      h('div', { class: 'mm-card mm-twin' }, h('p', { style: 'margin:0 0 8px;white-space:pre-wrap;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden' }, d.draft), again),
      h('div', { class: 'mm-card' }, h('h3', null, 'What happens next'), h('p', { class: 'mm-muted' }, 'Later I will ask what happened with it. It takes one tap.'),
        link('See waiting drafts', () => go('home')))],
      bar: [btn('Back to WhatsApp', 'mm-primary', leave), h('p', { class: 'mm-small', style: 'text-align:center;margin:0' }, 'If it does not open, use your recent apps button.')] }));
  };

  async function notReplying(d) {
    try { await api('/app/api/items/' + d.message_id + '/not-replying', { body: {} }); } catch (e) { /* the sweep will close it */ }
    go('noted', { message: 'Marked as not replying. Nothing was copied, and this one will not count toward your scores.' });
  }
  SCREENS.noted = (d) => show(frame({ where: false, body: [
    h('div', { class: 'mm-big' }, h('div', { class: 'mm-tick mm-chip', 'aria-hidden': 'true' }, '–'), h('h2', null, 'Noted')), h('p', { class: 'mm-muted', style: 'text-align:center' }, d.message)],
    bar: btn('Back to WhatsApp', 'mm-primary', leave) }));

  // ---- blind sample (3a-3c): the draft is held on the server; nothing of it is here
  SCREENS.held = (d) => {
    show(frame({ where: 'Shared message', body: [
      h('div', { class: 'mm-card' }, h('span', { class: 'mm-label' }, 'Message you shared'), d.masked_text ? masked(d.masked_text) : h('p', { class: 'mm-small' }, '(message not kept in test mode)')),
      h('h2', null, 'I have drafted this one'),
      h('div', { class: 'mm-hold' }, chip('Draft held', 'twin', '◔'), h('p', { style: 'margin:8px 0 0' }, 'Reply in WhatsApp first, then paste your reply here. I will show you mine.')),
      h('ol', { class: 'mm-muted' }, h('li', null, 'Reply in WhatsApp the way you normally would.'), h('li', null, 'Long-press your reply, tap Copy, then open Mini Me and tap Paste message.')),
      h('p', { class: 'mm-small' }, 'This helps me measure how well I know you.')],
      bar: [btn('Back to WhatsApp', 'mm-primary', leave), link('Not replying', () => notReplying(d))] }));
  };
  async function replyToHeld(mid, capture, textGiven) {
    let text = textGiven;
    if (text == null) { try { text = await navigator.clipboard.readText(); } catch (e) { return go('pasteManual', { reason: 'blocked' }); } }
    if (!text.trim()) return go('pasteManual', { reason: 'empty' });
    try { const r = await api('/app/api/items/' + mid + '/reply', { body: { text, capture } }); go('heldGot', { message_id: mid, matched: r.matched, text }, { replace: !!textGiven }); }
    catch (e) { if (e.kind === 'auth') return go('login', {}, { replace: true }); toast('That one is already recorded.'); go('home', {}, { replace: true }); }
  }
  SCREENS.heldGot = (d) => show(frame({ where: false, body: [
    h('div', { class: 'mm-card mm-you' }, h('span', { class: 'mm-label' }, 'Your reply'), chip('Reply saved', 'ok', '✓'), h('p', { class: 'mm-incoming' }, d.text)),
    h('p', { class: 'mm-muted' }, 'Personal details are hidden before saving. My draft is ready to show you.')],
    bar: btn('Show my draft', 'mm-primary', async () => { try { const r = await api('/app/api/items/' + d.message_id + '/reveal', { body: {} }); go('reveal', r, { replace: true }); } catch (e) { toast('I could not show it yet.'); } }) }));
  SCREENS.reveal = (d) => show(frame({ where: false, body: [
    h('div', { class: 'mm-card mm-you' }, h('span', { class: 'mm-label' }, 'Your reply'), h('p', { class: 'mm-incoming' }, d.your_reply)),
    h('div', { class: 'mm-card mm-twin' }, h('span', { class: 'mm-label' }, 'My draft, held until you replied'), d.confidence_label ? conf(d.confidence_label) : null, masked(d.draft), d.why ? h('p', { class: 'mm-why' }, 'Why: ' + d.why) : null),
    h('p', { class: 'mm-muted' }, 'You have already replied, so there is nothing to copy.')],
    bar: btn('Done', 'mm-primary', () => go('home', {}, { replace: true })) }));

  // ---- edge states
  SCREENS.askfirst = async (d) => {
    const r = d.reason || {};
    const status = h('p', { class: 'mm-small', role: 'status' });
    show(frame({ where: 'Shared message', body: [
      h('div', { class: 'mm-card' }, h('span', { class: 'mm-label' }, 'Message you shared'), d.masked_text ? masked(d.masked_text) : null),
      await fromRow(d, () => SCREENS.askfirst(d)),
      h('div', { class: 'mm-status warn' }, chip('Needs you', 'warn', '!'), h('h2', { style: 'margin-top:8px' }, 'I did not draft this one'), h('p', null, h('strong', null, r.headline || 'Asked you first.')), h('p', { class: 'mm-muted' }, r.detail || '')),
      h('p', { class: 'mm-muted' }, 'Optional: after you reply, paste your reply here and I will learn from it. It is not scored.'),
      btn('Paste my reply', 'mm-outline', async () => {
        let text = ''; try { text = await navigator.clipboard.readText(); } catch (e) { status.textContent = 'Your phone did not let me read the clipboard.'; return; }
        try { await api('/app/api/items/' + d.message_id + '/reply', { body: { text, capture: S.last && S.last.entry === 'share' ? 'share' : 'paste' } }); go('noted', { message: 'Reply saved. This one is not scored.' }, { replace: true }); }
        catch (e) { status.textContent = 'I could not save that reply.'; }
      }), status],
      bar: [btn('Back to WhatsApp', 'mm-primary', leave), link('Not replying', () => notReplying(d))] }));
  };
  SCREENS.failed = (d) => show(frame({ where: 'Shared message', body: [
    h('div', { class: 'mm-status bad' }, chip('Could not draft', 'bad', '✕'), h('h2', { style: 'margin-top:8px' }, 'I could not write a draft this time'),
      h('p', null, d.message || 'The drafting service did not answer.'), h('p', { class: 'mm-muted' }, 'Your message is not lost. I still have it, with personal details hidden.'),
      h('p', { class: 'mm-small' }, 'This one will not count toward your numbers.'))],
    bar: [S.last ? btn('Try again', 'mm-primary', () => startDraft(S.last.payload, S.last.entry, S.item && S.item.message_id, true)) : null, link('Back to WhatsApp', leave)] }));
  SCREENS.offline = () => show(frame({ where: false, body: [
    h('div', { class: 'mm-status warn' }, chip('No connection', 'warn', '⌁'), h('h2', { style: 'margin-top:8px' }, 'You are offline'),
      h('p', null, 'I need a connection to write a draft. Nothing from your message was saved on this phone.'), h('p', { class: 'mm-muted' }, 'Go back to WhatsApp and copy it again once you are online.'))],
    bar: btn('Back to WhatsApp', 'mm-primary', leave) }));
  SCREENS.copyfirst = (d) => show(frame({ where: false, body: [
    h('div', { class: 'mm-status info' }, h('h2', null, 'Copy the message first'), h('p', null, d.message || 'That is the draft I just made. Copy the message you want a reply to first, then try again.'),
      h('p', { class: 'mm-muted' }, 'In WhatsApp, long-press the message and tap Copy.'))],
    bar: [btn('Paste message', 'mm-primary', pasteMessage), link('Back to WhatsApp', leave)] }));

  window.addEventListener('offline', () => { if (S.screens[S.idx] && S.screens[S.idx].name === 'drafting') go('offline', {}, { replace: true }); });
  start();
})();
