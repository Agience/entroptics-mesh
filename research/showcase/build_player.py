"""Build a self-contained HTML player from a fleet showcase recording.

    python research/showcase/build_player.py [research/showcase/fleet_showcase.json]

writes research/showcase/fleet_showcase.html next to it: the recording (cut to what the player
draws) inlined, no network, opens from disk.  Light and dark follow the viewer's system setting.

What it shows, slot by slot (the same canvas approach as research/viewer/build_viewer.py):
  * map: every node at its TRUE position (gateway square, relay triangle, drone circle), its
    ANNOUNCED position as a hollow ring joined to it when they differ, and a red cross (with how
    many nodes) on a node whose position claim some node distrusts at that moment;
  * links: the SNR each receiver measured each transmitter at (its own read of the burst),
    coloured on one blue scale, fading with age;
  * transmissions: each burst of the slot flashes a ring on its sender in its sub-band's colour,
    with the sub-band and mode written beside it, and a band strip (the 2.4 MHz capture) shows
    who is on which part of the band;
  * delivery / latency panel and an event log (node loss, spoof flagged, relay plans, deliveries).

It also prints a summary of the run (deliveries, latency, distrust verdicts, measured-vs-true
SNR of every frame taken), which is what the README quotes.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def _mode(tx) -> str:
    if tx["family"] == "coherent":
        return f"{tx['M']}-QAM {tx['code']}"
    return {2: "DBPSK", 4: "DQPSK", 8: "D8PSK", 16: "D16PSK"}.get(tx["M"], f"D{tx['M']}PSK") + " " + tx["code"]


def _band(tx, rx_rate) -> str:
    if tx["rate"] >= rx_rate * 0.99:
        return "full"
    return "upper" if tx["center"] > 0 else "lower"


def compact(rec: dict) -> tuple[dict, dict]:
    """The player's data, and the run summary."""
    names = [n["name"] for n in rec["nodes"]]
    roles = [n["role"] for n in rec["nodes"]]
    idx = {n["id"]: i for i, n in enumerate(rec["nodes"])}
    name_of = {n["id"]: n["name"] for n in rec["nodes"]}
    name_of[rec.get("home", "")] = "HOME"
    rx_rate = float(rec["rx_rate"])
    spoof = rec["spoof"]["node"]
    frames, events, epochs = [], [], []
    errs = []                                      # (measured - true in-band) per frame taken
    distrust_now = {nm: set() for nm in names}
    honest_flags, spoof_flags = [], []
    first_delivery = {}
    for d in rec["deliveries"]:
        if "latency_ms" in d:
            first_delivery[(d["e"], d["s"], d["kind"], d["origin"], d["epoch"], d.get("k"))] = d
    for ep in rec["epochs"]:
        e = ep["e"]
        epochs.append(dict(e=e, t=ep["t"], truth=ep["truth"], claims=ep["claims"], alive=ep["alive"],
                           views={g: dict(cut=v["cut_off"], plan=v["plan"], distrusted=v["distrusted"])
                                  for g, v in ep.get("views", {}).items()}))
        if e == rec["spoof"]["epoch"]:
            events.append(dict(f=len(frames), kind="spoof",
                               text=f"epoch {e}: {spoof}'s GPS starts to drift {rec['spoof']['step_m'] / 1000:.0f} km "
                                    f"sideways per epoch (the air does not move)"))
        for sl in ep["slots"]:
            f = len(frames)
            tx = []
            rate_of = {}
            for t in sl["tx"]:
                rate_of[t["node"]] = t["rate"]
                tx.append([t["node"], _band(t, rx_rate), _mode(t), name_of.get(t["origin"], "?"), t["seq"],
                           bool(t["own"]), round(t["center"] / 1e6, 2), round(t["rate"] / 1e6, 2),
                           round(t.get("start", 0.0) * 1e6)])
            took = []
            for rname, r in sl["nodes"].items():
                ri = names.index(rname)
                for k in r.get("took", []):
                    ti = idx.get(k["tx"])
                    if ti is None or k.get("snr") is None:
                        continue
                    took.append([ri, ti, k["snr"]])
                    rate = rate_of.get(ti)
                    if rate is not None and ep["snr"][ti][ri] is not None:
                        errs.append(k["snr"] - (ep["snr"][ti][ri] - 10 * math.log10(rate / rx_rate)))
                if "distrusted" in r:
                    new = set(r["distrusted"])
                    new_n = {name_of.get(h, h) for h in new}
                    for who in sorted(new_n - distrust_now[rname]):
                        honest = who != spoof or e < rec["spoof"]["epoch"]
                        (honest_flags if honest else spoof_flags).append((rname, who, e, sl["s"]))
                        events.append(dict(f=f, kind="distrust" if honest else "flag",
                                           text=f"{rname} distrusts {who}'s position claim"
                                                + ("" if honest else " (the spoofed node)")))
                    for who in sorted(distrust_now[rname] - new_n):
                        events.append(dict(f=f, kind="trust", text=f"{rname} trusts {who}'s claim again"))
                    distrust_now[rname] = new_n
            if sl.get("event"):
                events.append(dict(f=f, kind="loss", text=f"epoch {e}: {sl['event']} (no notice; nothing told)"))
            for d in rec["deliveries"]:
                if d["e"] == e and d["s"] == sl["s"] and "latency_ms" in d:
                    what = (f"telemetry {d['origin']} (epoch {d['epoch']})" if d["kind"] == "tlm"
                            else f"UDP {d['origin']} #{d['k']} (epoch {d['epoch']})")
                    events.append(dict(f=f, kind="deliver", text=f"{what} home at {d['at']}, {d['latency_ms']:.0f} ms"))
            frames.append(dict(e=e, s=sl["s"], t=sl["t"], tx=tx, took=took,
                               dist={k: sorted(v) for k, v in distrust_now.items() if v},
                               defer=[names.index(n) for n in sl.get("deferred", [])]))
        for g, v in ep.get("views", {}).items():
            if roles[names.index(g)] != "gateway":
                continue            # a drone's view has no home of its own: everyone reads cut off
            if v["cut_off"] or v["plan"]:
                slots = sum(len(s) for s in v["plan"].values())
                events.append(dict(f=len(frames) - 1, kind="plan",
                                   text=f"end of epoch {e}: {g} sees {', '.join(v['cut_off']) or 'nobody'} cut off from home; "
                                        f"relay plan {slots} slot(s)"))
    sent = rec.get("sent", [])
    ok = [x for x in sent if x["delivered"]]
    lat = [x["latency_ms"] for x in ok]
    a = np.asarray(errs) if errs else np.zeros(1)
    summary = dict(
        delivered=f"{len(ok)}/{len(sent)}",
        tlm=f"{sum(x['delivered'] for x in sent if x['kind'] == 'tlm')}/{sum(x['kind'] == 'tlm' for x in sent)}",
        udp=f"{sum(x['delivered'] for x in sent if x['kind'] == 'udp')}/{sum(x['kind'] == 'udp' for x in sent)}",
        latency_median_ms=float(np.median(lat)) if lat else None,
        latency_p90_ms=float(np.percentile(lat, 90)) if lat else None,
        latency_max_ms=float(max(lat)) if lat else None,
        bursts=sum(len(fr["tx"]) for fr in frames), slots=len(frames), epochs=len(rec["epochs"]),
        deferred=sum(len(fr["defer"]) for fr in frames), listen=bool(rec.get("listen", False)),
        bursts_per_delivered=round(sum(len(fr["tx"]) for fr in frames) / max(1, len(ok)), 2),
        snr_frames=len(errs), snr_err_median=float(np.median(a)), snr_err_mean=float(np.mean(a)),
        snr_err_abs_p90=float(np.percentile(np.abs(a), 90)), snr_err_abs_max=float(np.max(np.abs(a))),
        snr_err_within_1db=float(np.mean(np.abs(a) <= 1.0)),
        honest_distrust=[list(x) for x in honest_flags], spoof_flagged=[list(x) for x in spoof_flags],
        wall_s=rec.get("wall_s"))
    by_epoch = []
    for ep in rec["epochs"]:
        s_ = [x for x in sent if x["epoch"] == ep["e"]]
        by_epoch.append([ep["e"], sum(x["delivered"] for x in s_), len(s_)])
    summary["by_epoch"] = by_epoch
    data = dict(names=names, roles=roles, slot_s=rec["slot_s"], epoch_s=rec["epoch_s"], V=rec["V"],
                tx_dbm=rec["tx_dbm"], noise_dbm=rec["noise_dbm"], rx_rate=rx_rate, spoof=rec["spoof"],
                loss=rec["loss"], epochs=epochs, frames=frames, events=events,
                sent=[dict(kind=x["kind"], origin=x["origin"], epoch=x["epoch"], k=x["k"], ok=x["delivered"],
                           ms=x["latency_ms"], at=x["at"]) for x in sent],
                summary=summary)
    return data, summary


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Showcase Replay</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--fg:#0b0b0b;--muted:#52514e;--faint:#8a8984;--line:#e4e3df;
--link-lo:#cde2fb;--link-hi:#104281;--b-lower:#eb6834;--b-full:#1baf7a;--b-upper:#e87ba4;--bad:#d42f2f;--ok:#008300;--claim:#6250d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#141413;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;
--faint:#7d7c75;--line:#33332f;--link-lo:#1c3a5e;--link-hi:#86b6ef;--b-lower:#d95926;--b-full:#199e70;--b-upper:#d55181;--bad:#e66767;--ok:#3ecf8e;--claim:#9085e9}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#141413;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--faint:#7d7c75;--line:#33332f;
--link-lo:#1c3a5e;--link-hi:#86b6ef;--b-lower:#d95926;--b-full:#199e70;--b-upper:#d55181;--bad:#e66767;--ok:#3ecf8e;--claim:#9085e9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:19px;margin:0 0 4px}h2{font-size:14px;margin:0 0 8px;color:var(--muted);font-weight:600}
p.sub{color:var(--muted);margin:0 0 12px;max-width:80ch}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;min-width:0}
.grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:12px}
@media (max-width:860px){.grid{grid-template-columns:minmax(0,1fr)}}
canvas{width:100%;display:block}
#map{height:300px}#zoom{height:300px}#band{height:100px}#lat{height:150px}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
input[type=range]{flex:1;min-width:140px}
button,select{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;padding:5px 10px;cursor:pointer;font:inherit}
button[aria-pressed=true]{background:var(--line)}
.clock{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}
.stat span{display:block}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:4px}
#log{max-height:300px;overflow:auto;font-size:13px;margin:0;padding:0;list-style:none}
#log li{padding:3px 0;border-bottom:1px solid var(--line);display:flex;gap:8px}
#log li.future{display:none}#log li .when{color:var(--faint);min-width:58px;font-variant-numeric:tabular-nums}
#log li.cur{font-weight:600}
.tag{font-size:11px;border-radius:4px;padding:0 5px;border:1px solid var(--line);color:var(--muted);white-space:nowrap;height:18px}
.tag.flag,.tag.loss,.tag.distrust{color:var(--bad);border-color:var(--bad)}.tag.deliver{color:var(--ok);border-color:var(--ok)}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:3px 5px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;display:none;z-index:5}
</style></head><body><main>
<h1>Fleet showcase replay</h1>
<p class="sub" id="desc"></p>
<div class="grid">
 <div>
  <div class="card"><h2>Map (true positions; hollow ring = announced position)</h2><canvas id="map"></canvas><h2 style="margin-top:10px">The drone formation, close up (labels: how many nodes distrust a claim)</h2><canvas id="zoom"></canvas>
   <div class="legend" id="legend"></div>
   <div class="controls"><button id="play" aria-pressed="false">Play</button><button id="prev" title="previous slot">&#9664;</button><button id="next" title="next slot">&#9654;</button>
    <input type="range" id="slider" min="0" value="0" aria-label="slot"><select id="speed" aria-label="speed"><option value="400">slow</option><option value="150" selected>normal</option><option value="50">fast</option></select>
    <select id="epoch" aria-label="jump to epoch"></select></div>
   <div class="clock" id="clock"></div></div>
  <div class="card"><h2>On the air this slot (2.4 MHz capture; each burst on its sub-band)</h2><canvas id="band"></canvas></div>
 </div>
 <div>
  <div class="card"><h2>Delivery and latency</h2><div class="stats" id="stats"></div><canvas id="lat"></canvas>
   <div class="legend"><span><i style="background:var(--ok)"></i>delivered (latency, ms)</span><span><i style="background:var(--bad)"></i>not delivered</span></div></div>
  <div class="card"><h2>Event log</h2><ul id="log"></ul></div>
 </div>
