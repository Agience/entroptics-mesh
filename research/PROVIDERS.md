# Writing providers — the entroptics-mesh plug-point SDK

*For integrators who will supply their own crypto, agility and interference handling. The
interfaces are in `src/entroptics_mesh/providers.py`.*

The entroptics-mesh core ships **interfaces and null providers only**. Everything that encrypts, hops,
or detects and avoids interference is **yours** to implement and plug in. Two working providers live outside
the core in `entroptics_mesh.ext`, which the core never imports: `ext.aead.AeadCrypto` (sealing) and
`ext.interference.EnergyInterference` (interference detection); select them explicitly or use them as templates. That split is deliberate: the core
offers only the sockets, a networking SDK, and you design what goes in them. Under the null
providers the mesh runs end to end as an **open broadcast network** on the public zero-state
aperture — so you can build and test everything else before you write a single provider.

## The one rule that governs every provider

**The mesh is one-way (reply-free).** A provider must never need anything back from another node to
function — no ack, no handshake, no quality report. A listen-only peer that can only receive must
still work. Anything a provider shares with its peers (keys, schedules) must be derivable
out-of-band (from a shared key + time + place), never negotiated over the link. If you find yourself
wanting a reply, redesign — it will break listen-only peers and it violates the core contract.

Two more invariants the core enforces, so your provider must respect them:
- **`K` is always `L`** (every mode is lit). `TxPlan` forces `K=None→L` and refuses `K≠L`. A spare
  mode is a noise-only direction a key-free observer reads the width from, so there are none.
- **Family / alphabet / code rate are not signalled.** The receiver reads both families and lets the
  frame CRC choose (`link.read_frame`). If your agility provider varies `family`/`M`/`code_rate`, the
  receiver still *discovers* them — unless you also give it a coordination prior (see the last
  section); either way it must be able to fall back to discovery.

## The three plug points

All live in `entroptics_mesh.providers`. Each is a `typing.Protocol` with a `name: str` and a null
implementation you can copy from.

### 1. `CryptoProvider` — seals payloads and supplies the PHY key
```python
class CryptoProvider(Protocol):
    name: str
    def seal(self, plaintext: bytes, *, context: bytes) -> bytes: ...
    def open(self, sealed: bytes, *, context: bytes) -> bytes | None: ...   # None = not for me / bad auth
    def waveform_key(self, context: bytes): ...                              # the aperture seed, or None
```
- `seal`/`open`: authenticated encryption. `context` (e.g. the frame header) is **bound but left
  in the clear** — authenticate it, don't encrypt it.
- `waveform_key(context)`: returns the seed the **physical-layer aperture** is drawn from (or `None`
  for the public zero state). This is what makes the emission keyed/noise-like.
- **Key freshness is yours to manage.** Under one key every burst repeats the same keyed aperture,
  reference row, per-row mode rotations and per-row power sequence — none gives the content away, but
  a key held a long time lets an observer collect many bursts and compare like with like. How often
  the key rotates, and **how a receiver comes to hold the next key, is your design** (the core only
  ever asks for the key in force). Because the mesh is reply-free, the next key must be derivable by
  the receiver without asking — e.g. a key schedule keyed to a coarse epoch clock.

**Working provider: `entroptics_mesh.ext.aead.AeadCrypto(key, waveform_key=None, cipher=...)`**
(install the `crypto` extra, `pip install "entroptics-mesh[crypto]"`). ChaCha20-Poly1305 (or
AES-256-GCM) under a 32-byte pre-shared mesh key; `context` is the associated data; the sealed form
is `sender id (8) || counter (4) || ciphertext || tag (16)`, the first 12 bytes being the nonce, so
every seal has a fresh nonce; `open` returns `None` on a wrong key, any tampering, or a replay (a
per-sender sliding window of 1024 counters, consulted only after authentication). The PHY key is a
separate argument, so every relay can share the aperture while only the end points hold the
sealing key: a relay forwards the sealed bytes without opening them (`tests/test_aead_crypto.py`
runs a three-node chain whose relay holds another key, or none). Limits: keys are pre-shared (no
key exchange); one instance per node (an instance holds one replay window); the window is in
memory, so a restarted receiver accepts counters it saw before the restart. Set the tunnel's
`seal_overhead` to `AeadCrypto.OVERHEAD` (28) when a tunnel is hosted.

