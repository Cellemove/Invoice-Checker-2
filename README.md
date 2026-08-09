# Invoice Checker

A production-ready tool that reconciles uploaded invoice files (`.xlsx` / `.csv`)
against **live Shopify order data**. It performs full three-dimensional
reconciliation:

1. **Match** invoice line items to Shopify orders by order id / order number.
2. **Compare** invoice line-item prices, quantities and totals against the
   matched Shopify order amounts.
3. **Verify** that products / SKUs on the invoice exist in Shopify inventory.

For every line it reports one of: `matched`, `price mismatch`,
`quantity mismatch`, `missing in Shopify`, or `missing in invoice`, plus a
SKU-in-inventory flag and an amount variance.

> **Internal build — authentication is disconnected.** The app opens straight
> into the tool with no login. The Supabase Auth code is still in the repo
> ([`frontend/src/components/Auth.jsx`](frontend/src/components/Auth.jsx),
> [`backend/app/auth.py`](backend/app/auth.py)) but is wired out of the request
> flow. To turn it back on, set `AUTH_REQUIRED=true` on the backend and gate the
> frontend on a Supabase session — see [Re-enabling auth](#re-enabling-auth).

```
Invoice2.0/
├── backend/        # FastAPI + Python reconciliation engine
├── frontend/       # React (Vite) single-page app
├── supabase/       # SQL schema (comparison_runs + RLS policies)
└── sample-data/    # sample_invoice.csv for a quick test
```

---

## Architecture

| Layer     | Tech                          | Responsibility                                            |
| --------- | ----------------------------- | --------------------------------------------------------- |
| Frontend  | React + Vite, Supabase JS     | Auth, file upload, results table, summary, history        |
| Backend   | FastAPI, pandas/openpyxl, httpx | Parsing, Shopify Admin API calls, reconciliation, JWT verify |
| Auth      | Supabase Auth (email/password)| Issues JWTs; backend verifies them on every request       |
| Storage   | Supabase Postgres             | `comparison_runs` history (Row Level Security enabled)    |

By default (internal build) endpoints do **not** require a token; runs are
attributed to `DEFAULT_USER_ID`. When `AUTH_REQUIRED=true`, every endpoint
requires a valid Supabase JWT (`Authorization: Bearer <token>`), verified
**offline** using the project's `SUPABASE_JWT_SECRET`.

---

## Prerequisites

- **Python 3.10+**
- **Node.js 18+**
- A **quotation** price-list `.xlsx` (per product code × quantity × country)
- (Optional) a **GCP** project + service account to read files from Google Drive
- (Optional) **Supabase** project for saving comparison history

---

## Google Drive setup (GCP service account)

Lets the app read invoices and quotations straight from a shared Drive folder —
headless, no OAuth pop-ups. One-time setup:

1. **GCP project** — go to <https://console.cloud.google.com>, pick or create a
   project (top bar).
2. **Enable the Drive API** — APIs & Services → Library → search **Google Drive
   API** → **Enable**.
3. **Create a service account** — APIs & Services → Credentials → **Create
   credentials → Service account**. Name it (e.g. `invoice-checker`), Create,
   skip the optional role steps, Done.
4. **Create a key** — open the service account → **Keys** tab → **Add key →
   Create new key → JSON**. A `.json` file downloads. This is the credential —
   keep it secret (it's gitignored).
5. **Place the key** — save it as `backend/service-account.json`.
6. **Share the Drive folder** — in Google Drive, put your invoice + quotation in
   a folder, right-click → **Share**, and share it with the service account's
   email (the `client_email` in the JSON, ends in
   `…iam.gserviceaccount.com`) as **Viewer**.
7. **Configure the backend `.env`:**
   ```env
   GOOGLE_SERVICE_ACCOUNT_FILE=./service-account.json
   # Optional — restrict to one folder (the id from the folder URL
   # https://drive.google.com/drive/folders/<THIS_ID>):
   GOOGLE_DRIVE_FOLDER_ID=<folder id>
   ```
8. **Restart the backend.** The **Google Drive** panel in the app now lists your
   files — click **Use as invoice** to reconcile, or **Set as quotation** to
   update the price list.

> The service account only ever needs read access, and can only see files you
> explicitly share with it. Its email is shown in the Drive panel and via
> `GET /drive/status`.

---

## 1. Supabase setup (optional — only for comparison history)

Auth is disconnected, so Supabase is only used to persist comparison history. If
you skip this, the app still uploads, compares and shows results — the History
tab just stays empty.

1. Create a project at <https://supabase.com>.
2. Open **SQL Editor** → paste and run [`supabase/schema.sql`](supabase/schema.sql).
   This creates the `comparison_runs` table (internal build: nullable `user_id`,
   no RLS).
3. Collect these from **Project Settings → API** for the backend `.env`:
   - Project URL → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (backend only — keep secret)

   The `anon` key and `SUPABASE_JWT_SECRET` are only needed if you later enable
   auth.

---

## 2. Backend (FastAPI)

```bash
cd backend
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # then edit .env with your real values
uvicorn app.main:app --reload --port 8000
```

The API is now at <http://localhost:8000> (interactive docs at `/docs`).
Check configuration with `GET /health`.

### Backend environment variables (`backend/.env`)

| Variable                     | Required | Description                                            |
| ---------------------------- | -------- | ------------------------------------------------------ |
| `SHOPIFY_STORE_URL`          | ✅       | e.g. `https://your-store.myshopify.com`                |
| `SHOPIFY_API_KEY`            | ◑        | App client id — used to fetch a token automatically    |
| `SHOPIFY_API_SECRET`         | ◑        | App client secret — used with the key (keep secret)    |
| `SHOPIFY_ACCESS_TOKEN`       | ◑        | Static token override (skips the auto-fetch)           |
| `SUPABASE_URL`               | ⬜       | Supabase project URL (only for history persistence)    |
| `SUPABASE_SERVICE_ROLE_KEY`  | ⬜       | Service-role key (only for history persistence)        |
| `AUTH_REQUIRED`              | ⬜       | `false` (default) disconnects auth; `true` enforces JWT|
| `DEFAULT_USER_ID`            | ⬜       | UUID runs are attributed to while auth is off          |
| `SUPABASE_JWT_SECRET`        | ⬜       | JWT secret — required only when `AUTH_REQUIRED=true`    |
| `SUPABASE_ANON_KEY`          | ⬜       | Only needed if you re-enable auth                      |
| `SHOPIFY_API_VERSION`        | ⬜       | Defaults to `2024-07`                                  |
| `CORS_ORIGINS`               | ⬜       | Comma-separated allowed origins                        |
| `PORT`                       | ⬜       | Defaults to `8000`                                     |

> `◑` = provide **either** `SHOPIFY_API_KEY` + `SHOPIFY_API_SECRET` (recommended)
> **or** a static `SHOPIFY_ACCESS_TOKEN`. If both are present the static token wins.

#### How the app gets the Shopify access token

Instead of pasting a permanent token, give the app your **API key + secret** and
it fetches an access token itself via Shopify's
[OAuth client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant)
(`POST /admin/oauth/access_token`, `grant_type=client_credentials`).

- Tokens last 24h and are **auto-refreshed**; they're cached in a gitignored
  `backend/.shopify_tokens.json` so they survive restarts.
- The app fetches lazily on first Shopify call. You can also trigger it from the
  UI: the **Shopify connection** bar shows status and a **Connect / Refresh**
  button, backed by `GET /shopify/auth/status` and `POST /shopify/auth/connect`.
- The token value is never sent to the browser or logged.
- Eligibility: the client credentials grant is for an app your own organization
  develops and installs on a store you own (the internal-tool case). If you only
  have a `shpat_…` token, set `SHOPIFY_ACCESS_TOKEN` instead and it's used as-is.

#### Multiple Shopify stores

Add suffixed credential sets and the frontend can select between them:

```env
# EU store via client credentials (auto-fetched token)
SHOPIFY_STORE_URL_EU=https://eu-store.myshopify.com
SHOPIFY_API_KEY_EU=eu-client-id
SHOPIFY_API_SECRET_EU=eu-client-secret
# US store via a static token
SHOPIFY_STORE_URL_US=https://us-store.myshopify.com
SHOPIFY_ACCESS_TOKEN_US=shpat_yyy
```

These appear as stores `eu` and `us` (plus `default`) in the UI's store picker.

---

## 3. Frontend (React + Vite)

```bash
cd frontend
npm install
cp .env.example .env        # then edit .env
npm run dev
```

App runs at <http://localhost:5173>.

### Frontend environment variables (`frontend/.env`)

| Variable                | Description                              |
| ----------------------- | ---------------------------------------- |
| `VITE_SUPABASE_URL`     | Supabase project URL                     |
| `VITE_SUPABASE_ANON_KEY`| Supabase anon key                        |
| `VITE_API_BASE_URL`     | Backend base URL (`http://localhost:8000`) |

---

## 4. Try it

1. Open <http://localhost:5173> — the tool loads directly (no login).
2. Pick a Shopify store (if more than one configured).
3. Upload [`sample-data/sample_invoice.csv`](sample-data/sample_invoice.csv)
   (or any invoice with columns like *Order Number, SKU, Product, Quantity,
   Unit Price, Line Total*).
4. The app uploads → parses → compares automatically and shows:
   - A **summary** (matched / mismatch / missing counts + total variance)
   - A filterable **results table** with per-row status badges
5. Open the **History** tab to see past runs (read straight from Supabase via RLS).

> The sample file references order numbers `1001`–`1004`. To see real matches,
> use order numbers and SKUs that actually exist in your Shopify store.

---

## Accepted invoice columns

The parser normalises headers and accepts common aliases (case/spacing
insensitive):

| Canonical field | Accepted header examples                                  |
| --------------- | --------------------------------------------------------- |
| `order_id`      | Order Number, Order ID, Order, Invoice Ref, Reference     |
| `sku`           | SKU, SKU Code, Variant SKU, Product Code, Barcode         |
| `product`       | Product, Title, Item, Description, Name                   |
| `quantity`      | Quantity, Qty, Units, Count                               |
| `unit_price`    | Unit Price, Price, Rate, Cost                             |
| `line_total`    | Line Total, Total, Amount, Subtotal, Extended Price       |
| invoice total   | Invoice Total, Grand Total, Total Due, Balance Due        |

Missing `line_total` is derived from `quantity × unit_price` when possible
(and vice-versa).

### Multi-sheet workbooks

For `.xlsx` files with multiple worksheets, every sheet that has recognisable
invoice columns is **combined into one invoice**, exact-duplicate rows are
dropped, and Excel auto-recovery sheets (named `Recovered_Sheet…`) are skipped.
The upload response reports `sheets_used` / `sheets_skipped`, shown in the UI
after upload.

### Store routing & order-level matching

Reconciliation is **order-level**: one result row per order. An order is
`matched` when it exists in its store; the row shows the invoice total vs the
Shopify order total (variance is informational) and whether the order's SKUs
exist in the store catalog. Line-by-line SKU pairing is intentionally not
attempted, because Shopify order line items often lack SKUs and use localized
variant names.

The **store selector** offers **Auto — route by region** (default) plus each
configured store. In Auto mode each line is routed to the store matching its
region (from the file's `Store` column, e.g. "Cellumove DE(Plus)" → `de`), all
stores are queried concurrently, and results merge into **one per-file table**.
Regions with no configured store (e.g. `USA` when no US store exists) are
reported under `unrouted_regions` and shown as "missing in Shopify". Stores
whose catalog has no SKUs (e.g. CZ) skip the SKU-existence check rather than
flagging every line as not-found.

### Reconciliation modes

The **Reconcile** dropdown (and the `check_prices` field on `POST /compare`)
selects how amounts are treated:

- **Orders & SKUs only** (`check_prices: false`) — verify each line's order
  number exists in Shopify and each SKU exists in inventory; price/quantity
  differences are **not** flagged as mismatches. Use this when the invoice
  amount is a fulfilment/shipping cost rather than the Shopify sale price.
  Amount variance is still computed and shown for information.
- **Full** (`check_prices: true`) — additionally flag `price mismatch` /
  `quantity mismatch` per line.

---

## API reference

In the internal build no `Authorization` header is required. When
`AUTH_REQUIRED=true`, all endpoints require `Authorization: Bearer <supabase_jwt>`.

| Method | Path               | Description                                          |
| ------ | ------------------ | ---------------------------------------------------- |
| `POST` | `/upload`          | Parse an `.xlsx`/`.csv` file → structured line items |
| `GET`  | `/shopify/stores`  | List configured Shopify store keys                   |
| `GET`  | `/shopify/orders`  | Fetch orders by `refs` (repeatable) + optional `store` |
| `POST` | `/compare`         | Full reconciliation; persists a `comparison_runs` row |
| `GET`  | `/history`         | Recent comparison runs for the current user          |
| `GET`  | `/health`          | Liveness + config sanity (no secrets)                |

---

## Deploy to Vercel

The repo ships a multi-service [`vercel.json`](vercel.json): the **frontend**
(Vite) and **backend** (FastAPI, auto-detected at `backend/app/main.py`) deploy
as one project, with `/api/*` rewritten to the backend and everything else to
the frontend. The API is mounted both at `/...` (local dev) and `/api/...`
(behind the rewrite), and the built frontend calls `/api` automatically in
production.

### Steps

1. Push the repo to Git and import it into Vercel (or run `vc deploy` from the
   repo root — CLI ≥ 48.1.8).
2. Set the backend environment variables in the Vercel project:

   | Variable | Value |
   | --- | --- |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the **contents** of `service-account.json` (one line). The file itself is `.vercelignore`d and must never be uploaded. |
   | `GOOGLE_DRIVE_FOLDER_ID` | Optional — restrict Drive listing to one folder |
   | `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Optional — comparison history |
   | `QUOTATION_SHEET` | Optional — defaults to `QTY=1-5` |

3. Deploy. `GET /api/health` should return `{"status": "ok", ...}`.

### Serverless notes

- The filesystem is read-only except `/tmp`, and instances don't share state.
  The app handles this: writable caches move to `/tmp` when `VERCEL` is set,
  and the frontend pins the chosen quotation by **Drive file id** with every
  compare (`quotation_file_id`), so any instance can serve any request.
- Local `.xlsx` files aren't uploaded — in the cloud, invoices and quotations
  come from Google Drive (or manual upload).
- Very large invoices: request bodies are capped (~4.5 MB) on Vercel functions;
  the biggest weekly files (~6k rows) fit, but keep it in mind.
- **⚠ Auth is disconnected** — a deployed URL is public. Enable Vercel
  **Deployment Protection** (or re-enable auth below) before sharing the URL.

## Re-enabling auth

The auth code is intact and switched off via configuration:

1. **Backend** — set `AUTH_REQUIRED=true` (and `SUPABASE_JWT_SECRET`) in
   `backend/.env`. Every endpoint then rejects requests without a valid JWT.
   No router changes are needed — `get_optional_user` delegates to the strict
   verifier automatically.
2. **Frontend** — set `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY`, then gate
   the app on a session in [`App.jsx`](frontend/src/App.jsx): render
   [`<Auth />`](frontend/src/components/Auth.jsx) when there is no session (the
   earlier auth-gated version of `App.jsx` did exactly this).
3. **Database** — uncomment the "secure variant" block in
   [`supabase/schema.sql`](supabase/schema.sql) to add the `auth.users` foreign
   key and per-user RLS policies.

## Security notes

- This is an **internal** build: auth is disconnected, so the API is open to
  anyone who can reach it. Deploy it only on a trusted network / behind a
  gateway, or re-enable auth as above.
- Secrets live only in environment variables — never committed (`.gitignore`
  excludes `.env`).
- The backend uses the **service-role** key server-side only; it is never sent
  to the browser.
- Uploads are validated by extension and size (10 MB cap) and parsed defensively.
- Shopify calls retry on `429`/`5xx` with backoff and respect `Retry-After`.

---

## Troubleshooting

| Symptom                                   | Fix                                                            |
| ----------------------------------------- | ------------------------------------------------------------- |
| `401 Invalid or expired token`            | Re-sign in; confirm `SUPABASE_JWT_SECRET` matches the project |
| `Server auth is not configured`           | Set `SUPABASE_JWT_SECRET` in `backend/.env`                   |
| `No Shopify store credentials configured` | Set `SHOPIFY_STORE_URL` + `SHOPIFY_ACCESS_TOKEN`              |
| `Shopify authentication failed`           | Check the access token scopes (`read_orders`, `read_products`)|
| CORS errors in browser console            | Add the frontend origin to `CORS_ORIGINS`                     |
| History tab empty                         | Confirm `schema.sql` ran and RLS policies exist               |
