"""Run one Shopify-to-MongoDB synchronization cycle."""

import json
import sys

from db import get_db
from luxury_app.shopify_maintenance import run_shopify_maintenance


def main() -> int:
    db = get_db()
    result = run_shopify_maintenance(
        db["items"],
        job_locks=db["background_jobs"],
        audit_logs=db["audit_logs"],
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
