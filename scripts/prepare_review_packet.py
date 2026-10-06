"""Prepare a pending review packet; never claims approval or fresh holdout."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legal_rag.evaluation_governance import GovernanceError, prepare_review_packet, write_json_exclusive  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a pending legal-gold review packet without providers, keys or automatic approval.")
    parser.add_argument("--dataset", default="legal-eval-v3")
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True, help="Fresh file, never overwritten")
    args = parser.parse_args(argv)
    root = args.repository_root.absolute()
    output = args.output if args.output.is_absolute() else root / args.output
    try:
        packet = prepare_review_packet(args.dataset, repository_root=root)
        digest = write_json_exclusive(output, packet, root=root)
        print(json.dumps({"status": "pending_human_review", "case_count": len(packet["cases"]),
                          "human_review_complete": False, "holdout_admitted": False,
                          "packet_sha256": digest}, ensure_ascii=False))
        return 0
    except (GovernanceError, ValueError, OSError):
        print(json.dumps({"status": "failed", "error_code": "review_packet_preparation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
