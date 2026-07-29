# Shopify SKU and image synchronization

The web app searches Shopify product variants by the local seven-character SKU
and stores the matching product's featured image and basic details in MongoDB.
Every matched, non-sold item is moved to `ON_SHELF` ("上架在售"). Sold items are
excluded. By default, a complete synchronization runs every hour.

## Shopify Dev Dashboard

1. Create the app under the same Shopify organization that owns the store.
2. Configure at least `read_products` or `write_products`. Shopify treats
   `write_products` as implicitly granting product read access and may omit the
   redundant `read_products` entry from the issued token's scope list.
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
