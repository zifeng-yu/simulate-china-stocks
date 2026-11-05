"""Prepare, apply/resume, or explicitly roll back a reviewed maintenance cutover."""

import argparse
import json
from pathlib import Path

from china_stock_sim.ledger.storage_promotion import (
    apply_promotion,
    prepare_promotion,
    rollback_promotion,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--staging-receipt')
    parser.add_argument('--projection-receipt')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--maintenance-confirmed', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        if not args.staging_receipt or not args.projection_receipt or args.plan:
            parser.error('prepare requires both receipts and no existing plan')
        result = prepare_promotion(args.root, staging_receipt=args.staging_receipt,
                                   projection_receipt=args.projection_receipt)
    else:
        if not args.plan or not args.maintenance_confirmed:
            parser.error('apply/rollback require --plan and --maintenance-confirmed')
        method = apply_promotion if args.action == 'apply' else rollback_promotion
        result = method(args.root, json.loads(args.plan.read_bytes()),
                        maintenance_confirmed=args.maintenance_confirmed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
