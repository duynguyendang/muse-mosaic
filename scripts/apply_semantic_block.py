#!/usr/bin/env python3
"""Install the canonical semantic blocks (evidence S1/S2, guard S3) into user policies.

Source of truth: trust-engine/policies/semantic/*.mg. Each target
policy carries a verbatim copy between the START/END markers; this script
inserts it (or refreshes it) and adds rules S1/S2 to the policy JSON. A test
asserts every target still carries the canonical text, so copies cannot drift.

    python3 scripts/apply_semantic_block.py            # refresh all targets
    python3 scripts/apply_semantic_block.py --check    # exit 1 if any is stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "trust-engine" / "policies" / "semantic"
# name -> (source file, marker tag, rules the block adds to the policy JSON)
BLOCKS = {
    "evidence": (POLICY_DIR / "semantic-v1.mg", "SEMANTIC-BLOCK v1", ["S1", "S2"]),
    "guard": (POLICY_DIR / "semantic-guard-v1.mg", "SEMANTIC-GUARD v1", ["S3"]),
}

# User-entity policies that support semantic rules, and which blocks each takes.
# The older repo versions (2026-10-27, 2026-10-28) are immutable and not listed.
# analytical_mesh has no escalation rules by design, so it takes no guard.
TARGETS = {
    "trust-engine/policies/user/pol-2026-11-02-v1.mg": ["evidence", "guard"],
    "data/strict-bcbs239/policies/user/pol-strict-bcbs239-v1.mg": ["evidence", "guard"],
    "data/retail-balanced/policies/user/pol-retail-balanced-v1.mg": ["evidence", "guard"],
    "data/analytical-mesh/policies/user/pol-analytical-mesh-v1.mg": ["evidence"],
    "data/kaggle/policies/user/pol-kaggle-2026-10-28-v1.mg": ["evidence", "guard"],  # local-only
}


def markers(tag: str) -> tuple[str, str]:
    return (f"% >>> {tag} (managed by scripts/apply_semantic_block.py)", f"% <<< {tag}")


def wrapped(name: str) -> str:
    source, tag, _ = BLOCKS[name]
    start, end = markers(tag)
    return f"{start}\n{source.read_text(encoding='utf-8').rstrip()}\n{end}\n"


def apply(text: str, names: list[str]) -> str:
    for name in names:
        start, end = markers(BLOCKS[name][1])
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
        if pattern.search(text):
            text = pattern.sub(lambda _, n=name: wrapped(n), text)
        else:
            text = text.rstrip() + "\n\n" + wrapped(name)
    return text


def add_rules(json_path: Path, names: list[str]) -> bool:
    if not json_path.exists():
        return False
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    wanted = [r for n in names for r in BLOCKS[n][2]]
    missing = [r for r in wanted if r not in rules]
    if not missing:
        return False
    data["rules"] = rules + missing
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    stale = []
    for rel, names in TARGETS.items():
        path = ROOT / rel
        if not path.exists():
            print(f"skip (absent): {rel}")
            continue
        current = path.read_text(encoding="utf-8")
        updated = apply(current, names)
        if updated != current:
            stale.append(rel)
            if not args.check:
                path.write_text(updated, encoding="utf-8")
                add_rules(path.with_suffix(".json"), names)
                print(f"updated: {rel}")
        else:
            print(f"ok:      {rel}")
    if args.check and stale:
        print("STALE:", *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
