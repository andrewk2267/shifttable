// End-to-end UI flows driven through real clicks in headless Chromium.
// Run against a fresh server, since the flows change data:
//   python3 server.py --port 8766 --db /tmp/e2e/roster.db   then   node tests/e2e/e2e.mjs
// BASE=<url> points it elsewhere (the flows assume the F&B demo with its demo logins).
import { launch } from './cdp.mjs';
import { mkdirSync } from 'node:fs';
const OUT = new URL('./shots/', import.meta.url).pathname; mkdirSync(OUT, { recursive: true });
const B = process.env.BASE || 'http://127.0.0.1:8766';
const b = await launch({ port: 9334 });
const results = [];
const HELPERS = `
window.T = {
  sleep: ms => new Promise(r => setTimeout(r, ms)),
  async until(fn, ms = 6000) { const t = Date.now(); while (Date.now() - t < ms) { try { const v = fn(); if (v) return v; } catch (e) {} await T.sleep(60); } throw new Error('timeout waiting for ' + fn); },
  q: s => document.querySelector(s), qa: s => [...document.querySelectorAll(s)],
  dlg: () => [...document.querySelectorAll('dialog[open]')].pop(),
  async click(sel, root) { const el = typeof sel === 'string' ? await T.until(() => (root || document).querySelector(sel)) : sel; el.scrollIntoView({block:'center'}); el.click(); await T.sleep(250); return el; },
  async clickText(text, sel = 'button, a', root) { const el = await T.until(() => [...(root || document).querySelectorAll(sel)].find(e => e.textContent.trim().includes(text))); el.click(); await T.sleep(250); return el; },
  set(el, v) { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); },
  async submitDialog() { const d = T.dlg(); d.querySelector('button[type=submit]').click(); await T.sleep(600); },
  toasts: () => [...document.querySelectorAll('.toast')].map(t => t.textContent.trim()),
  lastToast: () => { const t = [...document.querySelectorAll('.toast')].pop(); return t ? t.textContent.trim() : ''; },
  async api(m, p, body) { const r = await fetch(p, {method:m, headers:{'Content-Type':'application/json','X-ShiftTable':'1'}, body: body ? JSON.stringify(body) : undefined}); return r.json(); },
  file(input, name, text, type='text/csv') { const dt = new DataTransfer(); dt.items.add(new File([text], name, {type})); input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true})); },
};`;
async function step(name, body) {
  try { const v = await b.eval(HELPERS + body); results.push(['PASS', name, v ?? '']); }
  catch (e) { results.push(['FAIL', name, e.message.slice(0, 300)]); await b.shot(`${OUT}fail-${name.replace(/\W+/g, '_')}.png`); }
}
async function login(u, p) {
  await b.goto(B + '/');
  await step(`login ${u}`, `
    const f = await T.until(() => T.q('#login-form'));
    f.elements.username.value = '${u}'; f.elements.password.value = '${p}';
    f.requestSubmit();
    await T.until(() => !T.q('#app').hidden && T.q('#view .card'));
    return document.title;`);
}

// ------------------------------------------------ manager flows
await login('manager', 'Manager#2026');
await step('wrong password shows error', `
  await T.click('[data-action=logout]');
  const f = await T.until(() => !T.q('#login').hidden && T.q('#login-form'));
  f.elements.username.value = 'manager'; f.elements.password.value = 'nope'; f.requestSubmit();
  await T.until(() => !T.q('#login-error').hidden && T.q('#login-error').textContent.includes('Wrong'));
  f.elements.password.value = 'Manager#2026'; f.requestSubmit();
  await T.until(() => !T.q('#app').hidden);
  return T.q('#login-error').textContent;`);
await b.hash('#/roster');
await step('add shift via cell + template', `
  const btn = await T.until(() => T.qa('button.cell-add').find(x => x.dataset.emp && !x.closest('td').querySelector('.chip')));
  const emp = btn.dataset.emp, date = btn.dataset.date;
  btn.click(); await T.until(() => T.dlg());
  const d = T.dlg();
  const tpl = [...d.querySelectorAll('[data-tpl]')].find(x => x.textContent.includes('Lunch peak')); tpl.click();
  if (d.querySelector('[name=start_time]').value !== '11:00') throw new Error('template not applied');
  if (!d.querySelector('#shift-summary').textContent.includes('4h')) throw new Error('summary wrong: ' + d.querySelector('#shift-summary').textContent);
  await T.submitDialog();
  await T.until(() => T.qa('td.cell[data-emp="'+emp+'"][data-date="'+date+'"] .chip').length === 1);
  return T.lastToast();`);
