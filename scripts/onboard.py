#!/usr/bin/env python3
"""Zero-touch onboarding: sources + expected result -> derived bundle -> run.

    # step 1: derive only (free; no engine needed; no model unless USE_MOCK_MUSE=false).
    # Derived terms/ranks land as *pending review*; nothing loads until approval.
    python3 scripts/onboard.py --deepmatcher amazon-google \
        --expected-structure expected.json --out /tmp/onboard

    # step 1b: review and approve the derived terms/ranks (human gate)
    python3 scripts/onboard.py --show-review /tmp/onboard/<bundle>
    python3 scripts/onboard.py --accept /tmp/onboard/<bundle> --reviewer you

    # step 2: restart the trust engine with the bundle, then run the build
    # against the *same* input; the script refuses a mismatched input or an
    # unapproved bundle
    POLICY_DIR=/tmp/onboard/<bundle>/policies TAXONOMY_DIR=/tmp/onboard/<bundle>/taxonomy ...
    python3 scripts/onboard.py --deepmatcher amazon-google \
        --expected-structure expected.json --bundle /tmp/onboard/<bundle>

Nothing is auto-loaded anywhere: the bundle is derived first, then the
build runs with POLICY_DIR/TAXONOMY_DIR implicitly set for this process to
the bundle's directories. The trust engine is NOT started by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "mosaic-api"
sys.path.insert(0, str(API_DIR))
os.environ.setdefault("USE_MOCK_MUSE", "false")
os.environ.setdefault("MANGLEKIT_BIN", os.environ.get("MANGLEKIT_BIN", ""))

import onboarding  # noqa: E402
from config import get_settings  # noqa: E402
from jobs import execute_full_build  # noqa: E402
from ledger import init_db  # noqa: E402
from policies import load_policies  # noqa: E402
from schemas import BuildDataset, BuildRequest, Document  # noqa: E402
from taxonomies import load_taxonomies  # noqa: E402

_DEEPMATCHER = {
    "amazon-google": {
        "zip": "amazon_google_exp_data.zip",
        "prefix": "",
        "sources": ("amazon", "google"),
        "fields": ("title", "manufacturer", "price"),
        "entity_type": "product",
    },
    "dblp-acm-dirty": {
        "zip": "dirty_dblp_acm_exp_data.zip",
        "prefix": "exp_data/",
        "sources": ("dblp", "acm"),
        "fields": ("title", "authors", "venue", "year"),
        "entity_type": "publication",
    },
}


def documents_from_deepmatcher(name: str) -> list[dict]:
    spec = _DEEPMATCHER[name]
    # fixed instant: same input must hash to the same bundle (replay determinism)
    event_time = "2026-10-27T09:00:00+07:00"
    docs = []
    for tag, source in (("A", spec["sources"][0]), ("B", spec["sources"][1])):
        for row in _raw_rows_for(name, tag):
            fields = {}
            for field in spec["fields"]:
                value = (row.get(field) or "").strip()
                if value:
                    fields[field] = {
                        "value": value,
                        "authority_rank": 3,
                        "event_time": event_time,
                        "verified": True,
                        "ttl_seconds": 3600,
                    }
            if fields:
                docs.append(
                    {
                        "document_id": f"{tag}-{row.get('id')}",
                        "entity_type": spec["entity_type"],
                        "source": source,
                        "synthetic": True,
                        "fields": fields,
                    }
                )
    return docs


def _raw_rows_for(name: str, table: str) -> list[dict]:
    spec = _DEEPMATCHER[name]
    import csv

    zf = zipfile.ZipFile(ROOT / "data" / "deepmatcher" / spec["zip"])
    path = spec["prefix"] + ("tableA.csv" if table == "A" else "tableB.csv")
    return list(csv.DictReader(io.StringIO(zf.read(path).decode("utf-8", "replace"))))


async def _run(
    bundle_dir: Path, entity_type: str, documents: list[dict], expected_structure: dict | None
) -> dict:
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    policies = load_policies(bundle_dir / "policies")
    taxonomies = load_taxonomies(bundle_dir / "taxonomy")
    settings = get_settings()
    request = BuildRequest(
        work_id=f"onboard-{manifest['policy']['policy_version']}",
        idempotency_key=f"onboard-{manifest['policy']['policy_version']}",
        dataset=BuildDataset(documents=[Document.model_validate(d) for d in documents]),
        expected_structure=expected_structure,
    )
    expected = {entity_type: expected_structure} if expected_structure else {}
    init_db(settings.database_path)  # same DDL the service runs at startup
    return await execute_full_build(settings, policies, taxonomies, expected, request)


def _verify_bundle_matches(
    bundle_dir: Path, documents: list[dict], expected_structure: dict | None
) -> dict:
    """Fail-closed input check: refuse to evaluate a different input."""
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"no manifest.json under {bundle_dir}; is this a bundle?")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    from bundle_review import ReviewPending

    try:
        manifest = onboarding.verify_bundle_approved(bundle_dir)
    except ReviewPending as exc:
        raise SystemExit(
            f"bundle review is not approved: {exc}\n"
            f"review it, then approve with:\n"
            f"  python3 scripts/onboard.py --accept {bundle_dir} --reviewer <id>\n"
            f"inspect first with:\n"
            f"  python3 scripts/onboard.py --show-review {bundle_dir}"
        ) from exc
    inputs = manifest.get("inputs", {})
    digest = onboarding._stable_digest(documents)
    if inputs.get("documents_sha256") != digest:
        raise SystemExit(
            "input documents do not match the bundle's derivation input; "
            "refusing to evaluate a different dataset against it"
        )
    structure_sha = (
        hashlib.sha256(
            json.dumps(expected_structure, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if expected_structure is not None
        else None
    )
    if inputs.get("expected_structure_sha256") != structure_sha:
        raise SystemExit(
            "expected structure does not match the bundle's derivation input; "
            "refusing to evaluate with a different contract"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--documents", type=Path, help="JSON file with a document list")
    source.add_argument("--deepmatcher", choices=sorted(_DEEPMATCHER))
    parser.add_argument(
        "--accept",
        type=Path,
        default=None,
        help="review gate: approve a derived bundle (requires --reviewer)",
    )
    parser.add_argument(
        "--show-review",
        type=Path,
        default=None,
        help="print the pending (or approved) review for a bundle and exit",
    )
    parser.add_argument("--reviewer", default=None, help="person signing off the bundle")
    parser.add_argument("--note", default="", help="approval note (stored in approval.json)")
    parser.add_argument("--entity-type", default=None)
    parser.add_argument("--expected-structure", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="derive mode: where to write the bundle"
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="run mode: an already-derived, **approved** bundle to build against",
    )
    args = parser.parse_args()

    # --- review gate modes: no dataset input required -----------------------
    if args.show_review is not None:
        review_path = args.show_review / "review.json"
        if not review_path.is_file():
            print(f"no review.json under {args.show_review}; is this a bundle?", file=sys.stderr)
            return 2
        print(
            json.dumps(
                json.loads(review_path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False
            )
        )
        return 0
    if args.accept is not None:
        if not args.reviewer:
            parser.error("--accept requires --reviewer (name of the person signing off)")
        from bundle_review import ReviewPending

        try:
            approval = onboarding.accept_review(args.accept, reviewer=args.reviewer, note=args.note)
        except ReviewPending as exc:
            print(f"refusing to approve: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"approved": True, **approval}, indent=2, ensure_ascii=False))
        return 0

    if not args.deepmatcher and not args.documents:
        parser.error(
            "one of --documents / --deepmatcher is required (or use --accept / --show-review)"
        )

    if args.deepmatcher:
        documents = documents_from_deepmatcher(args.deepmatcher)
        entity_type = args.entity_type or _DEEPMATCHER[args.deepmatcher]["entity_type"]
    else:
        payload = json.loads(args.documents.read_text(encoding="utf-8"))
        documents = payload["documents"] if isinstance(payload, dict) else payload
        entity_type = args.entity_type

    expected_structure = (
        json.loads(args.expected_structure.read_text(encoding="utf-8"))
        if args.expected_structure
        else None
    )

    settings = get_settings()
    if args.bundle is not None:
        bundle_dir = args.bundle
        _verify_bundle_matches(bundle_dir, documents, expected_structure)
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        entity_type = manifest["entity_type"]
        result = asyncio.run(_run(bundle_dir, entity_type, documents, expected_structure))
        summary = {k: result.get(k) for k in ("verdict", "policy_versions", "splink_status")}
        summary["goldens"] = len(result.get("goldens", []))
        summary["stewards"] = len(result.get("steward_queue", []))
        summary["bundle"] = str(bundle_dir)
        print(json.dumps({"build": summary}, indent=2, ensure_ascii=False))
        return 0

    if args.out is None:
        parser.error("derive mode needs --out (or use --bundle to run an existing bundle)")
    try:
        manifest = asyncio.run(
            onboarding.assemble_bundle(
                documents,
                entity_type=entity_type,
                expected_structure=expected_structure,
                out_root=args.out,
                settings=settings,
            )
        )
    except onboarding.OnboardRefused as refused:
        print(
            json.dumps(
                {
                    "refused": True,
                    "code": refused.code,
                    "message": refused.message,
                    "details": refused.details,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
    print(
        "first: review the derived decisions, then approve the bundle:\n"
        f"  python3 scripts/onboard.py --show-review {manifest['bundle_dir']}\n"
        f"  python3 scripts/onboard.py --accept {manifest['bundle_dir']} --reviewer <you>\n"
        "then: start the trust engine with\n"
        f"  POLICY_DIR={manifest['bundle_dir']}/policies "
        f"TAXONOMY_DIR={manifest['bundle_dir']}/taxonomy\n"
        "then re-run with the same input plus --bundle <dir> to execute the build "
        "(the run refuses an unapproved bundle).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
