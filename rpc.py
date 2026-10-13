"""
KAT Farmer — thread-safe RPC client + chain helpers.
All functions take `url` as explicit parameter — no globals.
No imports from config.py (rpc.py is config-agnostic).
"""
import json, time, threading
import urllib.request, urllib.error

# ── Thread-safe request ID ─────────────────────────────────────────────────
_seq_lock = threading.Lock()
_seq      = 0

def _next_id():
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq

# ── Core RPC call ──────────────────────────────────────────────────────────
def _is_rate_limit(e):
    msg = str(e)
    return '429' in msg or '-32017' in msg or 'Rate Limit' in msg or 'rate limit' in msg


def rpc_call(url, method, params, retries=3, timeout=30):
    """Thread-safe JSON-RPC call. Returns result or None on failure.

    None always means "could not read", never a value. Callers that turn a read
    into a number must keep the two apart (see balance_of(strict=True)).
    """
    for attempt in range(retries):
        try:
            body = json.dumps({
                'jsonrpc': '2.0', 'id': _next_id(),
                'method': method, 'params': params,
            }).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if 'error' in data:
                    raise ValueError(f"RPC error: {data['error']}")
                return data.get('result')
        except Exception as e:
            if attempt < retries - 1:
                # The public Katana RPC allows ~7 calls/s; a 429 needs real backoff.
                time.sleep(min(2 ** attempt, 30) if _is_rate_limit(e) else 0.5 * (attempt + 1))
            else:
                print(f'  ⚠ {method} failed after {retries} attempts: {e}')
                return None

# ── Multicall3 ─────────────────────────────────────────────────────────────
# Same address on Katana and ETH mainnet. One eth_call carries a few hundred
# reads, so a full holder enumeration costs ~100 RPC calls instead of ~15,000,
# which is what kept tripping the public RPC's rate limit.
MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11'


def _word(n):
    return hex(n)[2:].zfill(64)


def encode_aggregate3(calls):
    """ABI-encode aggregate3((address,bool,bytes)[]) with allowFailure=true."""
    tuples = []
    for target, data in calls:
        raw = data[2:] if data.startswith('0x') else data
        nbytes = len(raw) // 2
        padded = raw + '0' * ((-len(raw)) % 64)
        tuples.append(target.lower()[2:].zfill(64) + _word(1) + _word(0x60) + _word(nbytes) + padded)
    offsets, pos = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(_word(pos))
        pos += len(t) // 2
    return '0x82ad56cb' + _word(0x20) + _word(len(calls)) + ''.join(offsets) + ''.join(tuples)


def decode_aggregate3(result):
    """Decode aggregate3's (bool success, bytes returnData)[] → [(ok, '0x…')]."""
    b = bytes.fromhex(result[2:])
    word = lambda off: int.from_bytes(b[off:off + 32], 'big')
    arr = word(0)
    n = word(arr)
    out = []
    for i in range(n):
        t = arr + 32 + word(arr + 32 + 32 * i)
        ok = word(t) == 1
        d = t + word(t + 32)
        ln = word(d)
        out.append((ok, '0x' + b[d + 32:d + 32 + ln].hex()))
    return out


def multicall(url, calls, chunk=250, attempts=8, block='latest'):
    """Run [(target, calldata)] through Multicall3.

    Returns one entry per call:
      (True, returndata)  the call succeeded
      (False, returndata) the call reverted on-chain (a real answer)
      None                the RPC never answered: unresolved, NOT zero
    Chunks that fail are retried with exponential backoff before giving up.
    """
    out = [None] * len(calls)
    pending = list(range(0, len(calls), chunk))
    for attempt in range(attempts):
        failed = []
        for start in pending:
            part = calls[start:start + chunk]
            r = rpc_call(url, 'eth_call', [{'to': MULTICALL3, 'data': encode_aggregate3(part)}, block])
            try:
                decoded = decode_aggregate3(r) if r and len(r) > 2 else None
            except Exception:
                decoded = None
            if decoded is None or len(decoded) != len(part):
                failed.append(start)
                continue
            out[start:start + len(part)] = decoded
        pending = failed
        if not pending:
            break
        time.sleep(min(2 ** attempt, 30))
    if pending:
        missing = sum(len(calls[s:s + chunk]) for s in pending)
        print(f'  ⚠ multicall: {missing:,} of {len(calls):,} reads unresolved after {attempts} attempts')
    return out


def decode_uint(res, decimals=None):
    """Turn a multicall entry into an int (or float if decimals given).

    None stays None (unresolved). A revert or empty return reads as 0, because
    the chain answered: e.g. balanceOf on an address with no code.
    """
    if res is None:
        return None
    ok, data = res
    val = int(data[2:66], 16) if ok and len(data) >= 66 else 0
    return val / (10 ** decimals) if decimals is not None else val

# ── Chain helpers ──────────────────────────────────────────────────────────
def get_block_number(url):
    """Return current block number for the given chain."""
    r = rpc_call(url, 'eth_blockNumber', [])
    return int(r, 16) if r else 0

def eth_get_logs(url, params):
    """Call eth_getLogs; return list (empty on failure)."""
    r = rpc_call(url, 'eth_getLogs', [params])
    return r if isinstance(r, list) else []

def eth_get_receipt(tx_hash, url):
    """Fetch transaction receipt. Used for swap detection in scan_transfers."""
    return rpc_call(url, 'eth_getTransactionReceipt', [tx_hash])

# ── Token / contract helpers ───────────────────────────────────────────────
def from_wei(hex_val, decimals=18):
    """Convert hex wei string to float. Returns 0.0 on any error."""
    if not hex_val or hex_val in ('0x', '0x0'):
        return 0.0
    try:
        return int(hex_val, 16) / (10 ** decimals)
    except Exception:
        return 0.0

def balance_of_call(token, wallet):
    """(target, calldata) for ERC-20 balanceOf(wallet), for multicall()."""
    return token, '0x70a08231' + wallet.replace('0x', '').lower().zfill(64)

def balance_of(token, wallet, url, strict=False):
    """ERC-20 balanceOf(wallet) → float. With strict=True a failed read is None."""
    target, data = balance_of_call(token, wallet)
    r = rpc_call(url, 'eth_call', [{'to': target, 'data': data}, 'latest'])
    if r is None and strict:
        return None
    return from_wei(r) if r else 0.0

def total_assets(vault, url, strict=False):
    """Call totalAssets() on an ERC-4626 vault → float. strict: None on failure."""
    r = rpc_call(url, 'eth_call', [{'to': vault, 'data': '0x01e1d114'}, 'latest'])
    if r is None and strict:
        return None
    return from_wei(r) if r else 0.0

def is_contract(addr, url):
    """Return True if address has deployed code (contract vs EOA)."""
    r = rpc_call(url, 'eth_getCode', [addr, 'latest'])
    return bool(r and len(r) > 2 and r != '0x')

def validate_hex(val, expected_len=66):
    """Return True if val is a hex string of at least expected_len characters."""
    return bool(val and isinstance(val, str) and len(val) >= expected_len)

# ── Display helpers ────────────────────────────────────────────────────────
def fmtM(n):
    """Format large numbers with B/M/K suffixes (2dp for M/B, 1dp for K)."""
    if n >= 1e9: return f'{n/1e9:.2f}B'
    if n >= 1e6: return f'{n/1e6:.2f}M'
    if n >= 1e3: return f'{n/1e3:.1f}K'
    return f'{n:.0f}'