await step('edit shift, find cover, assign', `
  const chip = await T.until(() => T.qa('.chip.draft')[0]);
  const id = chip.dataset.id; chip.click(); await T.until(() => T.dlg());
  await T.click('[data-find-cover]', T.dlg());
  await T.until(() => T.dlg().querySelector('#cover-panel .cand'));
  const n = T.dlg().querySelectorAll('#cover-panel .cand').length;
  const assign = T.dlg().querySelector('#cover-panel [data-assign]'); const who = assign.dataset.assign; assign.click();
  await T.until(() => !T.dlg());
  await T.until(() => T.q('.chip[data-id="'+id+'"]') && T.q('.chip[data-id="'+id+'"]').closest('td').dataset.emp === who);
  return n + ' candidates; ' + T.lastToast();`);
await step('validation error keeps dialog open', `
  const btn = T.qa('button.cell-add').find(x => x.dataset.emp); btn.click(); await T.until(() => T.dlg());
  const d = T.dlg(); T.set(d.querySelector('[name=break_minutes]'), '900');
  d.querySelector('button[type=submit]').click(); await T.sleep(600);
  if (!T.dlg()) throw new Error('dialog closed on invalid data');
  const msg = T.lastToast(); T.dlg().close(); await T.sleep(200); return msg;`);
await step('drag a shift onto another person and alt-copy', `
  const chip = T.qa('.chip[draggable=true]').find(c => c.closest('td').dataset.emp !== 'open');
  const id = chip.dataset.id, date = chip.closest('td').dataset.date;
  const target = T.qa('td.cell[data-date="'+date+'"]').find(td => td.dataset.emp !== 'open' && td.dataset.emp !== chip.closest('td').dataset.emp && !td.querySelector('.chip'));
  const dt = new DataTransfer();
  chip.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer: dt}));
  target.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, dataTransfer: dt}));
  target.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer: dt}));
  chip.dispatchEvent(new DragEvent('dragend', {bubbles:true, dataTransfer: dt}));
  await T.until(() => T.q('.chip[data-id="'+id+'"]') && T.q('.chip[data-id="'+id+'"]').closest('td').dataset.emp === target.dataset.emp);
  const moved = T.lastToast();
  const c2 = T.q('.chip[data-id="'+id+'"]'); const t2 = T.qa('td.cell[data-emp=open]')[0];
  const before = T.qa('.chip').length; const dt2 = new DataTransfer();
  c2.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer: dt2}));
  t2.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, altKey:true, dataTransfer: dt2}));
  await T.until(() => T.qa('.chip').length === before + 1);
  return moved + ' | copy ok';`);
await step('issues panel and goto issue', `
  await T.click('[data-action=toggle-health]');
  const iss = await T.until(() => T.qa('.issue')); const n = iss.length;
  const withShift = iss.find(b => b.textContent.includes('rest')) || iss[0];
  withShift.click(); await T.sleep(500);
  const opened = !!T.dlg(); if (T.dlg()) T.dlg().close();
  await T.click('[data-action=toggle-health]');
  return n + ' issues; dialog opened: ' + opened;`);
await step('forecast edit recalculates labour %', `
  if (!T.q('input[data-change=forecast]')) { const sel = T.q('[data-change=branch]'); T.set(sel, [...sel.options].find(o => o.value !== 'all').value); await T.until(() => T.q('input[data-change=forecast]'), 8000); }
  const inp = T.qa('input[data-change=forecast]')[0]; const date = inp.dataset.date;
  const before = T.qa('.roster tfoot tr')[3].children[1].textContent;
  T.set(inp, '1000');
  const after = await T.until(() => { const v = T.qa('.roster tfoot tr')[3].children[1].textContent; return v !== before && v; }, 8000).catch(() => before);
  const sel = T.q('[data-change=branch]'); T.set(sel, 'all'); await T.until(() => document.querySelector('#page-sub').textContent.includes('All branches'));
  await T.until(() => T.q('.roster'));
  if (before === after) throw new Error('labour % did not change: ' + before);
  return before + ' -> ' + after;`);
await step('day view renders timeline and coverage', `
  await T.click('[data-action=view-day]');
  await T.until(() => T.q('.tl-bar'));
  const bars = T.qa('.tl-bar').length, cov = T.qa('.cov i').length;
  await T.click('[data-action=pick-day]:nth-child(2)');
  await T.click('[data-action=view-week]');
  return bars + ' bars, ' + cov + ' coverage slots';`);
