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
def rpc_call(url, method, params, retries=3, timeout=30):
    """Thread-safe JSON-RPC call. Returns result or None on failure."""
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
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f'  ⚠ {method} failed after {retries} attempts: {e}')
                return None

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

def balance_of(token, wallet, url):
    """ERC-20 balanceOf(wallet) → float."""
    padded = wallet.replace('0x', '').lower().zfill(64)
    data   = '0x70a08231' + padded
    r = rpc_call(url, 'eth_call', [{'to': token, 'data': data}, 'latest'])
    return from_wei(r) if r else 0.0

def total_assets(vault, url):
    """Call totalAssets() on an ERC-4626 vault → float."""
    r = rpc_call(url, 'eth_call', [{'to': vault, 'data': '0x01e1d114'}, 'latest'])
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