*Called at:* `node.py` — `self.key = crypto.waveform_key(b"phy")` (PHY seed at construction),
`crypto.seal(payload, context=…)` on send, `crypto.open(payload, context=…)` on receive, and
`announce.seal(crypto, …)` for the optional "I'm here" beacon.

#### Pseudonymous addressing — the metadata-privacy hook (optional, null-default)
```python
    def address(self, identity: bytes, *, context: bytes = b"") -> bytes: ...   # id -> on-wire token
    def resolve(self, token: bytes, *, context: bytes = b"") -> bytes | None: ...# token -> id, for a holder
```
Content-blind relaying (`seal`/`open` with a key distinct from `waveform_key`) keeps *what* is said
confidential from a relay; it does **not** protect *who talks to whom* — the header's origin and destination are still
in the clear. `address`/`resolve` are the plug point for concealing them. They are the **addressing
analogue of the keyed transmit schedule**: `address` returns a keyed-pseudorandom token of
`(shared secret, a coarse epoch in context, identity)` — unlinkable across messages and to the true
identity for anyone without the secret, resolvable by a holder.

- **Null default is byte-identical.** `NullCrypto.address` returns the identity unchanged and
  `resolve` returns the token unchanged, so a core under the null provider addresses on raw ids
  exactly as before. The toy `KeyedPseudonym` in `tests/test_provider_contract.py` shows the keyed
  shape (tokenize + self-check + holder-resolve + epoch rotation).