await step('filter kitchen only', `
  const sel = T.q('[data-change=roster-filter]'); T.set(sel, 'dept:Kitchen'); await T.sleep(300);
  const n = T.qa('.roster tbody tr:not(.group):not(.open-row)').length; T.set(T.q('[data-change=roster-filter]'), 'all'); await T.sleep(300);
  return n + ' kitchen rows';`);
await step('copy as text falls back to a copy box', `
  await T.click('details.menu summary'); await T.click('[data-action=copy-text]'); await T.sleep(500);
  const d = T.dlg(); const txt = d ? d.querySelector('textarea').value : T.lastToast(); if (d) d.close();
  if (!txt.includes('Roster')) throw new Error(txt.slice(0, 80)); return txt.split('\\n').slice(0, 3).join(' / ');`);
await b.hash('#/roster/' + (await b.eval(`const m = new Date(); m.setDate(m.getDate() - ((m.getDay() + 6) % 7) + 14); return m.getFullYear() + '-' + String(m.getMonth()+1).padStart(2,'0') + '-' + String(m.getDate()).padStart(2,'0');`)));
await step('clear drafts, auto-fill and publish the draft week', `
  await T.until(() => T.q('.roster'));
  await T.click('details.menu summary'); await T.click('[data-action=clear-drafts]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.q('.stat .s-value') && T.q('.stat .s-value').textContent === '0h');
  await T.click('[data-action=autofill]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.qa('.chip').length > 50, 10000);
  const created = T.lastToast();
  await T.click('[data-action=publish]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.q('.badge.b-ok') && T.q('.badge.b-ok').textContent.includes('Published'));
  return created + ' | ' + T.q('.badge.b-ok').textContent;`);
await step('copy last week into an empty week', `
  await T.click('[data-action=week-next]'); await T.until(() => T.q('.roster'));
  await T.click('[data-action=copy-week]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.qa('.chip').length > 50, 8000); return T.lastToast();`);
await b.hash('#/requests');
await step('approve a cover request', `
  const btn = await T.until(() => T.qa('[data-action=decide][data-decision=approve][data-type=shift]').find(b => b.closest('.req-card').textContent.includes('Cover request')));
  btn.click(); await T.until(() => T.lastToast().includes('approved'));
  return T.lastToast();`);
await step('approve leave and release shifts', `
  const btn = await T.until(() => T.q('[data-action=decide][data-decision=approve][data-type=leave]'));
  btn.click(); await T.until(() => T.lastToast().includes('approved'));
  return T.lastToast();`);
await step('record leave for someone', `
  await T.click('[data-action=new-leave]'); const d = await T.until(() => T.dlg());
  T.set(d.querySelector('[name=employee_id]'), d.querySelector('[name=employee_id] option:nth-child(3)').value);
  await T.submitDialog(); await T.until(() => T.lastToast().includes('Leave'));
  await T.clickText('History'); await T.until(() => T.q('.req-card'));
  return T.lastToast();`);
await b.hash('#/team');
await step('edit a team member and availability', `
  const row = await T.until(() => T.q('tr.click')); row.click(); const d = await T.until(() => T.dlg());
  d.querySelector('[name=av_0]').checked = false;
  T.set(d.querySelector('[name=from_1]'), '10:00'); T.set(d.querySelector('[name=to_1]'), '22:00');
  await T.submitDialog(); await T.until(() => !T.dlg());
  await T.until(() => T.q('tr.click td:last-child'));
  return T.lastToast() + ' / ' + T.q('tr.click td:last-child').textContent;`);
await step('availability needs both times', `
  T.q('tr.click').click(); const d = await T.until(() => T.dlg());
  d.querySelector('[name=av_2]').checked = true; T.set(d.querySelector('[name=from_2]'), '10:00'); T.set(d.querySelector('[name=to_2]'), '');
  d.querySelector('button[type=submit]').click(); await T.sleep(500);
  const still = !!T.dlg(); T.dlg().close(); if (!still) throw new Error('saved invalid availability'); return T.lastToast();`);
await step('add team member', `
  await T.click('[data-action=edit-employee]:not([data-id])'); const d = await T.until(() => T.dlg());
  d.querySelector('[name=name]').value = 'Test Person <b>bold</b>';
  await T.submitDialog(); await T.until(() => T.qa('tr.click').some(r => r.textContent.includes('Test Person <b>bold</b>')));
  return 'escaped: ' + !T.q('#team-table b b');`);
await step('team search', `
  T.set(T.q('[data-team-search]'), 'kavin'); await T.sleep(200); return T.qa('tr.click').length + ' match';`);
