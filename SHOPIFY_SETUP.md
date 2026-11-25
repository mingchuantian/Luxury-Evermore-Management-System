# Shopify SKU and image synchronization

The web app searches Shopify product variants by the local seven-character SKU
and stores the matching product's featured image and basic details in MongoDB.
Every matched, non-sold item is moved to `ON_SHELF` ("上架在售"). Sold items are
excluded. By default, a complete synchronization runs every hour.

## Shopify Dev Dashboard

1. Create the app under the same Shopify organization that owns the store.
2. Configure at least `read_products` or `write_products`, plus `write_files`
   for the bulk Content image uploader, plus `read_inventory` and
   `write_inventory` so that marking an item SOLD in this system sets its
   Shopify "available" inventory to 0 at every location. Shopify treats
   `write_products` as implicitly granting product read access and may omit the
   redundant `read_products` entry from the issued token's scope list.
   The "修改 SKU" button on the Shopify库存更新 page changes a variant's SKU
   through `productVariantsBulkUpdate`, so it needs `write_products`
   (`read_products` alone is not enough).
   For URL redirects (reading them, and bulk creating / updating / importing
   them through `urlRedirectCreate`, `urlRedirectUpdate`,
   `urlRedirectImportCreate` + `urlRedirectImportSubmit`), add
   `read_online_store_navigation` and `write_online_store_navigation`.
   After changing scopes, release a new app version and re-approve it on the
   store, then restart the app so it fetches a new token with the new scopes.

   Current scope list (as of 2026-11-25):

   | Scope | Used for |
   |---|---|
   | `write_products` (covers `read_products`) | product lookup, hourly SKU sync, "修改 SKU" |
   | `write_files` | bulk Content image uploader |
   | `read_inventory`, `write_inventory` | set Shopify inventory to 0 when an item is sold; 已无同款 button |
   | `read_online_store_navigation`, `write_online_store_navigation` | URL redirects |

   To see what the live token actually has, run this on the server in the
   project folder (read-only; changes nothing):

   ```bash
   python3 check_shopify_access.py
   ```

   It lists the granted scopes, marks any the system needs but lacks, and
   reads the first few URL redirects as a test.
3. Release the app configuration and install the app on the store.
4. Copy the Client ID and Client secret from the app's Settings page.

The client-credentials grant only works for organization-owned apps installed
on stores owned by that same organization.

If Shopify returns `shop_not_permitted`, open the Dev Dashboard organization
containing the app and verify that the target store also appears under that
same organization's stores. Merely being the store owner or installing an app
created under a different Partner/Dev Dashboard organization is not sufficient.
In that topology, the app must use Shopify's authorization-code installation
flow instead of the client-credentials grant.

## URL重定向管理 (QR codes → product pages)

Each label's QR code opens `https://<store>/<SKU>`; a Shopify URL redirect
`/<SKU> → /products/<handle>` turns that into the product page. The system
manages these redirects itself (this replaces the old Shopify Flow, which only
created redirects once and never repaired them):

- Every hour it scans every product variant on Shopify (Active, Draft,
  Archived; any inventory). For every SKU found it makes sure `/<SKU>` points
  to that product (the Active one if several products carry the SKU; two
  Active products with the same SKU are listed as a conflict and not touched).
- A `/<SKU>` redirect whose SKU was not found in the scan is deleted.
- Only `/<SKU>` redirects are touched: the path is a SKU of this system, or has
  the SKU shape (7 or 9 characters of the SKU alphabet) and points to a product
  page. Pages, collections and other redirects are never changed.
- The first check after the app starts only looks (no changes); later hourly
  runs fix automatically. The Admin page **URL重定向管理** shows what needs
  doing and has "立即检查（只看不改）", "立即检查并修复全部" and per-row buttons.
- Nothing is stored in MongoDB; results live in memory. The same scan also
  gives the 网站状态 shown in 库存列表 (网站在售 / 网站上架·库存0 / 网站草稿 /
  网站已归档 / 不在网站上) and its filter.

Needs `read_online_store_navigation` + `write_online_store_navigation`.
Turn off the old Shopify Flow once the page looks right, so only one thing
manages these redirects.

Optional environment variables:

```text
URL_REDIRECT_ENABLED=true        # false = no background checks at all
URL_REDIRECT_AUTO_FIX=true       # false = hourly runs only check, fixes are manual
URL_REDIRECT_INTERVAL_HOURS=1
```

## Cloud environment variables

Set these as encrypted environment variables or secrets in the cloud platform:

