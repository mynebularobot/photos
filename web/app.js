'use strict';
const el = id => document.getElementById(id);
let state = null;

function say(text, kind = '') { // kind: '' (screen readers only), 'error', 'note'
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}

async function request(path, data) {
  const init = data === undefined ? { credentials: 'same-origin' }
    : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const response = await fetch(path, init);
  let body = {};
  try { body = await response.json(); } catch (e) {}
  if (!response.ok) { const e = new Error(body.error || 'Photos could not do that. Try again.'); e.code = body.code; throw e; }
  return body;
}

function n(x) { return typeof x === 'number' ? x.toLocaleString() : '0'; }

function setupCard(title, text) {
  const card = el('setup');
  card.replaceChildren();
  const h = document.createElement('h2'); h.textContent = title;
  const p = document.createElement('p'); p.className = 'ph-help'; p.textContent = text;
  card.append(h, p);
  card.hidden = false;
}

function render(s) {
  state = s;
  const own = s.links && s.links.immich;
  const im = s.immich || {};
  let line = '';
  if (!im.reachable) line = 'Starting Immich…';
  else if (!im.has_owner_account || !s.agent_connected) line = 'Setting up…';
  else if (s.library) line = n(s.library.photos) + ' photos · ' + n(s.library.videos) + ' videos · ' + (s.library.size_gb || 0) + ' GB';
  else line = 'Immich ' + (im.version || '');
  const busy = s.background_work && Object.values(s.background_work).reduce((a, q) => a + (q.waiting || 0) + (q.active || 0), 0);
  if (busy) line += ' · organising ' + n(busy);
  if (s.import && s.import.state === 'running') line += ' · importing';
  el('line').textContent = line;

  el('setup').hidden = true;
  if (!own) {
    setupCard('Open Photos from your Wizard', 'This page needs your Wizard\u2019s private address to show your library.');
  } else if (!im.reachable) {
    setupCard('Photos is starting', 'The first start takes a minute or two. This page refreshes on its own.');
  } else if (!(s.sign_in && s.sign_in.ready)) {
    setupCard('Photos is setting up', (s.sign_in && s.sign_in.problem) || 'Photos makes its own library for you; there is nothing to sign in to. This page refreshes on its own.');
  }
  el('open-full').hidden = !own;
  if (own) el('open-full').href = own;
  const frame = el('immich');
  if (own && im.reachable && s.sign_in && s.sign_in.ready) {
    el('frame-wrap').hidden = false;
    if (!frame.getAttribute('src')) frame.src = own;
  }
  const phone = s.links && s.links.mobile_server_url;
  el('phone-line').textContent = phone ? 'Phone backup: install the Immich app, type ' + phone + ' as the server, tap Next, then Continue. Keep Tailscale on.' : '';
  if (phone) backup.server = phone;
  el('agent-line').textContent = s.agent_connected ? 'Your agent is connected to Photos.' : 'Your agent connects by itself once Photos has finished setting up.';
  el('open-prints').hidden = !(s.features && s.features.grandparent_print_review);
}

function renderImport(d) {
  const st = d.state;
  const words = { idle: 'No import yet.', running: 'Importing… you can close this; it keeps going.', done: 'Last import finished.',
    failed: 'The last import stopped: ' + (d.message || 'see the log below.'), interrupted: 'The last import was interrupted. Import again to continue; nothing is duplicated.' };
  el('import-state').textContent = (words[st] || st) + ' ' + (d.waiting_files ? d.waiting_files + ' file(s), ' + d.waiting_gb + ' GB in Files › Photos › Import.' : 'No Takeout files in Files › Photos › Import yet.');
  el('import-go').disabled = st === 'running' || !d.waiting_files || !(state && state.agent_connected);
  if (!(state && state.agent_connected) && d.waiting_files) el('import-state').textContent += ' Photos is still setting up; Import now works in a minute.';
  const list = el('import-options'); list.replaceChildren();
  const labels = { include_archived: 'Include archived photos', include_partner: 'Include partner-shared photos', sync_albums: 'Recreate my Google albums', include_trashed: 'Include photos in Google\u2019s trash' };
  for (const [k, label] of Object.entries(labels)) {
    const li = document.createElement('li');
    const row = document.createElement('button'); row.type = 'button'; row.className = 'wz-row';
    const on = !!(d.options || {})[k];
    row.setAttribute('aria-pressed', String(on));
    const box = document.createElement('span'); box.className = 'wz-check'; box.setAttribute('aria-hidden', 'true');
    box.innerHTML = '<i><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></i>';
    const main = document.createElement('span'); main.className = 'wz-main';
    const b = document.createElement('b'); b.textContent = label; main.append(b);
    row.append(box, main);
    row.onclick = async () => {
      row.disabled = true;
      try { await request('api/import/options', { [k]: !on }); renderImport(await request('api/import')); }
      catch (e) { say(e.message, 'error'); row.disabled = false; }
    };
    li.append(row); list.append(li);
  }
  const log = el('import-log');
  log.hidden = !(d.log_tail && d.log_tail.length);
  log.textContent = (d.log_tail || []).join('\n');
}