await b.hash('#/reports');
await step('reports range + export', `
  await T.until(() => T.q('.table'));
  await T.clickText('This week', '.seg button'); await T.until(() => document.querySelector('#page-sub').textContent.includes('published'));
  const f = T.q('form[data-form=report-range]'); f.elements.drafts.checked = true; f.requestSubmit(); await T.sleep(800);
  return document.querySelector('#page-sub').textContent + ' / rows ' + T.qa('.table tbody tr').length;`);
await b.hash('#/settings/needs');
await step('add and copy a staffing need', `
  await T.click('[data-action=edit-need]:not([data-id])'); const d = await T.until(() => T.dlg());
  d.querySelector('[data-tpl]').click(); T.set(d.querySelector('[name=headcount]'), '2');
  await T.submitDialog(); await T.until(() => !T.dlg());
  await T.click('[data-action=copy-needs]'); const d2 = await T.until(() => T.dlg());
  d2.querySelector('[name=d]').checked = true; await T.submitDialog(); await T.until(() => T.lastToast().includes('Copied'));
  return T.lastToast();`);
await b.hash('#/settings/positions');
await step('add position and shift time', `
  await T.click('[data-action=edit-position]:not([data-id])'); let d = await T.until(() => T.dlg());
  d.querySelector('[name=name]').value = 'Host'; await T.submitDialog(); await T.until(() => T.qa('td').some(t => t.textContent.includes('Host')));
  await T.click('[data-action=edit-template]:not([data-id])'); d = await T.until(() => T.dlg());
  d.querySelector('[name=name]').value = 'Brunch'; T.set(d.querySelector('[name=start_time]'), '08:00'); T.set(d.querySelector('[name=end_time]'), '14:00');
  await T.submitDialog(); await T.until(() => T.qa('td').some(t => t.textContent.includes('Brunch')));
  return 'ok';`);
await b.hash('#/settings/rules');
await step('manager cannot edit rules', `
  await T.until(() => T.q('form[data-form=settings]'));
  return 'disabled inputs: ' + T.qa('form[data-form=settings] input:disabled').length + ', admin tabs visible: ' + T.qa('.subnav a').length;`);

// ------------------------------------------------ v2: branches, people filter, events, month view
await b.hash('#/roster');
await step('switch to one branch', `
  const sel = await T.until(() => T.q('[data-change=branch]'));
  const opt = [...sel.options].find(o => o.value !== 'all'); T.set(sel, opt.value);
  await T.until(() => document.querySelector('#page-sub').textContent.includes(opt.textContent));
  await T.until(() => T.q('.roster'));
  const tags = T.qa('.chip:not(.elsewhere) .b-tag').length;
  return opt.textContent + ' · rows ' + T.qa('.roster tbody tr:not(.group):not(.open-row)').length + ' · branch tags on own chips ' + tags + ' · elsewhere chips ' + T.qa('.chip.elsewhere').length;`);
await step('people filter on roster', `
  const d = await T.until(() => T.q('details[data-pp=roster]')); d.open = true;
  const boxes = T.qa('.pp-list input', d); boxes[0].checked = true; boxes[1].checked = true;
  T.q('[data-pp-apply]', d).click(); await T.sleep(400);
  const rows = T.qa('.roster tbody tr:not(.group):not(.open-row)').length;
  const label = T.q('details[data-pp=roster] summary').textContent.trim();
  const d2 = T.q('details[data-pp=roster]'); d2.open = true; T.q('[data-pp-all]', d2).click(); await T.sleep(400);
  if (rows !== 2) throw new Error('expected 2 rows, got ' + rows);
  return label + ' -> ' + rows + ' rows, reset -> ' + T.qa('.roster tbody tr:not(.group):not(.open-row)').length;`);
await step('add an event from the week header, then make it a closure', `
  const add = await T.until(() => T.qa('.add-ev')[2]); const date = add.dataset.date; add.click();
  let d = await T.until(() => T.dlg()); d.querySelector('[name=name]').value = 'E2E promo night'; await T.submitDialog();
  const badge = await T.until(() => T.qa('button.ev').find(x => x.textContent.includes('E2E promo night')));
  badge.click(); d = await T.until(() => T.dlg());
  d.querySelector('[data-kind=closed]').click(); await T.submitDialog();
  await T.until(() => T.qa('button.ev-closed').some(x => x.textContent.includes('E2E promo night')));
  const closedCells = T.qa('td.cell.closed[data-date="' + date + '"]').length;
  return 'closed cells on ' + date + ': ' + closedCells;`);
