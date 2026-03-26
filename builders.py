"""
KAT Farmer — output builders for data.json sections.
"""
from classify import classify
from config import EXCLUDE_STAKERS


def build_output(addresses, claimed_by_addr, dump_raw, cex_by_addr,
                 addr_balances, dest_balances, dest_types, stake_raw=None,
                 eth_balances=None):
    """Build the 'addresses' list for data.json.

    Returns sorted list matching original address order.
    """
    eth_balances = eth_balances or {}
    out = []
    for addr in addresses:
        claimed = claimed_by_addr.get(addr, 0.0)
        r = dump_raw.get(addr, {'sold': 0.0, 'bridged': 0.0, 'dests': {}})

        transferred, transferred_to = 0.0, []
        sent_held,   sent_held_to   = 0.0, []
        defi_positions = []
        for dest, amount in r['dests'].items():
            dtype = dest_types.get(dest, 'Wallet')
            if dtype != 'Wallet':
                defi_positions.append({
                    'address': dest,
                    'amount':  round(amount, 6),
                    'type':    dtype,
                    'held':    dest_balances.get(dest, 0) >= amount * 0.95,
                })
            elif dest_balances.get(dest, 0.0) >= amount * 0.95:
                sent_held += amount
                sent_held_to.append(dest)
            else:
                transferred += amount
                transferred_to.append(dest)

        xchain = cex_by_addr.get(addr, {'cexSent': 0.0, 'cexDests': []})
        bals   = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})

        d = {
            'address':        addr,
            'claimed':        round(claimed, 6),
            'claimable':      0,
            'total':          round(claimed, 6),
            'balance':        round(bals['kat'], 6),
            'avkat':          round(bals['avkat'], 6),
            'moved':          round(r['sold'], 6),
            'transferred':    round(transferred, 6),
            'transferredTo':  transferred_to,
            'sentHeld':       round(sent_held, 6),
            'sentHeldTo':     sent_held_to,
            'defiPositions':  defi_positions,
            'bridged':        round(r['bridged'], 6),
            'ethBalance':     round(eth_balances.get(addr, 0.0), 6),
            'cexSent':        round(xchain['cexSent'], 6),
            'cexDests':       xchain['cexDests'],
        }
        d['status'] = classify(d, stake_raw=stake_raw)
        out.append(d)
    return out


def build_buyers_output(buy_raw, stake_raw, addr_set, addr_balances, vkat_locks,
                        buyer_min_kat=1000):
    """Build the 'buyers' list for data.json.

    Only includes buyers with net KAT purchased >= buyer_min_kat.
    Returns list sorted by katNet descending.
    """
    out = []
    for addr, b in buy_raw.items():
        sources = b.get('sources', [])
        if len(sources) >= 2:
            buy_source = 'both'
        elif sources:
            buy_source = sources[0]
        else:
            buy_source = 'dex'
        kat_net     = b['katReceived'] - b.get('katSold', 0.0)
        bals        = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})
        kat_balance = bals['kat'] + bals['avkat']
        has_vkat    = addr in vkat_locks and vkat_locks[addr]['amount'] > 0
        has_avkat   = bals['avkat'] > 0
        if kat_net < buyer_min_kat:
            continue
        lock_info = vkat_locks.get(addr, {'amount': 0.0})
        vkat_amt  = lock_info['amount'] if has_vkat else 0.0
        avkat_amt = bals['avkat']
        out.append({
            'address':     addr,
            'category':    'airdrop_buyer' if addr in addr_set else 'pure_buyer',
            'buySource':   buy_source,
            'katHeld':     round(kat_balance, 6),
            'katNet':      round(kat_net, 6),
            'katReceived': round(b['katReceived'], 6),
            'katSold':     round(b.get('katSold', 0.0), 6),
            'txCount':     b['txCount'],
            'stakedVKAT':  has_vkat,
            'stakedAvKAT': has_avkat,
            'staked':      has_vkat or has_avkat,
            'vkatAmount':  round(vkat_amt, 6),
            'avkatAmount': round(avkat_amt, 6),
        })
    out.sort(key=lambda x: x['katNet'], reverse=True)
    return out


def build_stakers_output(stake_raw, addr_balances, vkat_locks, staker_min_kat=100):
    """Build the 'stakers' list for data.json.

    Only includes stakers with totalStaked >= staker_min_kat.
    Returns list sorted by totalStaked descending.
    """
    # Merge vKAT NFT holders into stake_raw so they appear even if they
    # never sent a KAT Transfer to the escrow (e.g. received NFT via transfer)
    for addr in vkat_locks:
        if addr not in stake_raw:
            stake_raw[addr] = {'stakedVKAT': True, 'stakedAvKAT': False}
        else:
            stake_raw[addr]['stakedVKAT'] = True

    out = []
    for addr, s in stake_raw.items():
        if addr.lower() in EXCLUDE_STAKERS:
            continue
        bals      = addr_balances.get(addr, {'kat': 0.0, 'avkat': 0.0})
        avkat_bal = bals['avkat']
        lock_info = vkat_locks.get(addr, {'amount': 0.0, 'endTime': 0})
        vkat_amt  = lock_info['amount']
        total     = vkat_amt + avkat_bal
        if total < staker_min_kat:
            continue
        out.append({
            'address':     addr,
            'vkatAmount':  round(vkat_amt, 6),
            'avkatAmount': round(avkat_bal, 6),
            'totalStaked': round(total, 6),
            'stakedVKAT':  vkat_amt > 0,
            'stakedAvKAT': avkat_bal > 0,
        })
    out.sort(key=lambda x: x['totalStaked'], reverse=True)
    return out