async function refresh() {
  try { render(await request('api/status')); }
  catch (e) { say(e.message, 'error'); }
}

el('open-import').onclick = async () => {
  WzSheet.open(el('import-sheet'));
  try { renderImport(await request('api/import')); } catch (e) { say(e.message, 'error'); }
};
el('open-agent').onclick = () => WzSheet.open(el('agent-sheet'));
el('import-go').onclick = async () => {
  el('import-go').disabled = true;
  try { const r = await request('api/import/start', {}); say(r.started ? 'Import started.' : (r.message || 'Not started.'), 'note'); }
  catch (e) { say(e.message, 'error'); }
  try { renderImport(await request('api/import')); } catch (e) {}
};

// ---- Monthly grandparent print review (off-by-default feature switch) ----
let printReview = null;
let printApprovalRevision = '';

function selectedPrintIds() {
  return (printReview && printReview.candidates || []).filter(x => x.selected).map(x => x.id);
}

function printDate(value) {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value).slice(0, 10) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function renderPrints(d) {
  printReview = d;
  printApprovalRevision = '';
  const ids = selectedPrintIds();
  const started = d.state !== 'not_started';
  const has = !!(d.candidates && d.candidates.length);
  el('prints-summary').textContent = d.label + (started ? ' · ' + ids.length + ' selected · ' + (d.state === 'approved' ? 'Approved' : d.state === 'empty' ? 'No photos found' : 'Draft') : ' · Not started');
  el('prints-empty').hidden = d.state !== 'empty';
  el('prints-review').hidden = !has;
  el('prints-start').hidden = has;
  el('prints-generate').textContent = started ? 'Check for photos again' : 'Suggest last month’s photos';
  el('prints-confirm').hidden = true;
  if (!has) return;

  const grid = el('prints-grid');
  grid.replaceChildren();
  for (const asset of d.candidates) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'ph-print';
    button.setAttribute('aria-pressed', String(!!asset.selected));
    const detail = [printDate(asset.taken), (asset.people || []).slice(0, 3).join(', '), asset.file || 'photo'].filter(Boolean).join(', ');
    button.setAttribute('aria-label', (asset.selected ? 'Selected. Remove ' : 'Not selected. Add ') + detail + ' from the print set');
    const img = document.createElement('img'); img.src = asset.thumbnail; img.alt = '';
    const check = document.createElement('span'); check.className = 'ph-print-mark'; check.setAttribute('aria-hidden', 'true'); check.textContent = '✓';
    const meta = document.createElement('span'); meta.className = 'ph-print-meta';
    const names = (asset.people || []).slice(0, 3).join(', ');
    meta.textContent = [printDate(asset.taken), names].filter(Boolean).join(' · ') || asset.file || 'Photo';
    button.append(img, check, meta);
    button.onclick = () => {
      asset.selected = !asset.selected;
      printReview.state = 'draft';
      button.setAttribute('aria-pressed', String(asset.selected));
      button.setAttribute('aria-label', (asset.selected ? 'Selected. Remove ' : 'Not selected. Add ') + detail + ' from the print set');
      renderPrintSummary();
    };
    grid.append(button);
  }
  el('prints-note').value = d.note || '';
  renderPrintSummary();
}

function renderPrintSummary() {
  if (!printReview) return;
  printApprovalRevision = '';
  const count = selectedPrintIds().length;
  el('prints-summary').textContent = printReview.label + ' · ' + count + ' selected · ' + (printReview.state === 'approved' ? 'Approved' : 'Draft');
  el('prints-save').disabled = count < 1;
  el('prints-approve').disabled = count < 1;
  el('prints-confirm').hidden = true;
}

async function savePrints() {
  if (!printReview) return null;
  return request('api/print-review/save', { month: printReview.month, asset_ids: selectedPrintIds(), note: el('prints-note').value });
}