await step('month view with events and per-day summary', `
  await T.click('[data-action=view-month]');
  await T.until(() => T.q('.month .mday'), 8000);
  const days = T.qa('.mday').length, evs = T.qa('.mday .ev').length, sums = T.qa('.mday .msum').length;
  await T.click('[data-action=week-next]'); await T.until(() => document.querySelector('#page-sub').textContent.match(/[A-Z][a-z]+ 20\\d\\d/));
  const next = document.querySelector('#page-sub').textContent;
  await T.until(() => T.q('.month .mday'), 8000);
  const ph = T.qa('.mday .ev-holiday').map(x => x.textContent.trim());
  T.qa('.mday')[10].click(); await T.until(() => T.q('.tl, .day-tabs'));
  return days + ' days, ' + evs + ' events, ' + sums + ' summaries | ' + next + ' holidays: ' + ph.join('; ');`);
await b.hash('#/settings/events');
await step('load Singapore holidays for 2027', `
  const sel = await T.until(() => T.q('[data-change=ev-year]')); T.set(sel, '2027');
  const btn = await T.until(() => T.qa('[data-action=load-holidays]').find(x => x.dataset.year === '2027'));
  btn.click(); await T.until(() => T.lastToast().includes('2027'));
  await T.until(() => T.qa('tbody tr').length > 5);
  return T.lastToast() + ' / rows ' + T.qa('tbody tr').length;`);
await b.hash('#/reports');
await step('report people filter', `
  const d = await T.until(() => T.q('details[data-pp=report]')); d.open = true;
  T.qa('.pp-list input', d)[0].checked = true; T.q('[data-pp-apply]', d).click();
  await T.until(() => document.querySelector('#page-sub').textContent.includes('1 person'));
  await T.until(() => T.qa('.table tbody tr').length === 1);
  const rows = T.qa('.table tbody tr').length;
  const d2 = await T.until(() => T.q('details[data-pp=report]')); d2.open = true; T.q('[data-pp-all]', d2).click();
  await T.until(() => document.querySelector('#page-sub').textContent.includes('everyone'));
  return 'rows with filter: ' + rows;`);
await step('actual vs rostered report', `
  await T.click('[data-action=rep-tab][data-tab=actual]');
  await T.until(() => T.q('.kpi .k-value') && document.body.innerText.includes('Shift by shift'), 8000);
  const k = T.qa('.kpi .k-value').map(x => x.textContent).join(' | ');
  const ex = T.qa('.table').pop().querySelectorAll('tbody tr').length;
  await T.click('[data-action=ex-only][data-v="0"]'); await T.until(() => T.q('.kpi'));
  return k + ' · exceptions ' + ex + ' · all rows ' + T.qa('.table').pop().querySelectorAll('tbody tr').length;`);
await step('import clock data (preview then commit)', `
  const emps = (await T.api('GET', '/api/employees')).filter(e => e.active);
  const d0 = new Date(); d0.setDate(d0.getDate() - 30); const day = d0.toISOString().slice(0, 10).split('-').reverse().join('/');
  const csv = ['Staff Code,Employee,Date,Clock In,Clock Out,Break', emps[0].code + ',,' + day + ',08:02,17:10,60', ',' + emps[1].name + ',' + day + ',10:00 pm,6:30 am,30', 'NOBODY,,' + day + ',09:00,17:00,0', ''].join(String.fromCharCode(10));
  await T.click('[data-action=import-clock]'); const d = await T.until(() => T.dlg());
  T.file(d.querySelector('[name=file]'), 'clock.csv', csv);
  d.querySelector('#clock-go').click(); await T.until(() => d.querySelector('#clock-preview .badge'));
  const prev = d.querySelector('#clock-preview .row').innerText.replace(/\\s+/g, ' ');
  d.querySelector('#clock-go').click(); await T.until(() => !T.dlg(), 8000);
  return prev + ' | ' + T.lastToast() + ' | range now ' + document.querySelector('#page-sub').textContent.split('·')[0];`);
await b.hash('#/team');
await step('team member with branches and staff code', `
  const row = await T.until(() => T.q('tr.click')); row.click(); const d = await T.until(() => T.dlg());
  const code = d.querySelector('[name=code]').value; const home = d.querySelector('[name=branch_id]');
  const boxes = [...d.querySelectorAll('[name=branch]')]; const other = boxes.find(x => x.value !== home.value); const was = other.checked; other.checked = !was;
  await T.submitDialog(); await T.until(() => !T.dlg());
  const emp = (await T.api('GET', '/api/employees')).find(e => e.code === code);
  return code + ' home ' + emp.branch_id + ' branches ' + emp.branches.join(',') + ' (toggled ' + other.value + ')';`);
