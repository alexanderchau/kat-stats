#!/usr/bin/env python3
"""Generate holder_activity.json for the "Holders vs Users" tab.

KAT holder  = beneficial EOA with KAT exposure in ANY form: liquid KAT + locked vKAT
              + avKAT (held directly OR unwrapped from Morpho collateral / Uni-Sushi LP / Spectra PT).
Katana user = address that has done >= USER_TX_MIN transactions on Katana (nonce >= USER_TX_MIN).

Usage:  python3 holder_activity.py [--refresh]
  --refresh re-pulls holder lists from Blockscout + replays avKAT + rescans Morpho.
  Without it, cached pulls in .holder_cache/ are reused (only nonces are refreshed).
"""
import json, os, sys, time, re, urllib.request, urllib.parse
from datetime import datetime, timezone

RPC      = "https://rpc.katana.network"
BS       = "https://explorer.katanarpc.com/api/v2"
KAT      = "0x7f1f4b4b29f5058fa32cc7a97141b8d7e5abdc2d"   # liquid KAT (circulating)
VKAT     = "0x106f7d67ea25cb9eff5064cf604ebf6259ff296d"   # vote-escrow NFT (locked KAT)
AVKAT    = "0x7231dbacdfc968e07656d12389ab20de82fbfceb"   # autocompounding vKAT
MORPHO   = "0xd50f2dfffd62f94ee4aed9ca05c61d0753268abc"
AVKAT_DEPLOY  = 23368834
MORPHO_DEPLOY = 23368834   # avKAT markets can't predate avKAT
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_CREATE   = "0xac4b2400f169220b0c0afdde7a0b32e775ba727ea1cb30b35f935cdaab8683ac"
T_SUPCOL   = "0xa3b9472a1399e17e123f3c2e6586c23e504184d504de59cdaa2b375e880c6184"
ZERO = "0x0000000000000000000000000000000000000000"
USER_TX_MIN = 2

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".holder_cache")
HDRS = {"Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"}
REFRESH = "--refresh" in sys.argv

def _post(payload, timeout=180, tries=6):
    body = json.dumps(payload).encode(); last = None
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(RPC, data=body, headers=HDRS), timeout=timeout).read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 413: raise
            time.sleep(0.6*(k+1))
        except Exception as e:
            last = e; time.sleep(0.6*(k+1))
    raise last
def rpc(m, p): return _post({"jsonrpc":"2.0","id":1,"method":m,"params":p})
def topic_addr(t): return "0x" + t[-40:].lower()

def _get(url, tries=6):
    last = None
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HDRS), timeout=60).read())
        except Exception as e:
            last = e; time.sleep(0.5*(k+1))
    raise last

