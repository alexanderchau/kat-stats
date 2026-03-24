#!/usr/bin/env python3
"""
Enrich kat-farmer wallet addresses with identity labels.

Layers (cheapest first):
  1. Etherscan scraped labels (brianleect/etherscan-labels) — 30K+ addresses, local lookup
  2. ENS reverse lookup (api.ensdata.net) — free, concurrent with rate limiting

Outputs labels.json for the kat-farmer dashboard.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import json
import fileio

SCRIPT_DIR = Path(__file__).parent
DATA_FILE = SCRIPT_DIR / "data.json"
OUTPUT_FILE = SCRIPT_DIR / "labels.json"

ETHERSCAN_LABELS_URL = "https://raw.githubusercontent.com/brianleect/etherscan-labels/main/data/etherscan/combined/combinedAllLabels.json"
ENSDATA_URL = "https://api.ensdata.net/{}"

ENS_WORKERS = 10  # concurrent ENS lookups
ENS_TIMEOUT = 5   # seconds per request


def load_addresses():
    data = fileio.load_json(DATA_FILE, {})
    addrs = set()
    for entry in data.get("addresses", []):
        addrs.add(entry["address"].lower())
    for entry in data.get("buyers", []):
        addrs.add(entry["address"].lower())
    for entry in data.get("stakers", []):
        addrs.add(entry["address"].lower())
    return sorted(addrs)


def fetch_etherscan_labels():
    print("Fetching Etherscan labels dump...", flush=True)
    try:
        req = Request(ETHERSCAN_LABELS_URL, headers={"User-Agent": "kat-farmer-enricher/1.0"})
        with urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Failed: {e}", flush=True)
        return {}
    normalized = {}
    for addr, info in raw.items():
        key = addr.lower()
        name = info.get("name", "")
        labels = info.get("labels", [])
        if name or labels:
            normalized[key] = {"name": name, "labels": labels}
    print(f"  Loaded {len(normalized)} Etherscan labels", flush=True)
    return normalized


def lookup_ens(address):
    url = ENSDATA_URL.format(address)
    try:
        req = Request(url, headers={"User-Agent": "kat-farmer-enricher/1.0"})
        with urlopen(req, timeout=ENS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return address, data.get("ens_primary") or data.get("ens") or None
    except Exception:
        return address, None


def enrich():
    addresses = load_addresses()
    print(f"Total unique addresses: {len(addresses)}", flush=True)

    labels = {}

    # Layer 1: Etherscan labels
    etherscan = fetch_etherscan_labels()
    etherscan_hits = 0
    for addr in addresses:
        if addr in etherscan:
            info = etherscan[addr]
            label = info["name"] if info["name"] else ", ".join(info["labels"])
            if label:
                labels[addr] = label
                etherscan_hits += 1
    print(f"  Etherscan matches: {etherscan_hits}", flush=True)

    # Layer 2: ENS reverse lookup (concurrent)
    unlabeled = [a for a in addresses if a not in labels]
    print(f"\nENS lookup for {len(unlabeled)} addresses ({ENS_WORKERS} workers)...", flush=True)
    ens_hits = 0
    done = 0

    with ThreadPoolExecutor(max_workers=ENS_WORKERS) as pool:
        futures = {pool.submit(lookup_ens, addr): addr for addr in unlabeled}
        for future in as_completed(futures):
            addr, ens_name = future.result()
            done += 1
            if ens_name:
                labels[addr] = ens_name
                ens_hits += 1
            if done % 200 == 0:
                print(f"  Progress: {done}/{len(unlabeled)} ({ens_hits} ENS hits)", flush=True)

    print(f"  ENS matches: {ens_hits}", flush=True)

    # Summary
    print(f"\n--- Summary ---", flush=True)
    print(f"Total addresses: {len(addresses)}", flush=True)
    print(f"Labeled: {len(labels)} ({len(labels)/len(addresses)*100:.1f}%)", flush=True)
    print(f"  Etherscan: {etherscan_hits}", flush=True)
    print(f"  ENS: {ens_hits}", flush=True)
    print(f"Unlabeled: {len(addresses) - len(labels)}", flush=True)

    fileio.save_json(OUTPUT_FILE, labels, compact=False)
    print(f"\nWrote {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    enrich()
