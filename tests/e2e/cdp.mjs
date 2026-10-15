// Minimal Chrome DevTools Protocol driver (Node 22+: global WebSocket + fetch).
// Set CHROME to the browser to drive (Chrome, Chromium or chrome-headless-shell).
import { spawn } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BIN = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = ms => new Promise(r => setTimeout(r, ms));

export async function launch({ port = 9333, width = 1440, height = 900 } = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'cdp-'));
  const flags = [`--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--headless=new', '--no-first-run', '--hide-scrollbars', `--window-size=${width},${height}`];
  if (process.env.CI) flags.push('--no-sandbox');  // CI runners can't give Chrome its sandbox
  const proc = spawn(BIN, [...flags, 'about:blank'], { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 50; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); if (targets.length) break; } catch {} await sleep(100); }
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  let id = 0; const pending = new Map(); const logs = [];
  const listeners = [];
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const { res, rej } = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); }
    else if (m.method) {
      if (m.method === 'Runtime.exceptionThrown') logs.push('EXCEPTION: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
      if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(m.params.type)) logs.push(`console.${m.params.type}: ` + m.params.args.map(a => a.value ?? a.description).join(' '));
      if (m.method === 'Log.entryAdded' && ['error', 'warning'].includes(m.params.entry.level)) logs.push(`log.${m.params.entry.level}: ${m.params.entry.text} ${m.params.entry.url || ''}`);
      listeners.forEach(fn => fn(m));
    }
  });
  const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  const api = {
    logs, send,
    async viewport(w, h, mobile = false) { await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile }); },
    async theme(scheme) { await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }] }); },
    async goto(url) { const loaded = new Promise(r => { const t = setTimeout(r, 4000); listeners.push(function f(m) { if (m.method === 'Page.loadEventFired') { clearTimeout(t); listeners.splice(listeners.indexOf(f), 1); r(); } }); }); await send('Page.navigate', { url }); await loaded; await sleep(400); },
    async hash(h) { await send('Runtime.evaluate', { expression: `location.hash = ${JSON.stringify(h)}` }); await sleep(900); },
    async eval(expr) {
      const r = await send('Runtime.evaluate', { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error('eval: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
      return r.result.value;
    },
    async shot(path, full = false) {
      let clip;
      if (full) { const m = await send('Page.getLayoutMetrics'); clip = { x: 0, y: 0, width: m.cssContentSize.width, height: m.cssContentSize.height, scale: 1 }; }
      const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: full, ...(clip ? { clip } : {}) });
      writeFileSync(path, Buffer.from(r.data, 'base64'));
    },
    sleep,
    async close() { try { ws.close(); } catch {} proc.kill(); },
  };
  await api.viewport(width, height);
  return api;
}