def batch(method, args, label=""):
    out = [None]*len(args); B = 200; i = 0
    while i < len(args):
        chunk = args[i:i+B]
        pl = [{"jsonrpc":"2.0","id":j,"method":method,"params":p} for j,p in enumerate(chunk)]
        try:
            resp = _post(pl)
        except urllib.error.HTTPError as e:
            if e.code == 413 and B > 1: B = max(1, B//2); continue
            raise
        for r in resp: out[i + r["id"]] = r.get("result") if "error" not in r else None
        i += len(chunk)
        if label and (i // B) % 20 == 0: print(f"  {label} {i}/{len(args)}", file=sys.stderr)
    return out

# ── cached/fetched holder lists ──────────────────────────────────────────────
def fetch_holders(token):
    out, npp, page = {}, None, 0
    while True:
        url = f"{BS}/tokens/{token}/holders" + ("?" + urllib.parse.urlencode(npp) if npp else "")
        d = _get(url); items = d.get("items", [])
        if not items: break
        for it in items:
            a = it.get("address", {})
            h = ((a.get("hash") if isinstance(a, dict) else a) or "").lower()
            if h: out[h] = {"is_contract": a.get("is_contract") if isinstance(a, dict) else None}
        page += 1; print(f"  {token[:8]} page {page} ({len(out)})", file=sys.stderr)
        npp = d.get("next_page_params")
        if not npp: break
    return out

def cached(name, builder):
    path = os.path.join(CACHE, name)
    if not REFRESH and os.path.exists(path):
        return json.load(open(path))
    os.makedirs(CACHE, exist_ok=True)
    val = builder()
    json.dump(val, open(path, "w"))
    return val

def get_logs(address, topics, start):
    logs = []; latest = int(rpc("eth_blockNumber", [])["result"], 16); lo = start; span = latest-start+1
    while lo <= latest:
        hi = min(latest, lo+span-1); ok = False; batch_ = None
        params = [{"fromBlock":hex(lo),"toBlock":hex(hi),"address":address,"topics":topics}]
        try:
            r = rpc("eth_getLogs", params)
            if "error" in r:
                msg = r["error"].get("data","") or r["error"].get("message","")
                m = re.search(r"0x[0-9a-fA-F]+\]", msg)
                if m:
                    sug = int(m.group(0)[:-1],16); span = max(1, min(span//2, sug-lo+1)) if sug<=lo else (sug-lo+1)
                else: span = max(1, span//2)
            else: ok, batch_ = True, r["result"]
        except urllib.error.HTTPError as e:
            if e.code == 413: span = max(1, span//2)
            else: raise
        if ok:
            logs.extend(batch_); lo = hi+1
            if len(batch_) < 20000: span = min(latest-lo+1, span*2) if span < (latest-lo+1) else span
    return logs

WRAPPERS = {  # avKAT held on behalf of users → unwrap to funders (verified EOA only)
    "0x8640e1867bd563b2ab865160e77cb7b875243b13": "UniV3Pool",
    "0xbda5995c8ffeb294f8f681253acee2ab87a0435e": "Spectra-PT",
    "0xbd91b400194ae150cc3c375e977dfd25901ad80c": "Spectra-PT/IBT",
}
def build_avkat_unwrap():
    print("replaying avKAT transfers…", file=sys.stderr)
    logs = get_logs(AVKAT, [T_TRANSFER], AVKAT_DEPLOY)
    funders = {w: set() for w in WRAPPERS}
    for lg in logs:
        frm = topic_addr(lg["topics"][1]); to = topic_addr(lg["topics"][2])
        if to in WRAPPERS and frm != ZERO and frm not in WRAPPERS:
            funders[to].add(frm)
    return {"wrapper_funders": {WRAPPERS[w]: sorted(s) for w, s in funders.items()}}

def build_morpho():
    print("scanning Morpho avKAT-collateral suppliers…", file=sys.stderr)
    cm = get_logs(MORPHO, [T_CREATE], MORPHO_DEPLOY)
    avkat_mkts = []
    for lg in cm:
        words = [lg["data"][2:][i:i+64] for i in range(0, len(lg["data"][2:]), 64)]
        if len(words) >= 2 and "0x"+words[1][-40:].lower() == AVKAT:
            avkat_mkts.append(lg["topics"][1])
    suppliers = set()
    if avkat_mkts:
        for lg in get_logs(MORPHO, [T_SUPCOL, avkat_mkts], MORPHO_DEPLOY):
            suppliers.add(topic_addr(lg["topics"][3]))
    return {"avkat_markets": avkat_mkts, "avkat_collateral_suppliers": sorted(suppliers)}

# ── build universe ───────────────────────────────────────────────────────────
liquid = cached("kat_liquid_holders.json", lambda: fetch_holders(KAT))
vkat   = cached("vkat_holders.json",       lambda: fetch_holders(VKAT))
avkat  = cached("avkat_holders.json",      lambda: fetch_holders(AVKAT))
unwrap = cached("avkat_unwrap.json",       build_avkat_unwrap)
morpho = cached("morpho_suppliers.json",   build_morpho)

def eoas(d):  return {h for h, v in d.items() if not v.get("is_contract")}
def conts(d): return {h for h, v in d.items() if v.get("is_contract")}
known_contracts = conts(liquid) | conts(vkat) | conts(avkat)

liquid_eoa, vkat_eoa, avkat_eoa = eoas(liquid), eoas(vkat), eoas(avkat)
core = liquid_eoa | vkat_eoa | avkat_eoa

morpho_sup = {a.lower() for a in morpho.get("avkat_collateral_suppliers", [])}
lp      = {a.lower() for a in unwrap["wrapper_funders"].get("UniV3Pool", [])}
spectra = {a.lower() for a in unwrap["wrapper_funders"].get("Spectra-PT", [])} | \
          {a.lower() for a in unwrap["wrapper_funders"].get("Spectra-PT/IBT", [])}
unwrapped = (morpho_sup | lp | spectra) - known_contracts

# verify the not-in-core unwrapped beneficiaries are actually EOAs (drop routers)
cand = sorted(unwrapped - core)
codes = batch("eth_getCode", [[a, "latest"] for a in cand], "code") if cand else []
new_eoa = {a for a, c in zip(cand, codes) if c in (None, "0x", "0x0", "")}

universe = sorted(core | new_eoa)
print(f"universe = {len(universe)} beneficial EOA holders", file=sys.stderr)

# ── classify by tx count (nonce) ─────────────────────────────────────────────
latest_block = int(rpc("eth_blockNumber", [])["result"], 16)
nonces = batch("eth_getTransactionCount", [[a, "latest"] for a in universe], "nonce")
counts = [int(x, 16) if x else 0 for x in nonces]

def bucket(n):
    if n == 0:  return "0"
    if n == 1:  return "1"
    if n <= 4:  return "2-4"
    if n <= 9:  return "5-9"
    if n <= 49: return "10-49"
    return "50+"
order = ["0", "1", "2-4", "5-9", "10-49", "50+"]
b = {k: 0 for k in order}
for n in counts: b[bucket(n)] += 1

users    = sum(1 for n in counts if n >= USER_TX_MIN)
nonusers = len(counts) - users
total    = len(counts)

out = {
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "katanaBlock": latest_block,
    "userTxMin": USER_TX_MIN,
    "totalHolders": total,
    "users": users,
    "nonUsers": nonusers,
    "usersPct": round(100*users/total, 1),
    "nonUsersPct": round(100*nonusers/total, 1),
    "buckets": [{"label": k, "count": b[k]} for k in order],
    "components": [
        {"label": "Liquid KAT",              "count": len(liquid_eoa)},
        {"label": "Staked · vKAT (locked)",  "count": len(vkat_eoa)},
        {"label": "avKAT",                   "count": len(avkat_eoa)},
        {"label": "Morpho avKAT collateral", "count": len(morpho_sup - core)},
        {"label": "Sushi/Uni LP + Spectra",  "count": len((lp | spectra) - core - known_contracts)},
    ],
    "chainAddresses": None,  # filled below if reachable
}
# chain-wide address count for scale
try:
    stats = _get(f"{BS}/stats")
    out["chainAddresses"] = int(stats.get("total_addresses")) if stats.get("total_addresses") else None
except Exception:
    pass

json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "holder_activity.json"), "w"), indent=2)
print(json.dumps({k: v for k, v in out.items() if k not in ("buckets", "components")}, indent=2))
print("buckets:", out["buckets"])
print("components:", out["components"])
