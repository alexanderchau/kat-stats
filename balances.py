"""
KAT Farmer — balance fetching and vKAT lock enumeration.
All functions receive config values as parameters (no globals).
"""
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
      unresolved:    set of addresses whose balance reads never succeeded
    """
    all_dest_tokens = list(kat_tokens) + [avkat_addr]
    all_dests = set()
    for r in dump_raw.values():
        all_dests.update(r['dests'].keys())

    extra = set(extra_addrs or []) - set(addresses)
    all_fetch = list(addresses) + list(extra)

    all_dests = list(all_dests)
    print(f'  Balances: {len(all_fetch):,} addresses + {len(all_dests):,} destinations (multicall)…')

    kat_tokens = list(kat_tokens)
    addr_tokens = kat_tokens + [avkat_addr]
    calls = [rpc.balance_of_call(t, a) for a in all_fetch for t in addr_tokens]
    calls += [rpc.balance_of_call(t, d) for d in all_dests for t in all_dest_tokens]
    res = rpc.multicall(rpc_url, calls)
    vals = [rpc.decode_uint(r, 18) for r in res]

    # A failed read leaves the address in `unresolved`, never at 0. Its
    # balances fall back to 0 for display only; the holder-count gate in
    # indexer.py refuses to publish counts while anything is unresolved.
    addr_balances = {}
    dest_balances = {}
    unresolved    = set()
    i = 0
    for addr in all_fetch:
        row = vals[i:i + len(addr_tokens)]
        i += len(addr_tokens)
        if None in row:
            unresolved.add(addr)
        row = [v or 0.0 for v in row]
        addr_balances[addr] = {'kat': sum(row[:-1]), 'avkat': row[-1]}
    for dest in all_dests:
        row = vals[i:i + len(all_dest_tokens)]
        i += len(all_dest_tokens)
        if None in row:
            unresolved.add(dest)
        dest_balances[dest] = sum(v or 0.0 for v in row)
    if unresolved:
        print(f'  ⚠ Balances: {len(unresolved):,} addresses unresolved (RPC failures)')

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
    return addr_balances, dest_balances, dest_types, unresolved


def get_eth_balances(bridged_addrs, eth_rpc, kat_eth_addr):
    """Fetch KAT balance on ETH mainnet for addresses that bridged tokens.

    Returns: {addr: float} — ETH-side KAT balance for each address that read OK.
    """
    if not bridged_addrs:
        return {}
    print(f'  ETH balances: fetching {len(bridged_addrs):,} bridged addresses…')
    bridged_addrs = list(bridged_addrs)
    res = rpc.multicall(eth_rpc, [rpc.balance_of_call(kat_eth_addr, a) for a in bridged_addrs])
    # Unresolved addresses are left out (builders read them as unknown → 0)
    # rather than recorded as a confirmed zero.
    eth_balances = {a: v for a, v in zip(bridged_addrs, (rpc.decode_uint(r, 18) for r in res))
                    if v is not None}
    total = sum(eth_balances.values())
    print(f'  ETH balances: {rpc.fmtM(total)} KAT held on ETH across {len(bridged_addrs)} addresses')
    return eth_balances


def enumerate_vkat_locks(rpc_url, lock_nft, voting_escrow, kat_decimals):
    """Enumerate all vKAT Lock NFTs.

    Returns (owner_locks, unresolved): owner_locks is {owner: {amount, endTime}},
    unresolved the number of NFTs whose reads never succeeded. Callers must not
    publish holder counts while unresolved > 0 (see integrity.py).
    """
    r = rpc.rpc_call(rpc_url, 'eth_call', [{'to': lock_nft, 'data': '0x18160ddd'}, 'latest'])
    if r is None:
        print('  ⚠ vKAT: could not read Lock NFT totalSupply')
        return {}, 1
    total = int(r, 16) if len(r) > 2 else 0
    if total == 0:
        return {}, 0
    print(f'  vKAT: enumerating {total:,} Lock NFTs (multicall)…')

    pad = rpc._word
    id_res = rpc.multicall(rpc_url, [(lock_nft, '0x4f6ccce7' + pad(i)) for i in range(total)])
    unresolved = sum(1 for x in id_res if x is None)
    # A revert means the index fell off the end (a lock was burned mid-scan).
    token_ids = [rpc.decode_uint(x) for x in id_res if x is not None and x[0]]

    calls = []
    for tid in token_ids:
        calls.append((lock_nft, '0x6352211e' + pad(tid)))        # ownerOf
        calls.append((voting_escrow, '0xb45a3c0e' + pad(tid)))   # locked
    res = rpc.multicall(rpc_url, calls)

    owner_locks = {}
    for k in range(len(token_ids)):
        owner_r, lock_r = res[2 * k], res[2 * k + 1]
        if owner_r is None or lock_r is None:
            unresolved += 1
            continue
        if not owner_r[0] or not lock_r[0]:
            continue   # burned between enumeration and read
        owner = '0x' + owner_r[1][26:66].lower()
        data = lock_r[1]
        if len(data) < 130:
            continue
        amount   = int(data[2:66], 16) / (10 ** kat_decimals)
        end_time = int(data[66:130], 16)
        if amount > 0:
            o = owner_locks.setdefault(owner, {'amount': 0.0, 'endTime': 0})
            o['amount'] += amount
            o['endTime'] = max(o['endTime'], end_time)

    total_locked = sum(v['amount'] for v in owner_locks.values())
    msg = f'  vKAT: {len(owner_locks):,} owners, {rpc.fmtM(total_locked)} KAT locked'
    if unresolved:
        msg += f' — ⚠ {unresolved:,} of {total:,} locks UNRESOLVED'
    print(msg)
    return owner_locks, unresolved


def enumerate_exit_queue(rpc_url, lock_nft, voting_escrow, kat_decimals):
    """Sum the KAT sitting in the vKAT exit queue.

    beginWithdrawal() transfers the Lock NFT to the voting escrow itself and
    files a ticket with the escrow's ExitQueue. The KAT stays in the escrow
    until withdraw() burns the NFT, so every NFT the escrow owns is a queued
    exit. Ticket words: [holder, queuedAt, feeType, cooldown, ...].

    Returns ({amount, positions, holders, matured, maturedAmount}, rows), where
    `matured` = tickets past their cooldown (exit fee fully decayed) that
    haven't been withdrawn yet, and rows is one entry per holder:
    {address, amount, locks, maturedAmount, nextExit, lastExit} (unix seconds
    when the earliest / latest of their tickets clears cooldown), sorted by
    amount. (None, []) if the RPC reads fail.
    """
    import time
    pad = lambda v: hex(v)[2:].zfill(64)
    ve_word = voting_escrow.lower()[2:].zfill(64)

    queue_r = rpc.rpc_call(rpc_url, 'eth_call', [{'to': voting_escrow, 'data': '0xe10d29ee'}, 'latest'])
    count_r = rpc.rpc_call(rpc_url, 'eth_call', [{'to': lock_nft, 'data': '0x70a08231' + ve_word}, 'latest'])
    if not rpc.validate_hex(queue_r) or not count_r:
        print('  ⚠ Exit queue: could not read queue address / NFT count')
        return None, []
    exit_queue = '0x' + queue_r[26:]
    count = int(count_r, 16)

    def get_token_id(idx):
        r = rpc.rpc_call(rpc_url, 'eth_call',
                         [{'to': lock_nft, 'data': '0x2f745c59' + ve_word + pad(idx)}, 'latest'])
        return int(r, 16) if r else None

    def get_ticket(token_id):
        lock_r = rpc.rpc_call(rpc_url, 'eth_call',
                              [{'to': voting_escrow, 'data': '0xb45a3c0e' + pad(token_id)}, 'latest'])
        tick_r = rpc.rpc_call(rpc_url, 'eth_call',
                              [{'to': exit_queue, 'data': '0xddf0b009' + pad(token_id)}, 'latest'])
        if not lock_r or len(lock_r) < 66 or not tick_r or len(tick_r) < 2 + 64 * 4:
            return None
        amount    = int(lock_r[2:66], 16) / (10 ** kat_decimals)
        words     = [tick_r[2 + 64 * i: 2 + 64 * (i + 1)] for i in range(4)]
        holder    = '0x' + words[0][24:]
        queued_at = int(words[1], 16)
        cooldown  = int(words[3], 16)
        return amount, holder, queued_at + cooldown

    def fetch_all(fn, items):
        # The public RPC rate-limits bursts; retry stragglers serially.
        with ThreadPoolExecutor(max_workers=8) as ex:
            out = list(ex.map(fn, items))
        for attempt in range(8):
            missing = [i for i, v in enumerate(out) if v is None]
            if not missing:
                break
            time.sleep(min(2 ** attempt, 30))   # back off; the box sees long 429 storms
            for i in missing:
                out[i] = fn(items[i])
        return out

    token_ids = fetch_all(get_token_id, list(range(count)))
    if None in token_ids:
        print(f'  ⚠ Exit queue: {token_ids.count(None)} of {count} token ids unreadable')
        return None, []
    tickets = fetch_all(get_ticket, token_ids)
    if None in tickets:
        print(f'  ⚠ Exit queue: {tickets.count(None)} of {count} positions unreadable')
        return None, []

    now = int(time.time())
    matured = [t for t in tickets if t[2] <= now]
    out = {
        'amount':        round(sum(t[0] for t in tickets), 6),
        'positions':     count,
        'holders':       len({t[1] for t in tickets}),
        'matured':       len(matured),
        'maturedAmount': round(sum(t[0] for t in matured), 6),
    }
    by_holder = {}
    for amount, holder, exit_at in tickets:
        r = by_holder.setdefault(holder, {'address': holder, 'amount': 0.0, 'locks': 0,
                                          'maturedAmount': 0.0, 'nextExit': exit_at, 'lastExit': exit_at})
        r['amount'] += amount
        r['locks']  += 1
        if exit_at <= now:
            r['maturedAmount'] += amount
        r['nextExit'] = min(r['nextExit'], exit_at)
        r['lastExit'] = max(r['lastExit'], exit_at)
    rows = sorted(by_holder.values(), key=lambda r: r['amount'], reverse=True)
    for r in rows:
        r['amount'] = round(r['amount'], 6)
        r['maturedAmount'] = round(r['maturedAmount'], 6)

    print(f'  Exit queue: {rpc.fmtM(out["amount"])} KAT in {count:,} positions '
          f'({out["holders"]:,} holders), {out["matured"]:,} past cooldown '
          f'({rpc.fmtM(out["maturedAmount"])} KAT)')
    return out, rows
