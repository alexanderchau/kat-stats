"""
KAT Farmer — balance fetching and vKAT lock enumeration.
All functions receive config values as parameters (no globals).
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import rpc


def get_fresh_balances(addresses, dump_raw, extra_addrs, rpc_url, kat_tokens,
                       avkat_addr, kat_pool_addrs, stake_dests, bridge_dests,
                       distributor_addr):
    """Fetch KAT and avKAT balances for all addresses + dump destinations.

    Returns: (addr_balances, dest_balances, dest_types)
      addr_balances: {addr: {kat, avkat}}
      dest_balances: {dest: total_kat}
      dest_types:    {dest: 'Wallet'|'LP Pool'|'Staking'|'Bridge'|...}
    """
    all_dest_tokens = list(kat_tokens) + [avkat_addr]
    all_dests = set()
    for r in dump_raw.values():
        all_dests.update(r['dests'].keys())

    extra = set(extra_addrs or []) - set(addresses)
    all_fetch = list(addresses) + list(extra)

    total_tasks = len(all_fetch) + len(all_dests)
    print(f'  Balances: {len(all_fetch):,} addresses + {len(all_dests):,} destinations = {total_tasks:,} tasks…')

    addr_balances = {}
    dest_balances = {}
    done_count    = 0
    lock          = threading.Lock()

    def fetch_addr(addr):
        kat   = sum(rpc.balance_of(t, addr, rpc_url) for t in kat_tokens)
        avkat = rpc.balance_of(avkat_addr, addr, rpc_url)
        return addr, kat, avkat

    def fetch_dest(dest):
        total = sum(rpc.balance_of(t, dest, rpc_url) for t in all_dest_tokens)
        return dest, total

    with ThreadPoolExecutor(max_workers=16) as ex:
        addr_futs = {ex.submit(fetch_addr, a): a for a in all_fetch}
        dest_futs = {ex.submit(fetch_dest, d): d for d in all_dests}
        all_futs  = list(addr_futs.keys()) + list(dest_futs.keys())

        for fut in as_completed(all_futs):
            with lock:
                done_count += 1
                n = done_count
            if n % 100 == 0 or n == total_tasks:
                print(f'    {n}/{total_tasks} balance fetches done…', end='\r')

    print()
    for fut, addr in addr_futs.items():
        try:
            _, kat, avkat = fut.result()
        except Exception:
            kat, avkat = 0.0, 0.0
        addr_balances[addr] = {'kat': kat, 'avkat': avkat}

    for fut, dest in dest_futs.items():
        try:
            _, total = fut.result()
        except Exception:
            total = 0.0
        dest_balances[dest] = total

    # Classify each destination address
    print(f'  Classifying {len(all_dests):,} destination addresses…')
    dest_types    = {}
    unknown_dests = []
    for dest in all_dests:
        if dest in kat_pool_addrs:
            dest_types[dest] = 'LP Pool'
        elif dest in stake_dests:
            dest_types[dest] = 'Staking'
        elif dest in bridge_dests:
            dest_types[dest] = 'Bridge'
        elif dest == distributor_addr:
            dest_types[dest] = 'Merkl Distributor'
        else:
            unknown_dests.append(dest)

    contract_results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(rpc.is_contract, d, rpc_url): d for d in unknown_dests}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                contract_results[d] = fut.result()
            except Exception:
                contract_results[d] = False

    for dest in unknown_dests:
        if contract_results.get(dest, False):
            dest_types[dest] = 'Contract'
        else:
            dest_types[dest] = 'Wallet'

    contracts = sum(1 for v in dest_types.values() if v != 'Wallet')
    print(f'  Classified: {contracts} contracts, {len(dest_types) - contracts} wallets')
    return addr_balances, dest_balances, dest_types


def get_eth_balances(bridged_addrs, eth_rpc, kat_eth_addr):
    """Fetch KAT balance on ETH mainnet for addresses that bridged tokens.

    Returns: {addr: float} — ETH-side KAT balance for each address.
    """
    if not bridged_addrs:
        return {}
    print(f'  ETH balances: fetching {len(bridged_addrs):,} bridged addresses…')
    eth_balances = {}
    done_count = 0
    lock = threading.Lock()

    def fetch(addr):
        return addr, rpc.balance_of(kat_eth_addr, addr, eth_rpc)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fetch, a): a for a in bridged_addrs}
        for fut in as_completed(futs):
            with lock:
                done_count += 1
                n = done_count
            if n % 50 == 0 or n == len(bridged_addrs):
                print(f'    {n}/{len(bridged_addrs)} ETH balance fetches done…', end='\r')
            try:
                addr, bal = fut.result()
                eth_balances[addr] = bal
            except Exception:
                eth_balances[futs[fut]] = 0.0
    print()
    total = sum(eth_balances.values())
    print(f'  ETH balances: {rpc.fmtM(total)} KAT held on ETH across {len(bridged_addrs)} addresses')
    return eth_balances


def enumerate_vkat_locks(rpc_url, lock_nft, voting_escrow, kat_decimals):
    """Enumerate all vKAT Lock NFTs → {owner: {amount, endTime}}."""
    r = rpc.rpc_call(rpc_url, 'eth_call', [{'to': lock_nft, 'data': '0x18160ddd'}, 'latest'])
    total = int(r, 16) if r else 0
    if total == 0:
        return {}
    print(f'  vKAT: enumerating {total:,} Lock NFTs…')

    def get_token_id(idx):
        data = '0x4f6ccce7' + hex(idx)[2:].zfill(64)
        r = rpc.rpc_call(rpc_url, 'eth_call', [{'to': lock_nft, 'data': data}, 'latest'])
        return int(r, 16) if r else None

    with ThreadPoolExecutor(max_workers=16) as ex:
        token_ids = list(ex.map(get_token_id, range(total)))
    token_ids = [t for t in token_ids if t is not None]

    def get_lock_info(token_id):
        tid_hex  = hex(token_id)[2:].zfill(64)
        owner_r  = rpc.rpc_call(rpc_url, 'eth_call',
                                [{'to': lock_nft, 'data': '0x6352211e' + tid_hex}, 'latest'])
        owner = None
        if rpc.validate_hex(owner_r):
            owner = '0x' + owner_r[26:].lower()
        lock_r   = rpc.rpc_call(rpc_url, 'eth_call',
                                [{'to': voting_escrow, 'data': '0xb45a3c0e' + tid_hex}, 'latest'])
        amount, end_time = 0.0, 0
        if lock_r and len(lock_r) >= 130:
            amount   = int(lock_r[2:66], 16) / (10 ** kat_decimals)
            end_time = int(lock_r[66:130], 16)
        return owner, amount, end_time

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(get_lock_info, token_ids))

    owner_locks = {}
    for owner, amount, end_time in results:
        if owner and amount > 0:
            if owner not in owner_locks:
                owner_locks[owner] = {'amount': 0.0, 'endTime': 0}
            owner_locks[owner]['amount']  += amount
            owner_locks[owner]['endTime']  = max(owner_locks[owner]['endTime'], end_time)

    total_locked = sum(v['amount'] for v in owner_locks.values())
    print(f'  vKAT: {len(owner_locks):,} owners, {rpc.fmtM(total_locked)} KAT locked')
    return owner_locks