</div>
<details class="card"><summary><b>Every item sent</b> (table)</summary><table><thead><tr><th>epoch</th><th>item</th><th>from</th><th>home at</th><th class="num">latency (ms)</th></tr></thead><tbody id="items"></tbody></table></details>
<p class="sub">Every burst is real entroptics-rf IQ on an emulated free-space air (Friis at 915 MHz, every burst summed at each receiver with its noise); every node is the mesh's own runtime deciding from its own reads -- no acks, no schedule, nothing pre-shared but the waveform key. Mission time jumps between epochs; within one, slots are milliseconds and positions hold still.</p>
</main><div class="tip" id="tip"></div>
<script>
const D = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const $ = id => document.getElementById(id);
const F = D.frames, N = D.names.length, S = D.summary;
$('desc').textContent = `${D.names.filter((n,i)=>D.roles[i]==='drone').length} drones fly out at ${D.V} m/s with ${D.names.filter((n,i)=>D.roles[i]==='relay').length} relays trailing above and two gateways at the base (HOME anycast, shared backhaul); ${D.tx_dbm} dBm, noise ${D.noise_dbm} dBm. Telemetry and a tunnelled UDP flow go home every epoch (${D.epoch_s} s). ${D.spoof.node}'s GPS is spoofed from epoch ${D.spoof.epoch}; ${D.loss.node} is lost in epoch ${D.loss.epoch}.` + (S.listen ? ' Every node listens before it talks: it draws its own start at random in a short contention window, listens just before it, and defers to a later slot when it hears the channel taken (its own floor and gate; nothing agreed, nothing sent back).' : '');
// ── stats, items ──
const fmt = v => v === null || v === undefined ? '–' : Math.round(v);
const B = D.before;
$('stats').innerHTML = [[S.delivered,'items delivered home', B && B.delivered],[`${fmt(S.latency_median_ms)} ms`,'median latency', B && `${fmt(B.latency_median_ms)} ms`],
  [`${fmt(S.latency_p90_ms)} ms`,'p90 latency', B && `${fmt(B.latency_p90_ms)} ms`],[`${S.bursts_per_delivered}`,'bursts per item delivered', B && B.bursts_per_delivered],
  [`${S.bursts}`,`bursts in ${S.slots} slots`, B && `${B.bursts} in ${B.slots}`],[`${S.deferred}`,'starts deferred (heard busy)', B && '0']]
  .map(([v,l,b])=>`<div class="stat"><b>${v}</b><span>${l}${b ? ` · before: ${b}` : ''}</span></div>`).join('');
