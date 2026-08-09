import { supabase } from './supabaseClient'

// In production the app is served behind Vercel rewrites where the backend
// lives under /api on the same origin; in dev it runs on localhost:8000.
const API_BASE =
  import.meta.env.VITE_API_BASE_URL ||
  (import.meta.env.PROD ? '/api' : 'http://localhost:8000')

/**
 * Auth is disconnected for this internal tool, so a token is optional.
 * If a Supabase session happens to exist (the dormant login flow is still in
 * the codebase), we forward its token; otherwise we send no Authorization
 * header and the backend attributes the request to its internal user.
 */
async function authHeader() {
  try {
    const { data } = await supabase.auth.getSession()
    const token = data?.session?.access_token
    return token ? { Authorization: `Bearer ${token}` } : {}
  } catch {
    return {}
  }
}

/** Parse a JSON response, raising a useful Error on non-2xx. */
async function handle(res) {
  let body = null
  const text = await res.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = { detail: text }
    }
  }
  if (!res.ok) {
    const detail =
      (body && (body.detail || body.message)) || `Request failed (${res.status})`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body
}

/** POST /upload — multipart file upload, returns parsed invoice. */
export async function uploadInvoice(file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${API_BASE}/upload`, {
    method: 'POST',
    headers: { ...(await authHeader()) },
    body: form,
  })
  return handle(res)
}

/**
 * POST /upload-check — upload, parse AND reconcile in one request. The
 * request carries only the file itself and the response only per-order
 * results, keeping both sides under serverless payload caps.
 */
export async function uploadCheckInvoice(file, quotationFileId) {
  const params = new URLSearchParams()
  if (quotationFileId) params.set('quotation_file_id', quotationFileId)
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${API_BASE}/upload-check?${params.toString()}`, {
    method: 'POST',
    headers: { ...(await authHeader()) },
    body: form,
  })
  return handle(res)
}

/** POST /compare — run reconciliation against the quotation / Shopify. */
export async function runComparison(
  parsedInvoice,
  store,
  checkPrices = true,
  quotationFileId = null,
) {
  const res = await fetch(`${API_BASE}/compare`, {
    method: 'POST',
    headers: {
      ...(await authHeader()),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      filename: parsedInvoice.filename,
      line_items: parsedInvoice.line_items,
      invoice_total: parsedInvoice.invoice_total,
      store: store || null,
      check_prices: checkPrices,
      quotation_file_id: quotationFileId || null,
      persist: true,
    }),
  })
  return handle(res)
}

/** GET /shopify/stores — list configured store keys. */
export async function listStores() {
  const res = await fetch(`${API_BASE}/shopify/stores`, {
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** GET /shopify/auth/status — connection state for a store (no token exposed). */
export async function getShopifyStatus(store) {
  const qs = store ? `?store=${encodeURIComponent(store)}` : ''
  const res = await fetch(`${API_BASE}/shopify/auth/status${qs}`, {
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** POST /shopify/auth/connect — fetch/refresh a token via client credentials. */
export async function connectShopify(store, force = false) {
  const params = new URLSearchParams()
  if (store) params.set('store', store)
  if (force) params.set('force', 'true')
  const res = await fetch(`${API_BASE}/shopify/auth/connect?${params.toString()}`, {
    method: 'POST',
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** GET /drive/status — whether Drive is configured + active quotation. */
export async function getDriveStatus() {
  const res = await fetch(`${API_BASE}/drive/status`, {
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** GET /drive/files — list spreadsheet/CSV files from Drive. */
export async function listDriveFiles(folder) {
  const qs = folder ? `?folder=${encodeURIComponent(folder)}` : ''
  const res = await fetch(`${API_BASE}/drive/files${qs}`, {
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** POST /drive/import-invoice — download+parse an invoice from Drive. */
export async function importDriveInvoice(fileId) {
  const res = await fetch(
    `${API_BASE}/drive/import-invoice?file_id=${encodeURIComponent(fileId)}`,
    { method: 'POST', headers: { ...(await authHeader()) } },
  )
  return handle(res)
}

/**
 * POST /drive/check — download, parse and reconcile a Drive invoice in one
 * request (line items never reach the browser; serverless-payload safe).
 */
export async function checkDriveInvoice(fileId, quotationFileId) {
  const params = new URLSearchParams({ file_id: fileId })
  if (quotationFileId) params.set('quotation_file_id', quotationFileId)
  const res = await fetch(`${API_BASE}/drive/check?${params.toString()}`, {
    method: 'POST',
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}

/** POST /drive/set-quotation — set the active quotation from a Drive file. */
export async function setDriveQuotation(fileId) {
  const res = await fetch(
    `${API_BASE}/drive/set-quotation?file_id=${encodeURIComponent(fileId)}`,
    { method: 'POST', headers: { ...(await authHeader()) } },
  )
  return handle(res)
}

/** GET /history — backend-recorded comparison runs. */
export async function fetchHistory(limit = 50) {
  const res = await fetch(`${API_BASE}/history?limit=${limit}`, {
    headers: { ...(await authHeader()) },
  })
  return handle(res)
}
