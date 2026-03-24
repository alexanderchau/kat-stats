"""
KAT Farmer — on-chain event scanners.
All functions receive config values as parameters (no globals).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import rpc


def scan_claimed(state, addr_set, rpc_url, distributor_addr, claimed_topic,
                 kat_tokens, log_chunk):
    """Scan Claimed events from the Merkl distributor."""
    scan_from = state['katanaBlock'] + 1
    latest    = rpc.get_block_number(rpc_url)
    claimed   = state['claimedByAddr']

    if scan_from > latest:
        print(f'  Claimed: cache current at block {latest:,}')
        return claimed, latest

    chunks = [(f, min(f + log_chunk - 1, latest)) for f in range(scan_from, latest + 1, log_chunk)]
    print(f'  Claimed: {len(chunks)} chunks ({scan_from:,} → {latest:,})…')

    for i, (frm, to) in enumerate(chunks, 1):
        logs = rpc.eth_get_logs(rpc_url, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   distributor_addr,
            'topics':    [claimed_topic],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            if not rpc.validate_hex(topics[1]) or not rpc.validate_hex(topics[2]):
                continue
            user  = '0x' + topics[1][26:].lower()
            token = '0x' + topics[2][26:].lower()
            if user not in addr_set or token not in kat_tokens:
                continue
            claimed[user] = claimed.get(user, 0) + rpc.from_wei(log.get('data', '0x0'))
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print(f'  Claimed: {len(claimed):,} claimants total            ')
    return claimed, latest


def scan_transfers(state, addr_set, cex_wallets, latest_block, rpc_url,
                   kat_tokens, kat_pool_addrs, transfer_topic, swap_v3_topic,
                   swap_v2_topic, stake_dests, voting_escrow, bridge_dests,
                   zero_addr, log_chunk):
    """One pass over all KAT Transfer events, building dump_raw, buy_raw, stake_raw.

    Single-pass design: splitting the log loop would require 3 passes over the
    same data (30+ min scans), so all three outputs are built together.

    Returns: (dump_raw, buy_raw, stake_raw)
    """
    scan_from = state['katanaBlock'] + 1
    dump_raw  = state['dumpRaw']
    buy_raw   = state.get('buyRaw', {})
    stake_raw = state.get('stakeRaw', {})

    if scan_from > latest_block:
        print(f'  Transfers: cache current at block {latest_block:,}')
        return dump_raw, buy_raw, stake_raw

    buy_sources = kat_pool_addrs | set(cex_wallets)
    chunks = [(f, min(f + log_chunk - 1, latest_block)) for f in range(scan_from, latest_block + 1, log_chunk)]
    print(f'  Transfers: {len(chunks)} chunks × {len(kat_tokens)} tokens ({scan_from:,} → {latest_block:,})…')

    all_logs = []
    for i, (frm, to) in enumerate(chunks, 1):
        for token in kat_tokens:
            logs = rpc.eth_get_logs(rpc_url, {
                'fromBlock': hex(frm),
                'toBlock':   hex(to),
                'address':   token,
                'topics':    [transfer_topic],
            })
            all_logs.extend(logs)
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')
    print(f'  Transfers: {len(all_logs):,} raw events collected            ')

    tx_map = {}  # for dump receipt detection
    for log in all_logs:
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        if not rpc.validate_hex(topics[1]) or not rpc.validate_hex(topics[2]):
            continue
        sender = '0x' + topics[1][26:].lower()
        dest   = '0x' + topics[2][26:].lower()
        amount = rpc.from_wei(log.get('data', '0x0'))

        # ── Staking: to = vKAT escrow or avKAT vault ──
        if dest in stake_dests:
            if sender not in stake_raw:
                stake_raw[sender] = {'stakedVKAT': False, 'stakedAvKAT': False}
            if dest == voting_escrow:
                stake_raw[sender]['stakedVKAT'] = True
            else:
                stake_raw[sender]['stakedAvKAT'] = True

        # ── Buyers: from = pool or CEX → to = external wallet ──
        if (sender in buy_sources
                and dest != zero_addr
                and dest not in bridge_dests
                and dest not in kat_pool_addrs):
            source = 'cex' if sender in cex_wallets else 'dex'
            if dest not in buy_raw:
                buy_raw[dest] = {'katReceived': 0.0, 'katSold': 0.0, 'txCount': 0, 'sources': []}
            buy_raw[dest]['katReceived'] += amount
            buy_raw[dest]['txCount'] += 1
            if source not in buy_raw[dest]['sources']:
                buy_raw[dest]['sources'].append(source)

        # ── Buyer sells: known buyer sending KAT back to a pool ──
        if sender in buy_raw and dest in kat_pool_addrs:
            buy_raw[sender]['katSold'] = buy_raw[sender].get('katSold', 0.0) + amount

        # ── Dump: from = airdrop address ──
        if sender in addr_set:
            tx = log.get('transactionHash', '')
            if not tx:
                continue
            if tx not in tx_map:
                tx_map[tx] = {'from': sender, 'entries': [], 'is_swap': False}
            tx_map[tx]['entries'].append({'to': dest, 'amount': amount})

    # Swap detection for dump data
    receipts_needed = []
    for tx_hash, v in tx_map.items():
        entries = v['entries']
        all_bridge = all(e['to'] == zero_addr or e['to'] in bridge_dests for e in entries)
        if all_bridge:
            continue
        if all(e['to'] in kat_pool_addrs for e in entries):
            v['is_swap'] = True
            continue
        receipts_needed.append(tx_hash)

    print(f'  Transfers: {len(receipts_needed):,} receipts needed for swap detection…')
    receipt_results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(rpc.eth_get_receipt, h, rpc_url): h for h in receipts_needed}
        done = 0
        for fut in as_completed(futs):
            receipt_results[futs[fut]] = fut.result()
            done += 1
            if done % 100 == 0 or done == len(receipts_needed):
                print(f'    {done}/{len(receipts_needed)} receipts…', end='\r')
    if receipts_needed:
        print()

    for tx_hash in receipts_needed:
        rec = receipt_results.get(tx_hash)
        if not rec:
            continue
        logs = rec.get('logs', [])
        if any((l.get('topics') or [''])[0] in (swap_v3_topic, swap_v2_topic) for l in logs):
            tx_map[tx_hash]['is_swap'] = True

    for v in tx_map.values():
        sender = v['from']
        if sender not in dump_raw:
            dump_raw[sender] = {'sold': 0.0, 'bridged': 0.0, 'dests': {}}
        r = dump_raw[sender]
        for entry in v['entries']:
            dest, amount = entry['to'], entry['amount']
            if v['is_swap']:
                r['sold'] += amount
            elif dest == zero_addr or dest in bridge_dests:
                r['bridged'] += amount
            else:
                r['dests'][dest] = r['dests'].get(dest, 0.0) + amount

    print(f'  Transfers: {len(dump_raw):,} airdrop addrs · {len(buy_raw):,} buyers · {len(stake_raw):,} stakers')
    return dump_raw, buy_raw, stake_raw


def scan_avkat_holders(state, stake_raw, rpc_url, avkat_addr, transfer_topic,
                       zero_addr, deploy_block, log_chunk):
    """Scan aVKAT token Transfer events to discover all holders.

    Addresses that received aVKAT shares via zaps/routers won't appear in
    stake_raw from the KAT transfer scan. This closes that gap.
    """
    avkat_holders = state.get('avkatHolders', set())
    if isinstance(avkat_holders, list):
        avkat_holders = set(avkat_holders)
    scan_from = state.get('avkatHolderBlock', deploy_block - 1) + 1
    latest    = rpc.get_block_number(rpc_url)

    if scan_from > latest:
        print(f'  aVKAT holders: cache current at block {latest:,}')
        return avkat_holders, latest

    chunks = [(f, min(f + log_chunk - 1, latest)) for f in range(scan_from, latest + 1, log_chunk)]
    print(f'  aVKAT holders: {len(chunks)} chunks ({scan_from:,} → {latest:,})…')

    new_found = 0
    for i, (frm, to) in enumerate(chunks, 1):
        logs = rpc.eth_get_logs(rpc_url, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   avkat_addr,
            'topics':    [transfer_topic],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            if not rpc.validate_hex(topics[1]) or not rpc.validate_hex(topics[2]):
                continue
            sender = '0x' + topics[1][26:].lower()
            dest   = '0x' + topics[2][26:].lower()
            for addr in (sender, dest):
                if addr != zero_addr and addr != avkat_addr and addr not in avkat_holders:
                    avkat_holders.add(addr)
                    new_found += 1
        if i % 20 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    # Merge into stake_raw so they get balance-fetched
    merged = 0
    for addr in avkat_holders:
        if addr not in stake_raw:
            stake_raw[addr] = {'stakedVKAT': False, 'stakedAvKAT': True}
            merged += 1
        elif not stake_raw[addr].get('stakedAvKAT'):
            stake_raw[addr]['stakedAvKAT'] = True

    print(f'  aVKAT holders: {len(avkat_holders):,} total, {new_found:,} new, {merged:,} merged into stakeRaw')
    return avkat_holders, latest


def scan_cex(state, addr_set, cex_wallets, dump_raw, eth_rpc_url,
             kat_eth_addr, transfer_topic, eth_log_chunk):
    """Scan ETH mainnet for KAT transfers to known CEX wallets."""
    if not cex_wallets:
        return state['cexByAddr'], state['ethBlock']

    # Build reverse map: intermediary dest → original claimer
    dest_to_claimer = {}
    if dump_raw:
        for claimer, raw in dump_raw.items():
            for dest_addr in raw.get('dests', {}):
                dest_to_claimer[dest_addr] = claimer
    eth_senders = addr_set | set(dest_to_claimer)

    scan_from   = state['ethBlock'] + 1
    latest      = rpc.get_block_number(eth_rpc_url)
    cex_by_addr = state['cexByAddr']

    if scan_from > latest:
        print(f'  CEX: cache current at ETH block {latest:,}')
        return cex_by_addr, latest

    chunks = [(f, min(f + eth_log_chunk - 1, latest)) for f in range(scan_from, latest + 1, eth_log_chunk)]
    print(f'  CEX: {len(chunks)} ETH chunks ({scan_from:,} → {latest:,}), tracking {len(eth_senders):,} senders…')

    for i, (frm, to) in enumerate(chunks, 1):
        logs = rpc.eth_get_logs(eth_rpc_url, {
            'fromBlock': hex(frm),
            'toBlock':   hex(to),
            'address':   kat_eth_addr,
            'topics':    [transfer_topic],
        })
        for log in logs:
            topics = log.get('topics', [])
            if len(topics) < 3:
                continue
            if not rpc.validate_hex(topics[1]) or not rpc.validate_hex(topics[2]):
                continue
            sender = '0x' + topics[1][26:].lower()
            dest   = '0x' + topics[2][26:].lower()
            if sender not in eth_senders or dest not in cex_wallets:
                continue
            owner  = dest_to_claimer.get(sender, sender)
            amount = rpc.from_wei(log.get('data', '0x0'))
            if owner not in cex_by_addr:
                cex_by_addr[owner] = {'cexSent': 0.0, 'cexDests': []}
            cex_by_addr[owner]['cexSent'] += amount
            if dest not in cex_by_addr[owner]['cexDests']:
                cex_by_addr[owner]['cexDests'].append(dest)
        if i % 5 == 0 or i == len(chunks):
            print(f'    {i}/{len(chunks)} chunks…', end='\r')

    print(f'  CEX: {len(cex_by_addr):,} addresses with cross-chain CEX activity            ')
    return cex_by_addr, latest
