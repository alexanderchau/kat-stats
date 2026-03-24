"""
KAT Farmer — atomic file I/O helpers.
Named fileio.py (not io.py) to avoid shadowing stdlib io module.
"""
import json, os
from pathlib import Path

def atomic_write(path, content):
    """Write content to path atomically via tmp+os.replace.
    path: Path object or str
    content: str
    """
    path = Path(path)
    tmp  = path.with_suffix('.tmp')
    tmp.write_text(content)
    os.replace(tmp, path)

def load_json(path, default=None):
    """Load JSON from path. Returns default on missing file or parse error."""
    path = Path(path)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f'  ⚠ load_json: could not read {path.name}: {e}')
        return default

def save_json(path, data, compact=True):
    """Write data as JSON to path atomically.
    compact=True  → separators=(',',':')  — for state files, frontend data
    compact=False → indent=2              — for human-readable output files
    """
    if compact:
        text = json.dumps(data, separators=(',', ':'))
    else:
        text = json.dumps(data, indent=2)
    atomic_write(path, text)

def load_state(path, defaults):
    """Load JSON state file and merge against defaults dict.
    Missing keys in the loaded file receive default values.
    Returns defaults if file missing or corrupt.
    """
    path = Path(path)
    loaded = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except Exception as e:
            print(f'  ⚠ Corrupt state file {path.name} ({e}), using defaults')
    if loaded is None:
        return dict(defaults)
    # Merge: loaded values win, missing keys get defaults
    return {**defaults, **loaded}