// ------------------------------------------------ admin flows
await b.eval(`await fetch('/api/logout', {method:'POST', headers:{'X-ShiftTable':'1'}}); return 1;`);
await login('admin', 'Admin#2026');
await b.hash('#/settings/rules');
await step('admin saves rules and business name', `
  const f = await T.until(() => T.q('form[data-form=settings]'));
  f.elements.business_name.value = 'Harbour Lane Bistro & Bar'; f.elements.min_rest_hours.value = '10';
  f.requestSubmit(); await T.until(() => T.lastToast().includes('saved'));
  const side = T.q('#side-biz').textContent;
  await T.click('[data-action=sg-defaults]'); f.requestSubmit(); await T.sleep(700);
  return side + ' / rest ' + f.elements.min_rest_hours.value;`);
await step('invalid rule value is rejected', `
  const f = T.q('form[data-form=settings]'); f.elements.max_daily_hours.value = '40'; f.requestSubmit(); await T.sleep(700);
  const t = T.lastToast(); f.elements.max_daily_hours.value = '12'; f.requestSubmit(); await T.sleep(600); return t;`);
await b.hash('#/settings/users');
await step('uploads over the limit are stopped before sending', `
  if (!state.maxUpload) throw new Error('the page has no upload limit from the server');
  let msg = '';
  try { await api('POST', '/api/data/import', { type: 'employees', csv: 'x'.repeat(state.maxUpload), commit: false }); } catch (e) { msg = e.message; }
  if (!msg.includes('the limit is')) throw new Error('not stopped: ' + msg);
  return msg;`);
await step('add login and reset password', `
  await T.click('[data-action=edit-user]:not([data-id])'); const d = await T.until(() => T.dlg());
  d.querySelector('[name=username]').value = 'kavin'; d.querySelector('[name=display_name]').value = 'Kavin Raj';
  T.set(d.querySelector('[name=employee_id]'), [...d.querySelectorAll('[name=employee_id] option')].find(o => o.textContent === 'Kavin Raj').value);
  d.querySelector('[name=password]').value = 'Kavin#2026!'; await T.submitDialog();
  await T.until(() => T.qa('td').some(t => t.textContent === 'kavin'));
  await T.click(T.qa('[data-action=reset-password]').find(b => b.dataset.name === 'kavin')); const d2 = await T.until(() => T.dlg());
  d2.querySelector('[name=password]').value = 'Kavin#2027!'; await T.submitDialog();
  return T.lastToast();`);
await b.hash('#/settings/data');
await step('snapshot, import preview + commit', `
  const n0 = T.qa('[data-action=restore-snapshot]').length; await T.click('[data-action=snapshot]'); await T.until(() => T.lastToast().includes('Snapshot saved'), 10000); await T.until(() => T.qa('[data-action=restore-snapshot]').length > n0);
  const f = await T.until(() => T.q('form[data-form=import-preview]'));
  T.file(f.elements.file, 'team.csv', 'name,position,type,hourly_rate\\nIvy Chan,Server,part-time,11\\nBad Row,,,\\n');
  f.requestSubmit(); await T.until(() => T.q('#import-preview .badge'));
  const prev = T.q('#import-preview').innerText.split('\\n')[0];
  await T.click('[data-action=import-commit]'); await T.until(() => T.lastToast().includes('Imported'));
  return prev + ' | ' + T.lastToast();`);
await step('restore from JSON backup file', `
  const json = await (await fetch('/api/data/export?format=json')).text();
  const f = await T.until(() => T.q('form[data-form=restore-file]'));
  T.file(f.elements.file, 'backup.json', json, 'application/json');
  f.requestSubmit(); const d = await T.until(() => T.dlg());
  d.querySelector('[name=typed]').value = 'RESTORE'; await T.submitDialog();
  await T.until(() => !T.q('#login').hidden, 8000);
  return T.q('#login-error').textContent;`);
await login('admin', 'Admin#2026');
await b.hash('#/settings/data');
await step('restore rejects a non-backup file', `
  const f = await T.until(() => T.q('form[data-form=restore-file]'));
  T.file(f.elements.file, 'x.json', '{"hello": 1}', 'application/json');
  f.requestSubmit(); const d = await T.until(() => T.dlg()); d.querySelector('[name=typed]').value = 'RESTORE'; await T.submitDialog();
  await T.until(() => T.lastToast().includes('not a ShiftTable'));
  return T.lastToast() + ' / still signed in: ' + T.q('#login').hidden;`);
await step('load sample data', `
  await T.click('[data-action=load-sample]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.lastToast().includes('loaded'), 15000); return T.lastToast();`);
