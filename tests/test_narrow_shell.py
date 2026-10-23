"""0.9.0 (F8) measured, not eyeballed: at 480x900 the dashboard
and a real project's interview page must not overflow
horizontally (document.documentElement.scrollWidth <=
window.innerWidth), and the interview's chat column must not be
crushed beside a fixed sidebar (the fitview clips instead of
scrolling, so raw scrollWidth alone would pass a broken page —
the panel's rendered width is asserted too).

Driven through the Chrome DevTools Protocol against the real
app server, the way the parent's manual app test found F8.
Skips when node or a chrome headless binary is unavailable.

Run with:  python3 -m unittest tests.test_narrow_shell
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weaverbird import app as app_module  # noqa: E402
from weaverbird import interview, scope, state  # noqa: E402
from weaverbird import template_text  # noqa: E402

NODE = shutil.which("node")


def _chrome() -> str | None:
    candidates = [
        os.environ.get("WEAVERBIRD_CHROME", ""),
        "/home/hatch/assets/artifacts/headless-shell/"
        "chrome-headless-shell",
        shutil.which("chrome-headless-shell") or "",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


CHROME = _chrome()

_PROBE_JS = r"""
const { spawn } = require('child_process');
const http = require('http');
const [chromePath, portStr, urlsJson] = process.argv.slice(2);
const port = parseInt(portStr, 10);
const urls = JSON.parse(urlsJson);
const chrome = spawn(chromePath, ['--headless', '--no-sandbox',
  '--disable-gpu', '--remote-debugging-port=' + port,
  '--window-size=480,900', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] });
chrome.stderr.on('data', () => {});
function getJson(path, method) { return new Promise((resolve, reject) => {
  const req = http.request({ host: '127.0.0.1', port, path,
    method: method || 'GET' }, res => {
    let body = ''; res.on('data', c => body += c);
    res.on('end', () => { try { resolve(JSON.parse(body)); }
      catch (e) { reject(e); } });
  });
  req.on('error', reject); req.end();
}); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
let msgId = 0; const pending = new Map();
async function measure(wsUrl, url) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  const send = (method, params) => new Promise(resolve => {
    const id = ++msgId; pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  ws.onmessage = ev => { const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result);
      pending.delete(m.id); } };
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',
    { width: 480, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    const r = await send('Runtime.evaluate',
      { expression: 'document.readyState', returnByValue: true });
    if (r && r.result && r.result.value === 'complete') break;
    await sleep(200);
  }
  await sleep(400);
  const expr = `JSON.stringify({
    sw: document.documentElement.scrollWidth,
    iw: window.innerWidth,
    chatwrap: (document.querySelector('.chatwrap') || {})
      .getBoundingClientRect?.().width || 0,
    side: (document.querySelector('.side') || {})
      .getBoundingClientRect?.().height || 0 })`;
  const r = await send('Runtime.evaluate',
    { expression: expr, returnByValue: true });
  ws.close();
  return JSON.parse(r.result.value);
}
(async () => {
  try {
    for (let i = 0; i < 100; i++) {
      try { await getJson('/json/version'); break; }
      catch (e) { await sleep(200); } }
    const out = [];
    for (const url of urls) {
      const target = await getJson(
        '/json/new?' + encodeURIComponent('about:blank'), 'PUT');
      const m = await measure(target.webSocketDebuggerUrl, url);
      out.push({ url, ...m });
    }
    console.log(JSON.stringify(out));
  } catch (e) {
    console.error('MEASURE-ERROR ' + (e && e.message || e));
    process.exitCode = 2;
  } finally { chrome.kill('SIGKILL'); }
})();
"""


def _filled_scope_text() -> str:
    lines = []
    for line in template_text("SCOPE.md").splitlines():
        stripped = line.strip()
        if stripped.startswith("- REQUIRED"):
            name = stripped[len("- REQUIRED"):].strip() \
                .partition(":")[0].strip()
            hint = {"Dimension": "2D", "Scope class": "Micro"}.get(
                name, "Filled in for the test")
            lines.append(f"- REQUIRED {name}: {hint}")
        elif stripped.startswith("- Telemetry:"):
            lines.append("- Telemetry: no")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


@unittest.skipUnless(NODE and CHROME,
                     "needs node and a chrome headless binary")
class NarrowShellHeadlessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp) / "workspace"
        self.project = self.workspace / "bee-game"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(_filled_scope_text(),
                                              encoding="utf-8")
        scope.approve_scope(self.project / "SCOPE.md", by="Tester")
        state.init_state(self.project)
        interview.start(self.project)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = self.server.url.rstrip("/")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _measure(self, paths):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        script = Path(self.tmp) / "probe.js"
        script.write_text(_PROBE_JS, encoding="utf-8")
        urls = [self.base + p for p in paths]
        result = subprocess.run(
            ["node", str(script), CHROME, str(port), json.dumps(urls)],
            capture_output=True, text=True, timeout=240)
        self.assertEqual(result.returncode, 0,
                         result.stderr[-400:])
        return {row["url"]: row for row in json.loads(result.stdout)}

    def test_no_horizontal_overflow_at_480(self):
        rows = self._measure(["/", "/project/bee-game/scope"])
        for path, row in ((p, rows[self.base + p])
                          for p in ("/", "/project/bee-game/scope")):
            self.assertEqual(row["iw"], 480, path)
            self.assertLessEqual(
                row["sw"], row["iw"],
                f"{path} overflows horizontally at 480px: "
                f"scrollWidth {row['sw']} > innerWidth {row['iw']}")

    def test_interview_is_not_crushed_beside_the_sidebar(self):
        rows = self._measure(["/project/bee-game/scope"])
        row = rows[self.base + "/project/bee-game/scope"]
        # The sidebar is a slim top bar now (short), and the chat
        # column gets the full content width (was 144px crushed
        # beside the 248px sidebar before the fix).
        self.assertLess(row["side"], 120)
        self.assertGreater(row["chatwrap"], 400)


if __name__ == "__main__":
    unittest.main()