```text
SHOPIFY_SHOP=71eaf7.myshopify.com
SHOPIFY_CLIENT_ID=...
SHOPIFY_CLIENT_SECRET=...
SHOPIFY_API_VERSION=2026-07
SHOPIFY_SYNC_ENABLED=true
SHOPIFY_SYNC_INTERVAL_HOURS=1
```

Keep `SHOPIFY_SHOP` pointed at the permanent Admin API hostname, such as
`71eaf7.myshopify.com`. If the public storefront uses a custom domain, configure
it separately:

```text
SHOPIFY_STOREFRONT_BASE_URL=https://luxuryevermore.com
```

Never place the Client secret or an Admin access token in Git, a Docker image,
frontend JavaScript, or application logs.

The app exchanges the Client ID and secret at Shopify's OAuth token endpoint.
It caches the returned token in memory and refreshes it before the 24-hour
expiry. If Shopify rejects a cached token with HTTP 401, the app obtains a new
token and retries once.

## Cloud scheduling behavior

The scheduler remains non-blocking and runs in a daemon thread. A MongoDB
`background_jobs` lease ensures that only one Gunicorn worker or horizontally
scaled app instance performs a due synchronization run. Other workers remain
available for web requests.

At the start of each run, historical records already marked
`shopify_sku_exist=true` are reconciled to `ON_SHELF` immediately without an API
round trip. Newly discovered SKU matches are moved to `ON_SHELF` as soon as the
hourly Shopify scan finds them. `SOLD` is never changed back, and every automatic
status transition is written to the item and global audit logs as `shopify-sync`.

The web service must stay running. A scale-to-zero or request-only serverless
platform cannot guarantee background execution. On such a platform, configure
its scheduled job facility to run:

```text
python shopify_sync.py
```

The same MongoDB lease prevents this command and the web scheduler from running
the same synchronization cycle concurrently.

## API maintenance

`SHOPIFY_API_VERSION` is configurable. Shopify releases a stable Admin API
version quarterly and supports each stable version for at least 12 months.
Review and update this value during regular maintenance rather than using the
unstable API in production.

`SHOPIFY_ADMIN_ACCESS_TOKEN` remains supported only as a temporary fallback for
older deployments. New deployments should use Client ID and Client secret.

## Inventory set to 0 on sale

When Admin or Management marks an item SOLD (sale form or quick sale), the app
looks up the SKU in Shopify and calls `inventorySetQuantities` to set the
`available` quantity to 0 at every location that still has stock. It uses
compare-and-swap (`changeFromQuantity`) and the required `@idempotent`
directive (API 2026-04+). Staff sales (the `items_view` copy) never touch
Shopify.

A Shopify failure never undoes the sale. The user sees a red message asking
them to set the inventory to 0 in Shopify manually. Products whose inventory
is not tracked in Shopify cannot be zeroed and are reported the same way.
Returning a sold item to stock does not restore Shopify inventory.

## Shopify库存更新 (Admin menu)

Every 2 hours (and on "手动刷新") the app scans all Shopify product variants and
the items collection, read-only, and lists four kinds of mismatch:

- 红 系统内已售出，但 Shopify 上还在卖 (Active, inventory above 0 or not
  tracked). Row button "已无同款，商品设置库存为0" sets that variant's
  available inventory to 0.
- 橙 Shopify 上在卖，但系统中无此SKU (usually a typo in the Shopify SKU).
- 蓝 系统内上架在售，但 Shopify 上买不了 (inventory 0, or Draft/Archived).
- 紫 系统内上架在售，但 Shopify 上找不到此SKU (SKU changed or product deleted
  in Shopify). Links to the product if the item still remembers it from the
  hourly sync, otherwise to a Shopify admin search by the English name.

Only the red kind has a button; the others are fixed in Shopify or by
updating the item's status. The dashboard shows a red alert with the four
counts and the menu shows a red total while the list is not empty. Selling an
item that was linked to Shopify but whose SKU can no longer be found there
shows a red warning.

**Nothing is stored in MongoDB.** The list is computed from live data and kept
only in the web process's memory; restarting the app recomputes it. If the app
runs several worker processes, each keeps its own copy and computes it on its
own schedule, so counts can briefly differ between workers.

Optional environment variables:

```text
SHOPIFY_STOCK_CHECK_ENABLED=true
SHOPIFY_STOCK_CHECK_INTERVAL_HOURS=2
```

The inventory list (库存列表) shows the time of the last hourly Shopify sync at
the top (read from the sync's existing lease record); it turns red if the last
run failed or no run finished within 2 × interval + 30 minutes.
