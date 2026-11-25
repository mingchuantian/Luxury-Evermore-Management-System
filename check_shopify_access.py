"""Check which Shopify permissions this system's app currently has (read-only).

Run on the server, in the project folder:

    python3 check_shopify_access.py

It reads the Shopify settings from the environment, or from a `.env` file next
to this script, asks Shopify for a fresh token, and prints the granted access
scopes plus a small read-only test of URL redirects. Nothing is changed in
Shopify or in the database.
"""

import os
import sys
from pathlib import Path

# The scopes this system uses, and what for. write_x also covers read_x.
NEEDED = [
    ("write_products", "读商品、改 SKU（Shopify库存更新的“修改 SKU”）"),
    ("write_files", "批量上传商品图片"),
    ("write_inventory", "卖出后把 Shopify 库存改成 0"),
    ("write_online_store_navigation", "读取和批量修改 URL 重定向"),
]


def load_env_file(path):
    """Fill in missing environment variables from a simple KEY=VALUE file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def granted_scopes(gql):
    data = gql("{ currentAppInstallation { accessScopes { handle } } }", {})
    return sorted(s["handle"] for s in data["currentAppInstallation"]["accessScopes"])


def has_scope(scopes, needed):
    if needed in scopes:
        return True
    # write_x implies read_x, never the other way round.
    return needed.startswith("read_") and ("write_" + needed[len("read_"):]) in scopes


def main():
    load_env_file(Path(__file__).resolve().parent / ".env")
    # Import only after the environment is ready: the module reads it on import.
    from luxury_app.shopify_maintenance import SHOP, TOKEN_PROVIDER, shopify_gql

    problem = TOKEN_PROVIDER.configuration_error() if hasattr(TOKEN_PROVIDER, "configuration_error") else None
    if problem:
        print("Shopify 设置不完整：", problem)
        return 2
    TOKEN_PROVIDER.get_token(force_refresh=True)   # a fresh token carries newly released scopes

    print(f"店铺：{SHOP}")
    scopes = granted_scopes(shopify_gql)
    print("已授予的权限：", ", ".join(scopes) or "（无）")
    print()
    missing = 0
    for scope, purpose in NEEDED:
        ok = has_scope(scopes, scope)
        missing += not ok
        print(f"  {'✓' if ok else '✗ 缺少'} {scope:32s} {purpose}")
    print()

    try:
        data = shopify_gql("{ urlRedirects(first: 5) { nodes { path target } } }", {})
        nodes = data["urlRedirects"]["nodes"]
        print(f"URL 重定向读取测试：成功，前 {len(nodes)} 条：")
        for node in nodes:
            print(f"    {node['path']}  →  {node['target']}")
    except Exception as error:   # noqa: BLE001 - show whatever Shopify said
        print("URL 重定向读取测试：失败")
        print("   ", " ".join(str(error).split())[:400])
        missing += 1
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