$('items').innerHTML = D.sent.map(x=>`<tr><td>${x.epoch}</td><td>${x.kind==='tlm'?'telemetry':'UDP #'+x.k}</td><td>${x.origin}</td><td class="${x.ok?'ok':'bad'}">${x.ok?x.at:'not delivered'}</td><td class="num">${x.ok?x.ms.toFixed(0):'–'}</td></tr>`).join('');
// ── event log ──
const tagName = {spoof:'spoof',flag:'flagged',distrust:'distrust',trust:'trust',loss:'node lost',plan:'relay plan',deliver:'delivered'};
$('log').innerHTML = D.events.map((ev,k)=>`<li data-f="${ev.f}" data-k="${k}"><span class="when">e${F[ev.f].e}·s${F[ev.f].s}</span><span class="tag ${ev.kind}">${tagName[ev.kind]}</span><span>${ev.text}</span></li>`).join('');
$('log').onclick = e => { const li = e.target.closest('li'); if (li) { stop(); show(+li.dataset.f); } };
// ── epoch selector ──
$('epoch').innerHTML = D.epochs.map(ep=>`<option value="${F.findIndex(f=>f.e===ep.e)}">epoch ${ep.e} (t=${ep.t} s)</option>`).join('');
$('epoch').onchange = e => { stop(); show(+e.target.value); };
// ── legend ──
const bands = [['lower','lower 1.2 MHz'],['full','full 2.4 MHz'],['upper','upper 1.2 MHz']];
$('legend').innerHTML = `<span>&#9632; gateway &nbsp; &#9650; relay &nbsp; &#9679; drone</span><span><i style="background:linear-gradient(90deg,var(--link-lo),var(--link-hi))"></i>link: measured SNR 0 → 30 dB</span>`
  + bands.map(([b,l])=>`<span><i style="background:var(--b-${b})"></i>${l}</span>`).join('') + `<span><i style="border:2px solid var(--claim)"></i>announced position</span><span style="color:var(--bad)">&#10005; position claim distrusted (by how many nodes; hover for whom)</span>`;
