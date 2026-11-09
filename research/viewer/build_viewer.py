"""Build a self-contained replay page from a recorded scenario JSON.

    python research/viewer/build_viewer.py research/scenarios/fleet_departure.json

writes research/viewer/<scenario>.html with the recording inlined (opens from disk, no server).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Replay</title>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1f;--muted:#6b6b70;--line:#d9d9dc;--ok:#1a7f4b;--bad:#b3261e;--tx:#2463eb;--relay:#7a5af8;--base:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--muted:#9a9aa3;--line:#33333a;--ok:#3ecf8e;--bad:#ff6b5e;--tx:#6aa0ff;--relay:#a78bfa;--base:#fb923c}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px} p.sub{color:var(--muted);margin:0 0 12px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
canvas{width:100%;height:260px;display:block}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input[type=range]{flex:1;min-width:160px}
button{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.stat b{display:block;font-size:20px}
.stat span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px} td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
.ok{color:var(--ok)} .bad{color:var(--bad)}
</style></head><body><main>
<h1>Fleet replay: <span id="name"></span></h1>
<p class="sub" id="desc"></p>
<div class="card"><canvas id="cv"></canvas>
<div class="controls"><button id="play">Play</button><input type="range" id="slider" min="0" value="0"><span id="clock"></span></div></div>
<div class="card stats" id="stats"></div>
<div class="card"><table><thead><tr><th>t (s)</th><th>lead (km)</th><th>lead→base SNR</th><th>transmissions (slot:node)</th><th>base</th></tr></thead><tbody id="rows"></tbody></table></div>
<p class="sub">Every burst is real entroptics-rf IQ on an emulated free-space air (Friis at 915 MHz, collisions summed, receiver noise), decoded blind with the key alone; messages flood with no acks.</p>
</main>
<script>
const R = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
document.getElementById('name').textContent = R.scenario.replace('_',' ');
document.getElementById('desc').textContent = `Lead flies out at ${R.V} m/s dropping ${R.relays} relays every ${R.D/1000} km; telemetry home every ${R.T_MSG} s; ${R.tx_dbm} dBm, noise ${R.noise_dbm} dBm.`;
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const sl = document.getElementById('slider'); sl.max = R.messages.length - 1;
const maxX = Math.max(...R.messages.flatMap(m => m.positions.map(p => p[0]))) * 1.05 + 1;
function draw(i) {
  const m = R.messages[i], dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  const W = cv.clientWidth, H = cv.clientHeight, X = x => 30 + (W - 60) * x / maxX, Y = H * 0.55;
  ctx.clearRect(0,0,W,H); ctx.strokeStyle = css('--line'); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(30, Y); ctx.lineTo(W - 30, Y); ctx.stroke();
  ctx.fillStyle = css('--muted'); ctx.font = '11px system-ui';
  for (let k = 0; k <= maxX / 1000; k += 5) { ctx.fillText(k + ' km', X(k * 1000) - 10, Y + 34); }
  // links that decode (>= 9 dB per hop: the measured >=99% burst point)
  m.positions.forEach((p, a) => m.positions.forEach((q, b) => {
    if (b <= a) return; const s = m.snr[a][b]; if (s === null || s < 9) return;
    ctx.strokeStyle = css('--line'); ctx.globalAlpha = Math.min(1, (s - 6) / 20); ctx.beginPath();
    ctx.moveTo(X(p[0]), Y); ctx.quadraticCurveTo((X(p[0]) + X(q[0])) / 2, Y - 18 - (b - a) * 10, X(q[0]), Y); ctx.stroke();
  })); ctx.globalAlpha = 1;
  // transmissions this message
  m.tx.forEach((t, k) => { const p = m.positions[t.sender]; ctx.strokeStyle = css('--tx'); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(X(p[0]), Y, 14 + 6 * k, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke(); });
  ctx.lineWidth = 1;
  m.positions.forEach((p, n) => { const role = R.nodes[n].role;
    ctx.fillStyle = role === 'base' ? css('--base') : role === 'lead' ? css('--tx') : css('--relay');
    ctx.beginPath(); ctx.arc(X(p[0]), Y, role === 'base' ? 8 : 6, 0, 7); ctx.fill();
    ctx.fillStyle = css('--fg'); ctx.fillText(role, X(p[0]) - 12, Y + 20); });
  const got = '0' in m.delivered;
  ctx.fillStyle = got ? css('--ok') : css('--bad'); ctx.font = '13px system-ui';
  ctx.fillText(got ? `base received (slot ${m.delivered['0']})` : 'base: not received', 32, 20);
  document.getElementById('clock').textContent = `t = ${m.t} s · lead at ${(m.positions[m.positions.length-1][0]/1000).toFixed(1)} km`;
}
const n = R.messages.length, okN = R.messages.filter(m => '0' in m.delivered).length;
const lastOk = R.messages.filter(m => '0' in m.delivered).map(m => m.positions[m.positions.length-1][0]).reduce((a,b)=>Math.max(a,b),0);
document.getElementById('stats').innerHTML = [
  [`${okN}/${n}`, 'messages delivered to base'], [`${(lastOk/1000).toFixed(1)} km`, 'farthest delivery'],
  [`${R.messages.reduce((a,m)=>a+m.tx.length,0)}`, 'transmissions in total'], [`${R.relays}`, 'relays dropped']
].map(([v,l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join('');
document.getElementById('rows').innerHTML = R.messages.map(m => {
  const lead = m.positions.length - 1, got = '0' in m.delivered;
  return `<tr><td>${m.t}</td><td>${(m.positions[lead][0]/1000).toFixed(1)}</td><td>${m.snr[lead][0]} dB</td><td>${m.tx.map(t=>t.slot+':'+R.nodes[t.sender].role).join(' ')}</td><td class="${got?'ok':'bad'}">${got?'✓':'—'}</td></tr>`; }).join('');
sl.oninput = () => draw(+sl.value); let timer = null;
document.getElementById('play').onclick = e => { if (timer) { clearInterval(timer); timer = null; e.target.textContent = 'Play'; return; }
  e.target.textContent = 'Pause'; timer = setInterval(() => { sl.value = (+sl.value + 1) % n; draw(+sl.value); }, 700); };
window.onresize = () => draw(+sl.value); draw(0);
</script></body></html>"""

if __name__ == "__main__":
    src = Path(sys.argv[1])
    rec = json.loads(src.read_text())
    out = HERE / (src.stem + ".html")
    out.write_text(TEMPLATE.replace("__DATA__", json.dumps(rec)), encoding="utf-8")
    print("wrote", out)