- **Wiring (the deployer's deliberate step, not yet called by the core).** A sender maps the header
  **origin and destination through `address`** just before the header is sealed and sent, using the
  same `context` (coarse epoch) the destination reconstructs. The seal binds the **tokenized** header,
  so `open` is handed the as-received (tokenized) header and is unaffected. Routing then runs **on
  tokens**: a relay de-duplicates and forwards on the token it reads and never learns the identity;
  a destination recognizes a frame as its own by `token == address(self_id, context)` (no inverse
  needed — `resolve` is for diagnostics/attribution by a holder).
- **Two constraints the provider must honour.** (1) A token must stay **fixed over a message's life**
  in the mesh — its bursts share a `msg_id` that dedup/for-me key on — so the epoch is coarser than
  the longest cross-mesh delivery. (2) Because routing, the neighbour table and the keyed schedule all
  key on *whatever identity the header carries*, rotating tokens trades a relay's **acquire-once
  tracking** of a neighbour for the unlinkability. That is a real anonymity-vs-efficiency choice; the
  core leaves it to the provider rather than forcing it. Cover traffic (decoy bursts that a blind classifier cannot
  separate from payload bursts) is the complementary metadata-privacy layer for timing and volume (traffic-analysis resistance) — it lives on
  the agility provider as `cover_traffic` (see §2, "Optional — cover traffic").

*Called at:* nowhere in the core yet — the interface + null default are in place so a provider can be
written and the core wired at the header boundary above without an API change.

### 2. `AgilityProvider` — chooses how each transmission goes out
```python
class AgilityProvider(Protocol):
    name: str
    def plan(self, intent: Intent, default: TxPlan, table, ctx: PlanContext) -> TxPlan: ...
```
- `default` is the core's **audience-fitted** plan (it already fits what the node has heard).
- `table` is the node's `NeighborTable` — **clues, never constraints**. Hearing a neighbour go narrow
  does not oblige you to; never let the policy lock the node into its most limited heard mode.
- `ctx` is a `PlanContext(me, velocity, t)` — the **sender's own** position, velocity and time, so a
  schedule can seed on geometry and time without reaching into the node. Ignore it if you don't need it.

**Seeds available to a schedule** (so "a keyed schedule is supported" is a fact, not a hope — a
schedule/hop table in the agility provider is as legitimate as an out-of-band key in the crypto
provider; the core supplies the raw material, you supply the algorithm):
| seed | where it comes from |
|---|---|
| **expected receiver's last-known GPS / velocity / band** | `table[intent.to].announced.position / .velocity / .band` |
| **our measured link to that receiver (scale, carrier, level, SNR)** | `table[intent.to].measured` (`snr_db`, `level_db`, `scale`, `carrier`, `L/K/M`) |
| **who we've heard and at what SNR** | `table[id].announced.heard` (node id → SNR dB), and `table` iteration |
| **our own GPS / velocity / time** | `ctx.me`, `ctx.velocity`, `ctx.t` |
| **recipient / size / urgency** | `intent.to`, `intent.n_bytes`, `intent.priority` |
| **pseudorandom** | your provider's own keyed PRNG (you hold the key; the core never sees it) |

All of it is a *clue*: nothing obliges a particular plan, and the schedule must still need **nothing
back** from a peer (derive shared state from key + these seeds, never negotiate it on the link).
- Return a `TxPlan` (below). Vary `rate` (width), `center`, `power_db`, `L`, `M`, `family`,
  `code_rate` as your policy wants — e.g. a **keyed pseudo-random hop** over width/centre so an
  authorized peer predicts it and an eavesdropper cannot (generalized parameter hopping = your no-added-structure /
  interference-avoidance mechanism). **Don't mirror the band you just heard** — a reused band is a repeated
  pattern.

*Called at:* `node.py` — `chosen = providers.agility.plan(intent, plan, self.table, ctx)` then handed
to the interference provider.

**Optional — keyed start schedule (the coordination prior).** Implement these two methods and a
coordinated receiver can *predict* a sender's burst start and skip almost all of the blind scan
(measured ~1.8–2.8× faster per burst on the default mesh — the fast scan fires on every post-first
burst instead of none):
```python
    def transmit_offset(self, ctx: PlanContext, *, window_s: float) -> float | None: ...  # sender's own keyed start in [0, window_s)
    def predict_offset(self, who: bytes, t: float, *, window_s: float) -> float | None: ...# the SAME value a receiver reconstructs for sender `who` at time t
```
Both are a **pure function of (key, sender id `ctx.node_id`, coarse time)** — nothing comes back from
a peer. The sender uses `transmit_offset` to time its burst; the receiver uses `predict_offset`
(anchored to that sender's last lock, so the unknown propagation/clock constant cancels) to feed
the tracked scan (`phy.receive_known(tracked=...)`). Return `None` → the core's random-access draw / lock-cache (the null default,
byte-identical). Keep the sequence a **keyed PRNG** (uniform, no repeating period) so it stays
noise-like to a receiver without the key. `NullAgility` returns `None`; the shipped `schedules.KeyedSchedule`
is a minimal blake2b example — write your own. *A missing or wrong offset only costs a re-scan (CRC
fallback), never a wrong lock. Whether a real clock holds the predicted start within the scan window
between bursts is hardware-dependent.*

**Optional — cover traffic (the scheduling side of metadata privacy).** The crypto provider's
`address`/`resolve` protects *who* a message is between; cover traffic is the traffic-analysis-resistance
side: it keeps *when* and *how much* real traffic is sent from being read off the air. Implement one method:
```python
    def cover_traffic(self, ctx: PlanContext, *, sending: bool) -> list[TxPlan]: ...  # decoy bursts to emit now, [] for none
```
Called once per transmit opportunity. `sending` is `True` when the node is also sending a real burst
this slot — so a policy can pad an **idle** slot so busy and idle periods read alike, add bursts to even out **volume**, or
shape inter-burst **timing**. It is cheap here because a burst already reads as transmitted Gaussian
noise to the blind structure detectors (the keyed per-row amplitude law), so a decoy and a payload burst
are not separable by structure (both are visible as energy); draw each decoy's width/centre/power from the **same distribution as real
traffic** so they stay inseparable. A decoy carries no deliverable message — the node fills it with
keyed-random bytes, a receiver's **CRC fails and the frame is dropped** (a no-op to the mesh). Seed the
decision on `ctx` (the node's own id/time/position) and your key; it needs nothing back from a peer.
Return `[]` (and `NullAgility` does) → no cover, byte-identical to today. *A provider wanting a cheap
authenticated drop instead of a CRC failure at an authorized receiver seals a reserved cover marker
through its crypto provider — that cross-provider design is yours, not the core's.*

*Called at:* nowhere in the core yet — like `address`/`resolve`, the interface + null default are in
place so a provider can be written; the node wires it at a transmit opportunity (emit each returned
plan as a burst of random bytes).

### 3. `InterferenceProvider` — vetoes or reshapes a plan from what it observes
```python
class InterferenceProvider(Protocol):
    name: str
    def observe(self, obs: BandObservation) -> None: ...     # every band measurement
    def review(self, plan: TxPlan) -> TxPlan | None: ...      # a plan to send instead, or None to hold
```
- `observe(BandObservation(t, occupancy, center, interference, rate, cell, iq))`: **raw measurements
  only** (occupied share, its centre, the interference read `waveform.interference`, and the capture
  itself with its sample rate and the narrowest width the node plans a burst at); the interpretation
  is yours.
- `interference_bands()` (optional): the bands this node's receiver measures interference on, as
  `neighbors.InterferenceBand` (offset + width relative to the node's band centre). The node puts them in its
  announcement; a peer sending to it drops those sub-bands from its plan (`adapt.default_plan`).
- `review(plan)`: return a replacement plan (e.g. shift `center` off an interferer, drop `power_db`,
  widen) or `None` to **hold** the transmission.

*Called at:* `node.py` — `interference.observe(BandObservation(...))` on every capture; `interference.review(chosen)`
is the last step before transmit.

**Working provider: `entroptics_mesh.ext.interference.EnergyInterference()`** (detection; the avoid half is
the core planner's). It cuts the band into cells of the radio's minimum rate on the planner's own
grid of sub-band centres, reads each cell's noise floor the way the node reads its band floor (a low
quantile of 64-sample block powers, the least over the last `Node.FLOOR_CAPTURES` captures), and
reports a cell whose floor stands above the quietest cell's by more than noise alone reads between
that many cell floors (the bound is computed from Gamma(64, 1/64) block powers at the capture's block
count; nothing is tuned). `level_db` is the cell's floor over the quietest cell's. Limits: an interferer
covering every cell is not reported; a flat receiver response across the capture is assumed; an
interferer that switches on under a held quiet floor is reported once the quiet captures leave the
history. `tests/test_interference_detect.py` runs it end to end on the emulated air with a tone interferer.

## The data types you work with
```python
@dataclass(frozen=True)
class TxPlan:
    rate: float            # sample rate (occupied width), Hz
    center: float = 0.0    # offset from the radio's tuned freq, Hz
    power_db: float = -20.0 # TX gain below the radio max (<= 0)
    K: int | None = None   # forced to L; K != L is refused
    L: int = 64            # row width / modes
    M: int = 4             # alphabet size
    family: str = "differential"  # or "coherent" (square M-QAM)
    code_rate: str = "1/2"        # "1/2" | "2/3" | "3/4"
# re-widen with replace(plan, L=…): it carries K=None so K tracks the new L.

@dataclass(frozen=True)
class Intent:              # what providers see — never the payload
    to: bytes | None       # None = broadcast
    n_bytes: int
    priority: int = 0       # higher = more urgent (command, FPV)

@dataclass(frozen=True)
class BandObservation:
    t: float; occupancy: float; center: float; interference: float
    rate: float | None = None; cell: float | None = None; iq = None   # the capture, when handed over

@dataclass(frozen=True)
class PlanContext:          # the sender's own state, for schedule-seeding agility providers
    me: "Position | None" = None               # our GPS
    velocity: tuple[float, float, float] | None = None
    t: float = 0.0                              # timestamp
    node_id: bytes = b""                        # the sender's own id (so a schedule can key on identity)
```
Providers see `Intent` (to / size / priority), **never the payload**. Only the crypto provider
touches payload bytes.

## Attaching them to a node
```python
from entroptics_mesh.providers import Providers
from entroptics_mesh.node import Node

node = Node(..., providers=Providers(
    crypto=MyAesCrypto(key_schedule=…),
    agility=MyKeyedHopper(key=…),
    interference=MyBandPolicy(),
))
```
Any you omit default to the null provider (`Providers()` is all-null). Build and test the rest of
your system under nulls first — the mesh is fully functional as an open broadcast network, so your
providers are a *layer on a working mesh*, not a prerequisite for one.

## Worked sketches (illustrative — not production crypto)
```python
class MyAesCrypto:                                   # CryptoProvider
    name = "aesgcm-epoch"
    def __init__(self, master, epoch_s=3600): self.master, self.epoch_s = master, epoch_s
    def seal(self, pt, *, context):  return aesgcm(self._k("msg")).encrypt(pt, aad=context)
    def open(self, ct, *, context):
        try: return aesgcm(self._k("msg")).decrypt(ct, aad=context)
        except Exception: return None               # None = not for me / bad auth
    def waveform_key(self, context):                 # PHY seed, rotated per epoch (RX derives the same)
        return hkdf(self.master, salt=self._epoch().to_bytes(8,"big") + context, length=32)
    def _epoch(self): return int(time.time() // self.epoch_s)   # coarse shared clock, no reply needed

class MyKeyedHopper:                                 # AgilityProvider
    name = "keyed-hop"
    def __init__(self, key): self.key = key
    def plan(self, intent, default, table, ctx):
        # keyed pseudo-random width/centre per coarse time slot: authorized RX predicts, others can't
        r = prf(self.key, slot_of(ctx.t))                      # seed on ctx.t (+ ctx.me / receiver GPS if wanted)
        width = pick(default.rate, [1e6, 2e6, 4e6], r)          # don't collapse to a heard-narrow floor
        return replace(default, rate=width, center=centre_from(r))   # avoid mirroring the heard band

class MyBandPolicy:                                  # InterferenceProvider
    name = "shift-or-hold"
    def __init__(self): self.worst = 0.0
    def observe(self, obs): self.worst = max(self.worst, obs.interference)
    def review(self, plan):
        if self.worst > THRESH: return replace(plan, center=plan.center + STEP)  # step off the interferer
        return plan                                                               # (return None to hold)
```

## Forward note — the coordination prior (roadmap, not yet wired)
Because the receiver runs the **same** agility provider, it can *predict* a sender's `TxPlan` instead
of discovering it blind. Feeding that prediction as the receiver's candidate set collapses
first-contact acquisition (seconds → one verify) and the per-burst scan to a local refine — dropping
the receive-side compute/energy toward an incumbent fixed-demod cost (see the energy model,
`research/power/energy_model.md`). The guarantee to preserve when this lands: the prior must **accelerate, never be
required** — the receiver still *verifies* within the narrow window, so a stale/spoofed/absent hint
degrades to blind and never breaks decode. Today the plumbing exists on the sender side (`agility.plan`
emits the `TxPlan`) and the receiver has a width-given fast path and a per-sender scale cache
(`neighbors.known_scale`); wiring the predicted plan into the receiver's candidate set is the build.

## Checklist for a provider you ship
- [ ] Needs **no reply** from any peer to function (listen-only peers still work).
- [ ] Any shared secret/schedule is **derivable out-of-band** (key + coarse time/place), never negotiated on the link.
- [ ] Keeps `K == L`; if it varies family/M/code_rate, the receiver can still discover (or you supply the prior).
- [ ] Keyed emission stays **noise-like** (don't introduce a periodic/repeated structure; don't mirror the heard band).
- [ ] `open()` returns `None` (not an exception) for messages not sealed for this node.
- [ ] Tested against the null baseline: the mesh delivered end-to-end before your provider, and still does with it.