// ── colour scale ──
function hex(c){c=c.replace('#','');return [0,2,4].map(i=>parseInt(c.substr(i,2),16));}
function mix(a,b,t){const A=hex(a),B=hex(b);return `rgb(${A.map((v,i)=>Math.round(v+(B[i]-v)*t)).join(',')})`;}
const snrColor = s => mix(css('--link-lo'), css('--link-hi'), Math.max(0, Math.min(1, s / 30)));
// ── map geometry: the whole cast at its true positions (wide), and the drone formation (close-up);
// a claim outside the view is drawn at the view's edge with its offset written beside it ──
function boxOf(ep, who){const pts=ep.truth.filter((p,i)=>who==='all'||D.roles[i]==='drone'), xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
  const m = who==='all' ? 800 : 250;
  return {x0:Math.min(...xs)-m, x1:Math.max(...xs)+m*2, y0:Math.min(...ys)-m, y1:Math.max(...ys)+m};}
const hits = {map:[], zoom:[]};
function sizeCanvas(cv){const dpr=window.devicePixelRatio||1;cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;const c=cv.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);return c;}
function drawMap(fi, id, who){
  const cv=$(id), c=sizeCanvas(cv), W=cv.clientWidth, H=cv.clientHeight, fr=F[fi], hs=hits[id]=[];
  const ep = D.epochs.find(x=>x.e===fr.e), box = boxOf(ep, who);
  const sc = Math.min((W-40)/(box.x1-box.x0), (H-40)/(box.y1-box.y0));
  const X = x => 20 + (x-box.x0)*sc, Y = y => H - 20 - (y-box.y0)*sc;
  const inView = p => p[0]>=box.x0 && p[0]<=box.x1 && p[1]>=box.y0 && p[1]<=box.y1;
  const shown = i => who==='all' || D.roles[i]==='drone';
  c.clearRect(0,0,W,H);
  // km grid
  c.strokeStyle=css('--line'); c.fillStyle=css('--faint'); c.font='11px system-ui'; c.lineWidth=1;
  const span = box.x1-box.x0, step = span > 30000 ? 10000 : span > 12000 ? 2000 : span > 3000 ? 1000 : 200;
  for (let x=Math.ceil(box.x0/step)*step; x<=box.x1; x+=step){c.beginPath();c.moveTo(X(x),12);c.lineTo(X(x),H-20);c.stroke();c.fillText(`${(x/1000).toFixed(step<1000?1:0)} km`,X(x)+3,H-6);}
  // links: the last SNR each receiver measured each transmitter at, this epoch up to now, fading with age
  const last = new Map();
  for (let k=fi; k>=0 && F[k].e===fr.e; k--) for (const [ri,ti,s] of F[k].took){const key=ti+'>'+ri; if(!last.has(key)) last.set(key,[ri,ti,s,fi-k]);}
  for (const [ri,ti,s,age] of last.values()){
    if (!shown(ri) || !shown(ti)) continue;
    const a=ep.truth[ti], b=ep.truth[ri];
    c.globalAlpha = Math.max(0.25, 1 - age/40); c.strokeStyle=snrColor(s); c.lineWidth = age===0 ? 3 : 2;
    c.beginPath(); c.moveTo(X(a[0]),Y(a[1])); c.lineTo(X(b[0]),Y(b[1])); c.stroke();
    hs.push({x:(X(a[0])+X(b[0]))/2, y:(Y(a[1])+Y(b[1]))/2, text:`${D.names[ri]} heard ${D.names[ti]} at ${s.toFixed(1)} dB (measured)`});
  }
  c.globalAlpha=1;
  // transmissions this slot: a ring in the sub-band's colour, with the sub-band and mode
  fr.tx.forEach(([ni,band,mode,origin,seq,own],k)=>{ if (!shown(ni)) return; const p=ep.truth[ni];
    c.strokeStyle=css('--b-'+band); c.lineWidth=3; c.beginPath(); c.arc(X(p[0]),Y(p[1]),12+4*k,0,7); c.stroke();
    if (who==='all'){c.fillStyle=css('--fg'); c.font='11px system-ui'; c.fillText(`${D.names[ni]}: ${band} · ${mode}${own?'':' (relaying '+origin+')'}`, 8, 14+13*k);}});
  // listened before talking, heard the channel taken, deferred: a dashed ring
  fr.defer.forEach(ni=>{ if (!shown(ni)) return; const p=ep.truth[ni];
    c.strokeStyle=css('--faint'); c.lineWidth=1.5; c.setLineDash([3,3]); c.beginPath(); c.arc(X(p[0]),Y(p[1]),10,0,7); c.stroke(); c.setLineDash([]);
    hs.push({x:X(p[0]),y:Y(p[1])-10,text:`${D.names[ni]} listened before its start, heard the channel taken, and deferred`});});
  // nodes
  const by = {}; Object.entries(fr.dist).forEach(([g,v])=>v.forEach(w=>(by[w]=by[w]||[]).push(g)));  // who distrusts whom, now
  ep.truth.forEach((p,i)=>{
    if (!shown(i)) return;
    const q=ep.claims[i], alive=ep.alive[i] && !(D.loss.node===D.names[i] && lostBy(fi));
    const x=X(p[0]), y=Y(p[1]), off=Math.hypot(q[0]-p[0],q[1]-p[1]);
    if (off > 50){
      const qx=Math.min(W-12,Math.max(12,X(q[0]))), qy=Math.min(H-24,Math.max(12,Y(q[1]))), out=!inView(q);
      c.setLineDash([4,4]); c.strokeStyle=css('--claim'); c.lineWidth=1.5; c.beginPath(); c.moveTo(x,y); c.lineTo(qx,qy); c.stroke(); c.setLineDash([]);
      c.lineWidth=2; c.beginPath(); c.arc(qx,qy,7,0,7); c.stroke();
      const lab = `${D.names[i]} claims ${out?'(off view, ':'('}${(off/1000).toFixed(1)} km off)`, lw = c.measureText(lab).width;
      c.fillStyle=css('--claim'); c.fillText(lab, qx+10+lw > W ? qx-10-lw : qx+10, qy+4);
      hs.push({x:qx,y:qy,text:`${D.names[i]} announces a position ${(off/1000).toFixed(1)} km from its true one`});
    }
    c.globalAlpha = alive ? 1 : 0.3; c.fillStyle=css('--fg'); c.beginPath();
    const r=D.roles[i]; if (r==='gateway') c.rect(x-6,y-6,12,12); else if (r==='relay'){c.moveTo(x,y-8);c.lineTo(x+7,y+5);c.lineTo(x-7,y+5);c.closePath();} else c.arc(x,y,5,0,7);
    c.fill(); c.fillStyle=css('--muted'); c.fillText(D.names[i]+(alive?'':' (lost)'), x+8, y+14);
    c.globalAlpha=1;
    if (by[D.names[i]]){c.strokeStyle=css('--bad'); c.lineWidth=2.5; c.beginPath(); c.moveTo(x-8,y-8); c.lineTo(x+8,y+8); c.moveTo(x+8,y-8); c.lineTo(x-8,y+8); c.stroke();
      if (who!=='all'){c.fillStyle=css('--bad'); c.fillText(`distrusted by ${by[D.names[i]].length}`, x+8, y-10);}}
    hs.push({x,y,text:`${D.names[i]} (${r}) true position${by[D.names[i]]?' — its position claim distrusted by '+by[D.names[i]].join(', '):''}`});
  });
}
function lostBy(fi){const k=D.events.find(ev=>ev.kind==='loss'); return k && fi>=k.f;}
function drawBand(fi){
  const cv=$('band'), c=sizeCanvas(cv), W=cv.clientWidth, H=cv.clientHeight, fr=F[fi];
  const X = mhz => 10 + (W-20)*(mhz + D.rx_rate/2e6)/(D.rx_rate/1e6);
  c.clearRect(0,0,W,H); c.strokeStyle=css('--line'); c.fillStyle=css('--faint'); c.font='11px system-ui';
  c.beginPath(); c.moveTo(10,H-18); c.lineTo(W-10,H-18); c.stroke();
  [-1.2,-0.6,0,0.6,1.2].forEach(m=>{c.fillText(`${m>0?'+':''}${m} MHz`, Math.min(W-50,Math.max(10,X(m)-18)), H-4);});
  const lanes = {lower:[],full:[],upper:[]}; fr.tx.forEach(t=>lanes[t[1]].push(t));
  // sub-band bursts on the lower rows, full-width ones stacked above them (they overlap both halves)
  const sub = Math.max(lanes.lower.length, lanes.upper.length), rows = Math.max(1, sub + lanes.full.length), h = Math.min(22, (H-26)/rows);
  Object.entries(lanes).forEach(([lane,l])=>l.forEach((t,k)=>{const [ni,band,mode,origin,seq,own,cMHz,rMHz,us]=t;
    const x0=X(cMHz-rMHz/2)+1, x1=X(cMHz+rMHz/2)-1, y=H-20-((lane==='full'?sub:0)+k+1)*h;
    c.fillStyle=css('--b-'+band); c.beginPath(); c.roundRect ? c.roundRect(x0,y+1,x1-x0,h-2,4) : c.rect(x0,y+1,x1-x0,h-2); c.fill();
    c.fillStyle=css('--bg'); c.font='11px system-ui'; c.fillText(`${D.names[ni]} · ${mode}${S.listen ? ` · start +${us} µs` : ''}`, x0+6, y+h/2+4);}));
  if (!fr.tx.length){c.fillStyle=css('--faint'); c.fillText('nothing on the air this slot', 12, 16);}
}
function drawLat(fi){
  const cv=$('lat'), c=sizeCanvas(cv), W=cv.clientWidth, H=cv.clientHeight, cur=F[fi].e;
  c.clearRect(0,0,W,H);
  const items=D.sent, maxMs=Math.max(50,...items.filter(x=>x.ok).map(x=>x.ms))*1.1;
  const E=D.epochs.length, X=(e,j,n)=>30+(W-40)*(e+(j+0.5)/n)/E, Y=ms=>H-16-(H-26)*ms/maxMs;
  c.strokeStyle=css('--line'); c.fillStyle=css('--faint'); c.font='11px system-ui';
  [0,Math.round(maxMs/2/10)*10].forEach(m=>{c.beginPath();c.moveTo(30,Y(m));c.lineTo(W-10,Y(m));c.stroke();c.fillText(`${m}`,2,Y(m)+4);});
  D.epochs.forEach(ep=>{c.fillText(`e${ep.e}`, X(ep.e,0,1)-6, H-2);
    const its=items.filter(x=>x.epoch===ep.e);
    its.forEach((x,j)=>{const px=X(ep.e,j,its.length); c.globalAlpha = ep.e<=cur ? 1 : 0.25;
      if (x.ok){c.fillStyle=css('--ok'); c.beginPath(); c.arc(px,Y(x.ms),4,0,7); c.fill();}
      else {c.strokeStyle=css('--bad'); c.lineWidth=2; c.beginPath(); c.moveTo(px-4,H-20); c.lineTo(px+4,H-12); c.moveTo(px+4,H-20); c.lineTo(px-4,H-12); c.stroke();}
      c.globalAlpha=1;});});
}
const sl=$('slider'); sl.max=F.length-1; let fi=0, timer=null;
function show(i){fi=Math.max(0,Math.min(F.length-1,i)); sl.value=fi; const fr=F[fi];
  drawMap(fi,'map','all'); drawMap(fi,'zoom','fleet'); drawBand(fi); drawLat(fi);
  $('clock').textContent=`epoch ${fr.e} · slot ${fr.s} · mission t = ${fr.t.toFixed(3)} s · ${fr.tx.length} burst(s) · ${fr.took.length} frame(s) taken` + (fr.defer.length ? ` · deferred (heard busy): ${fr.defer.map(i=>D.names[i]).join(', ')}` : '');
  $('epoch').value = String(F.findIndex(f=>f.e===fr.e));
  let lastShown=null;
  for (const li of $('log').children){const f=+li.dataset.f; li.classList.toggle('future', f>fi); li.classList.toggle('cur', f===fi); if(f<=fi) lastShown=li;}
  if (lastShown && timer) lastShown.scrollIntoView({block:'nearest'});}
