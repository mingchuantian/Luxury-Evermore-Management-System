"""Correct existing negative SOLD profits in inventory.items_view.

This script intentionally updates only the ``profit`` field. It uses the
application's existing MONGO_URI and MONGO_DB environment variables.
"""

from db import get_db


def fix_negative_profits(items_view) -> int:
    result = items_view.update_many(
        {
            "status": "SOLD",
            "$expr": {"$and": [
                {"$isNumber": "$profit"},
                {"$lt": ["$profit", 0]},
            ]},
        },
        [{"$set": {"profit": {"$abs": "$profit"}}}],
    )
    return result.modified_count


def main() -> None:
    changed = fix_negative_profits(get_db()["items_view"])
    print(f"Corrected negative SOLD profits: {changed}")


if __name__ == "__main__":
    main()