el('open-prints').onclick = async () => {
  WzSheet.open(el('prints-sheet'));
  try { renderPrints(await request('api/print-review')); } catch (e) { say(e.message, 'error'); }
};
el('prints-generate').onclick = async () => {
  el('prints-generate').disabled = true;
  try { renderPrints(await request('api/print-review/generate', { limit: 8 })); }
  catch (e) { say(e.message, 'error'); }
  finally { el('prints-generate').disabled = false; }
};
el('prints-save').onclick = async () => {
  el('prints-save').disabled = true;
  try { renderPrints(await savePrints()); say('Print set saved. Nothing has been ordered.', 'note'); }
  catch (e) { say(e.message, 'error'); }
  finally { if (printReview) el('prints-save').disabled = false; }
};
el('prints-approve').onclick = async () => {
  el('prints-approve').disabled = true;
  try {
    renderPrints(await savePrints());
    const preview = await request('api/print-review/approve', { month: printReview.month, confirmed: false });
    printApprovalRevision = preview.revision;
    el('prints-confirm-line').textContent = 'Approve these ' + preview.would_approve + ' photos as the print set? No order will be placed.';
    el('prints-confirm').hidden = false;
  } catch (e) { say(e.message, 'error'); }
  finally { el('prints-approve').disabled = false; }
};
el('prints-confirm-no').onclick = () => { printApprovalRevision = ''; el('prints-confirm').hidden = true; };
el('prints-confirm-yes').onclick = async () => {
  el('prints-confirm-yes').disabled = true;
  try {
    const d = await request('api/print-review/approve', { month: printReview.month, expected_count: selectedPrintIds().length,
      expected_revision: printApprovalRevision, confirmed: true });
    renderPrints(d); say('Print set approved. Nothing has been ordered.', 'note');
  } catch (e) { say(e.message, 'error'); }
  finally { el('prints-confirm-yes').disabled = false; }
};
el('prints-note').addEventListener('input', () => { if (printReview) printReview.state = 'draft'; renderPrintSummary(); });

if (!window.WizardApp || WizardApp.version !== 1) {
  say('Wizard system functions are unavailable. Open Photos through Wizard.', 'error');
} else {
  WizardApp.ready({ name: 'Photos', version: '0.4.0' });
  WizardApp.back.register({ canGoBack: () => false, back: () => false });
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);

// ---- Phone backup (from the Wizard OS Android app) ----
// The backup worker lives in Wizard OS for Android; this page drives it through the shell's Photos relay
// (photos.js, unchanged from the former view-only Photos app). The photo server is this app's own Immich, so the
// owner never types an address. In a browser or the desktop shell there is no relay and the button stays hidden.
const backup = { mode: '', status: null, error: '', busy: false, server: '', albumsOpen: false, albums: null, albumsError: '', confirmDisconnect: false };
const P = window.PhotosPage;
const framed = window.parent && window.parent !== window;
const bapi = P.client(msg => {
  if (!framed) throw Object.assign(new Error('no-bridge'), { code: 'no-bridge' });
  window.parent.postMessage(msg, location.origin);
}, {
  onStatus: s => { backup.status = s; if (backup.mode !== 'v1') { backup.mode = 'v1'; el('open-backup').hidden = false; } renderBackup(); },
  onResume: () => { if (backup.mode === 'v1') bapi.status().catch(() => {}); },
});
window.addEventListener('message', ev => { if (ev.origin === location.origin && ev.source === window.parent) bapi.onMessage(ev.data); });

function renderBackup() {
  const root = el('backup-root');
  if (document.activeElement && document.activeElement.matches && document.activeElement.matches('[data-ph-server]')) {
    root.querySelectorAll('[data-ph-status]').forEach(x => { x.textContent = backup.error || P.summary(backup.status); }); return;
  }
  root.innerHTML = backup.mode === 'v1' ? P.html(backup) : '<p class="ph-status">Checking…</p>';
  if (backup.status) el('open-backup').textContent = backup.status.enabled ? 'Phone backup · on' : 'Phone backup';
}
function backupTap(what, extra = {}) {
  backup.error = '';
  const pending = P.act(backup, bapi, what, extra);
  if (!pending) { renderBackup(); return; }
  if (what !== 'albums' && what !== 'album') backup.busy = what === 'run' ? 'run' : true;
  renderBackup();
  pending.catch(e => { backup.error = e.timeout ? 'The Wizard OS app did not answer · try again' : e.message; })
    .finally(() => { backup.busy = false; renderBackup(); });
}
el('backup-root').addEventListener('click', e => {
  const b = e.target.closest('[data-ph], [data-ph-album]'); if (!b || b.disabled) return;
  if (b.dataset.phAlbum != null) { backupTap('album', { id: b.dataset.phAlbum }); return; }
  const field = el('backup-root').querySelector('[data-ph-server]');
  backupTap(b.dataset.ph, field && field.value ? { server: field.value } : {});
});
el('backup-root').addEventListener('input', e => { if (e.target.matches('[data-ph-server]')) backup.server = e.target.value; });
el('open-backup').onclick = () => { WzSheet.open(el('backup-sheet')); renderBackup(); bapi.status().catch(() => {}); };
el('backup-sheet').addEventListener('close', () => { backup.albumsOpen = false; backup.confirmDisconnect = false; });
P.detect(bapi).then(mode => {
  if (mode !== 'v1' && mode !== 'v1-error') return; // browser, desktop or an old phone app: Immich phone app instructions stay in Agent
  backup.mode = 'v1';
  if (mode === 'v1-error') backup.error = 'Could not read backup status · try again';
  el('open-backup').hidden = false;
  renderBackup();
});