function stop(){if(timer){clearInterval(timer);timer=null;$('play').textContent='Play';$('play').setAttribute('aria-pressed','false');}}
$('play').onclick=()=>{if(timer){stop();return;} $('play').textContent='Pause'; $('play').setAttribute('aria-pressed','true');
  timer=setInterval(()=>{ if(fi>=F.length-1){stop();return;} show(fi+1); }, +$('speed').value);};
$('speed').onchange=()=>{if(timer){stop();$('play').click();}};
$('prev').onclick=()=>{stop();show(fi-1);}; $('next').onclick=()=>{stop();show(fi+1);};
sl.oninput=()=>{stop();show(+sl.value);};
const tip=$('tip');
for (const id of ['map','zoom']){ $(id).onmousemove=e=>{const r=e.target.getBoundingClientRect(), x=e.clientX-r.left, y=e.clientY-r.top;
  let best=null,bd=14; for(const h of hits[id]){const d=Math.hypot(h.x-x,h.y-y); if(d<bd){bd=d;best=h;}}
  if(best){tip.style.display='block'; tip.textContent=best.text; tip.style.left=(e.clientX+12)+'px'; tip.style.top=(e.clientY+12)+'px';} else tip.style.display='none';};
$(id).onmouseleave=()=>tip.style.display='none'; }
window.onresize=()=>show(fi);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>show(fi));
const h = /f=(\d+)/.exec(location.hash); show(h ? +h[1] : 0);
</script></body></html>"""


def main(src: Path, before: Path | None = None):
    """``before``: a recording of the same scenario to show beside this one (the access layer
    before listen-before-talk: ``fleet_showcase_before.json``)."""
    rec = json.loads(src.read_text())
    data, summary = compact(rec)
    if before is not None and before.exists():
        b = compact(json.loads(before.read_text()))[1]
        data["before"] = {k: b[k] for k in ("delivered", "latency_median_ms", "latency_p90_ms", "bursts",
                                            "slots", "bursts_per_delivered")}
    out = src.with_suffix(".html")
    out.write_text(TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":"))), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("honest_distrust", "spoof_flagged")}, indent=1))
    print("honest nodes distrusted (by, who, epoch, slot):", summary["honest_distrust"])
    print("spoof flagged (by, who, epoch, slot):", summary["spoof_flagged"])
    print("wrote", out, f"({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "fleet_showcase.json",
         Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "fleet_showcase_before.json")
