"""
KAT Farmer — central config loader.
Loads config.json once at import time; exports all constants as module-level names.
Import via: from config import KATANA_RPC, KAT_ADDR, ...
"""
import json
from pathlib import Path

_cfg = json.loads(Path(__file__).parent.joinpath('config.json').read_text())

# ── RPC endpoints ─────────────────────────────────────────────────────────────
KATANA_RPC = _cfg['katanaRpc']       # str
ETH_RPC    = _cfg['ethRpc']          # str

# ── Log chunking ──────────────────────────────────────────────────────────────
LOG_CHUNK     = 29_999   # Katana max getLogs range (30k fails)
ETH_LOG_CHUNK = 50_000   # ETH mainnet (~7 days per chunk)

# ── Contracts ─────────────────────────────────────────────────────────────────
DISTRIBUTOR_ADDR = _cfg['distributorAddr']   # str
CLAIMED_TOPIC    = _cfg['claimedTopic']      # str
TRANSFER_TOPIC   = _cfg['transferTopic']     # str
SWAP_V3_TOPIC    = _cfg['swapV3Topic']       # str
SWAP_V2_TOPIC    = _cfg['swapV2Topic']       # str

DEPLOY_BLOCK  = _cfg['deployBlock']   # int
KAT_ADDR      = _cfg['katAddr']       # str
AVKAT_ADDR    = _cfg['avkatAddr']     # str
KAT_DECIMALS  = _cfg['katDecimals']  # int

KAT_TOKENS     = set(_cfg['katTokens'])       # set[str] — all 3 KAT token variants
KAT_POOL_ADDRS = set(_cfg['poolAddresses'])   # set[str]

BRIDGES      = _cfg['bridges']           # list[str]
BRIDGE_DESTS = set(BRIDGES)              # set[str] — derived

ZERO_ADDR = _cfg['zeroAddress']   # str

VOTING_ESCROW = _cfg['votingEscrow']   # str
LOCK_NFT      = _cfg['lockNft']        # str
STAKE_DESTS   = {VOTING_ESCROW, AVKAT_ADDR}   # set[str] — derived

KAT_ETH_ADDR  = _cfg['katEthAddr']    # str — KAT token on ETH mainnet
ETH_KAT_START = _cfg['ethKatStart']   # int — block where ETH KAT scanning begins

# ── New fields (added in this refactor) ──────────────────────────────────────
CHAIN_ID      = _cfg['chainId']        # int — 747474
KAT_BASE_ADDR = _cfg['katBaseAddr']    # str — 10B base token (not distributed to users)

# Supply scanning: 2-token set (excludes KAT_BASE_ADDR which is the underlying
# token wrapping into the above — not distributed to end users directly)
SUPPLY_KAT_TOKENS = set(_cfg['supplyKatTokens'])   # set[str]

BUYER_MIN_KAT  = _cfg['buyerMinKat']   # int — 1000
STAKER_MIN_KAT = _cfg['stakerMinKat']  # int — 100