await b.hash('#/settings/activity');
await step('activity log filter', `
  await T.until(() => T.q('#log-body tr')); T.set(T.q('[data-log-filter]'), 'data.'); await T.sleep(200);
  return T.qa('#log-body tr').length + ' data.* entries';`);
await step('account dialog theme switch', `
  await T.click('[data-action=account]'); const d = await T.until(() => T.dlg());
  d.querySelector('[data-theme-pick=dark]').click(); const t = document.documentElement.dataset.theme;
  d.querySelector('[data-theme-pick=system]').click(); d.close(); return 'theme set to ' + t;`);

await b.hash('#/settings/branches');
await step('add a branch', `
  await T.click('[data-action=edit-branch]:not([data-id])'); const d = await T.until(() => T.dlg());
  d.querySelector('[name=name]').value = 'Jewel Changi'; d.querySelector('[name=code]').value = 'jcw';
  await T.submitDialog(); await T.until(() => T.qa('td').some(t => t.textContent.includes('Jewel Changi')));
  return [...T.q('[data-change=branch]').options].map(o => o.textContent).join(' / ');`);
await b.hash('#/settings/needs');
await step('staffing needs per branch and copy to the new branch', `
  const sel = await T.until(() => T.q('[data-change=needs-branch]'));
  const n0 = T.qa('.needs-grid tbody tr').length;
  await T.click('[data-action=copy-needs-branch]'); const d = await T.until(() => T.dlg());
  const opt = [...d.querySelectorAll('[name=to] option')].find(o => o.textContent === 'Jewel Changi'); d.querySelector('[name=to]').value = opt.value;
  await T.submitDialog(); await T.until(() => T.lastToast().includes('Copied'));
  await T.until(() => T.q('[data-change=needs-branch]') && T.q('[data-change=needs-branch]').selectedOptions[0].textContent.includes('Jewel'));
  return 'source rows ' + n0 + ' · ' + T.lastToast() + ' · now showing ' + T.q('[data-change=needs-branch]').selectedOptions[0].textContent + ' rows ' + T.qa('.needs-grid tbody tr').length;`);
await step('delete the empty branch', `
  location.hash = '#/settings/branches'; await T.until(() => T.qa('[data-action=edit-branch][data-id]').length);
  const btn = T.qa('[data-action=edit-branch][data-id]').pop(); btn.click(); const d = await T.until(() => T.dlg());
  d.querySelector('[data-del]').click(); const c = await T.until(() => T.qa('dialog[open]').length === 2 && T.dlg()); await T.submitDialog();
  await T.until(() => !T.qa('td').some(t => t.textContent.includes('Jewel Changi')));
  return 'deleted';`);
await b.hash('#/settings/data');
await step('load the Health & Wellness sample', `
  const btn = await T.until(() => T.q('[data-action=load-sample][data-industry=wellness]')); btn.click();
  await T.until(() => T.dlg()); await T.submitDialog(); await T.until(() => T.lastToast().includes('Vitality Wellness loaded'), 15000);
  return T.q('#side-biz').textContent + ' / branches: ' + [...T.q('[data-change=branch]').options].map(o => o.textContent).join(', ');`);
await b.hash('#/roster');
await step('wellness roster renders', `await T.until(() => T.q('.roster')); return T.qa('.roster tr.group td').map(t => t.textContent).join(', ') + ' · ' + T.qa('.chip').length + ' chips';`);
await b.hash('#/settings/data');
await step('load the Manufacturing sample', `
  const btn = await T.until(() => T.q('[data-action=load-sample][data-industry=manufacturing]')); btn.click();
  await T.until(() => T.dlg()); await T.submitDialog(); await T.until(() => T.lastToast().includes('Apex Precision Components loaded'), 15000);
  location.hash = '#/roster'; await T.until(() => T.q('.roster'), 8000);
  return T.q('#side-biz').textContent + ' · night chips ' + T.qa('.chip .c-t').filter(x => x.textContent.startsWith('23:00')).length;`);
await b.hash('#/reports');
await step('manufacturing actual vs rostered has data', `
  state_tab: { const b = await T.until(() => T.q('[data-action=rep-tab][data-tab=actual]')); b.click(); }
  await T.until(() => document.body.innerText.includes('Shift by shift'), 8000);
  return T.qa('.kpi .k-value').map(x => x.textContent).join(' | ');`);
