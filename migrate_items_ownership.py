"""One-time migration: assign legacy items to Admin ownership.

The script uses MONGO_URI and MONGO_DB from the process environment. Run it
once in the target deployment environment before enabling ownership filtering.
"""

import os

from pymongo import MongoClient

from luxury_app.constants import OWNERSHIP_ADMIN


def _required_env(name):
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def main():
    mongo_uri = _required_env("MONGO_URI")
    db_name = _required_env("MONGO_DB")

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
        result = client[db_name]["items"].update_many(
            {"$or": [
                {"ownership": {"$exists": False}},
                {"ownership": None},
                {"ownership": ""},
            ]},
            {"$set": {"ownership": OWNERSHIP_ADMIN}},
        )
    finally:
        client.close()

    print(f"Database: {db_name}")
    print(f"Legacy items without ownership: {result.matched_count}")
    print(f"Updated items: {result.modified_count}")
    print(f"Ownership assigned: {OWNERSHIP_ADMIN}")


if __name__ == "__main__":
    main()