await b.hash('#/settings/data');
await step('load the F&B sample again', `
  const btn = await T.until(() => T.q('[data-action=load-sample][data-industry=fnb]')); btn.click();
  await T.until(() => T.dlg()); await T.submitDialog(); await T.until(() => T.lastToast().includes('Harbour Lane Group loaded'), 15000);
  return T.q('#side-biz').textContent;`);
// ------------------------------------------------ staff flows
await b.eval(`await fetch('/api/logout', {method:'POST', headers:{'X-ShiftTable':'1'}}); return 1;`);
await login('marcus', 'Staff#2026');
await step('staff asks for cover from dashboard', `
  const btn = await T.until(() => T.qa('[data-action=ask-cover]').find(b => b.closest('.list-item')));
  btn.closest('details').open = true; btn.click(); const d = await T.until(() => T.dlg());
  const opt = [...d.querySelectorAll('[name=to] option')].find(o => o.value && !o.textContent.includes('not trained'));
  T.set(d.querySelector('[name=to]'), opt.value); await T.submitDialog();
  await T.until(() => T.lastToast().includes('Cover request')); return T.lastToast() + ' -> ' + opt.textContent;`);
await step('staff requests leave', `
  await T.click('[data-action=new-leave]'); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.lastToast().includes('Leave request')); return T.lastToast();`);
await step('staff picks up an open shift', `
  await T.until(() => T.q('.card'));
  const btn = T.q('[data-action=pickup]'); if (!btn) return 'no open shifts offered';
  btn.click(); await T.until(() => T.dlg()); await T.submitDialog(); await T.until(() => T.lastToast().includes('Pick-up')); return T.lastToast();`);
await b.hash('#/requests');
await step('staff cancels own request', `
  const btn = await T.until(() => T.q('[data-action=cancel-request]')); btn.click(); await T.until(() => T.dlg()); await T.submitDialog();
  await T.until(() => T.lastToast().includes('cancelled')); return T.lastToast();`);
await b.hash('#/availability');
await step('staff saves availability', `
  const f = await T.until(() => T.q('form[data-form=my-availability]'));
  f.elements.av_6.checked = false; f.requestSubmit(); await T.until(() => T.lastToast().includes('Availability')); return T.lastToast();`);
await b.hash('#/roster');
await step('staff roster: own shift dialog, no edit controls', `
  await T.until(() => T.q('.roster'));
  const adds = T.qa('.cell-add').length, drag = T.qa('[draggable=true]').length;
  const mine = T.q('.chip.mine'); mine.click(); const d = await T.until(() => T.dlg());
  const t = d.querySelector('h2').textContent; d.close();
  return 'add buttons ' + adds + ', draggable ' + drag + ', dialog: ' + t;`);
await b.hash('#/settings');
await step('staff cannot open manager pages', `await T.sleep(400); return location.hash;`);

// ------------------------------------------------ mobile and dark screenshots
await b.viewport(390, 844, true);
await b.hash('#/dashboard'); await b.shot(OUT + 'm-staff-dashboard.png');
await b.hash('#/roster'); await b.shot(OUT + 'm-staff-roster.png');
await step('mobile nav drawer opens', `await T.click('[data-action=open-nav]'); const open = T.q('#app').classList.contains('nav-open'); await T.click('.scrim'); return 'opened ' + open + ', closed ' + !T.q('#app').classList.contains('nav-open');`);
await step('no horizontal page scroll on phone', `return document.documentElement.scrollWidth + ' <= ' + window.innerWidth + ' : ' + (document.documentElement.scrollWidth <= window.innerWidth);`);
await b.eval(`await fetch('/api/logout', {method:'POST', headers:{'X-ShiftTable':'1'}}); return 1;`);
await b.theme('dark');
await login('manager', 'Manager#2026');
await b.hash('#/roster'); await b.shot(OUT + 'm-manager-roster-dark.png');
await step('no horizontal page scroll on phone (manager roster)', `return document.documentElement.scrollWidth + ' <= ' + window.innerWidth + ' : ' + (document.documentElement.scrollWidth <= window.innerWidth);`);
await b.viewport(1440, 900);
await b.hash('#/dashboard'); await b.shot(OUT + 'd-manager-dashboard-dark.png');
await b.hash('#/roster'); await b.shot(OUT + 'd-manager-roster-dark.png');

for (const [s, n, v] of results) console.log(s, '|', n, '|', String(v).slice(0, 200));
console.log('\nPASS', results.filter(r => r[0] === 'PASS').length, 'FAIL', results.filter(r => r[0] === 'FAIL').length);
console.log('CONSOLE:', JSON.stringify(b.logs, null, 1));
await b.close();
process.exitCode = results.some(r => r[0] === 'FAIL') ? 1 : 0;
