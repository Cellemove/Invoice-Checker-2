import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getDriveStatus, listDriveFiles, setDriveQuotation } from '../lib/api'
import styles from './DrivePanel.module.css'

const MONTHS = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
}

function isQuotation(name) {
  return /quotation|price\s*list|\bquote\b/i.test(name || '')
}

// Orders = the invoice files, i.e. the "trackings&costs" spreadsheets.
function isOrderFile(name) {
  return /tracking|cost/i.test(name || '')
}

/** Extract a comparable "month.day" value from a filename, e.g. "July.9" -> 709. */
function nameDateValue(name) {
  const m = /(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.\s\-_]*?(\d{1,2})/i.exec(
    name || '',
  )
  if (!m) return null
  const mon = MONTHS[m[1].slice(0, 3).toLowerCase()]
  const day = parseInt(m[2], 10)
  return mon && day ? mon * 100 + day : null
}

/**
 * Full date stamp for a quotation, derived from the DATE IN ITS FILENAME
 * (the rate period — what "latest" actually means), with the year inferred
 * from the file's own modified time. A named date more than ~45 days ahead
 * of the file's timestamp must belong to the previous year (e.g. a "Dec.29"
 * file seen in June), which keeps year-end files from outranking summer ones.
 */
function quoteDateStamp(f) {
  const nv = nameDateValue(f.name)
  if (nv == null) return null
  const mon = Math.floor(nv / 100)
  const day = nv % 100
  const ref = new Date(f.modifiedTime || Date.now())
  let d = new Date(ref.getFullYear(), mon - 1, day)
  if (d.getTime() - ref.getTime() > 45 * 86400000) {
    d = new Date(ref.getFullYear() - 1, mon - 1, day)
  }
  return d.getTime()
}

// "Latest" ranks by the filename's rate date first; the Drive modified time
// only breaks ties (editing an old file must NOT promote it to "latest").
function byLatest(a, b) {
  const da = quoteDateStamp(a)
  const db = quoteDateStamp(b)
  if (da != null && db != null && db !== da) return db - da
  if ((da == null) !== (db == null)) return da == null ? 1 : -1
  return new Date(b.modifiedTime || 0) - new Date(a.modifiedTime || 0)
}

function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString()
}

/**
 * Reads invoices and quotations from a shared Google Drive folder, split into
 * two columns: Orders (invoices to check) and Quotations (price references).
 * The latest-dated quotation is applied as the default price reference.
 */
export default function DrivePanel({
  onCheckInvoice,
  disabled,
  quotation,
  onQuotationChosen,
}) {
  const [status, setStatus] = useState(null)
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [orderFilter, setOrderFilter] = useState('')
  const [quoteFilter, setQuoteFilter] = useState('')

  // Read through a ref inside load() so the callback stays stable and a
  // quotation change doesn't retrigger a full Drive reload.
  const quotationRef = useRef(quotation)
  useEffect(() => {
    quotationRef.current = quotation
  }, [quotation])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const st = await getDriveStatus()
      if (!st.configured) {
        setStatus(st)
        setFiles([])
        return
      }
      const { files: all } = await listDriveFiles()
      const quotes = all.filter((f) => isQuotation(f.name)).sort(byLatest)

      // Default the price reference to the latest quotation. Auto-chosen
      // selections follow the latest as new files appear; only an explicit
      // manual choice (the "Use" button) is left alone.
      const current = quotationRef.current
      const latest = quotes[0]
      const currentInList = current && quotes.some((q) => q.id === current.id)
      const needsDefault =
        quotes.length > 0 &&
        (!currentInList || (!current.manual && current.id !== latest.id))
      if (needsDefault) {
        try {
          const res = await setDriveQuotation(latest.id)
          onQuotationChosen({
            id: latest.id,
            name: res.quotation || latest.name,
            manual: false,
          })
          setNotice(`Default quotation: ${latest.name} (latest).`)
        } catch {
          /* the latest didn't parse — leave the previous quotation active */
        }
      }
      setStatus(st)
      setFiles(all)
    } catch (err) {
      setError(err.message || 'Could not reach Google Drive.')
    } finally {
      setLoading(false)
    }
  }, [onQuotationChosen])

  useEffect(() => {
    load()
  }, [load])

  const { orders, quotations } = useMemo(() => {
    const q = files.filter((f) => isQuotation(f.name)).sort(byLatest)
    const o = files
      .filter((f) => isOrderFile(f.name))
      .sort((a, b) => new Date(b.modifiedTime || 0) - new Date(a.modifiedTime || 0))
    return { orders: o, quotations: q }
  }, [files])

  const activeQuoteId = quotation?.id
  const activeQuoteName = quotation?.name || status?.active_quotation_source
  const shownOrders = orders.filter((f) =>
    f.name.toLowerCase().includes(orderFilter.toLowerCase()),
  )
  const shownQuotes = quotations.filter((f) =>
    f.name.toLowerCase().includes(quoteFilter.toLowerCase()),
  )

  function checkInvoice(f) {
    // Parsing + reconciliation happen server-side in one request; the parent
    // owns the progress/result state.
    setError('')
    setNotice('')
    onCheckInvoice(f)
  }

  async function makeQuotation(f) {
    setBusyId(f.id)
    setError('')
    setNotice('')
    try {
      const res = await setDriveQuotation(f.id)
      onQuotationChosen({ id: f.id, name: res.quotation || f.name, manual: true })
      setNotice(`Quotation set to "${res.quotation}" (${res.sku_count} SKUs).`)
    } catch (err) {
      setError(err.message || 'Failed to set quotation.')
    } finally {
      setBusyId('')
    }
  }

  return (
    <div className="card">
      <div className={styles.head}>
        <h2>📁 Google Drive</h2>
        <button
          className="btn ghost small"
          onClick={load}
          disabled={loading}
          type="button"
        >
          ↻ Refresh
        </button>
      </div>

      {loading && <p className="muted small">Loading Google Drive files…</p>}

      {!loading && status && !status.configured && (
        <div className={`alert warn ${styles.alertGap}`}>
          Google Drive isn’t connected. Add a service-account key on the backend
          and share your Drive folder with it, then Refresh.
        </div>
      )}

      {error && <div className={`alert error ${styles.alertGap}`}>{error}</div>}
      {notice && !error && (
        <div className={`alert success ${styles.alertGap}`}>{notice}</div>
      )}

      {status && status.configured && !loading && (
        <div className={styles.cols}>
          {/* ── Orders ─────────────────────────────── */}
          <section className={styles.col} aria-label="Order files">
            <div className={styles.colHead}>
              <h3 className={styles.colTitle}>🧾 Orders</h3>
              <span className={styles.count}>{orders.length} files</span>
            </div>
            <input
              className={styles.filter}
              placeholder="Filter orders…"
              value={orderFilter}
              onChange={(e) => setOrderFilter(e.target.value)}
              aria-label="Filter order files"
            />
            <div className={styles.list}>
              {shownOrders.map((f) => (
                <div key={f.id} className={styles.item}>
                  <div className={styles.name}>
                    <span>{f.name}</span>
                    <span className={styles.meta}>{fmtDate(f.modifiedTime)}</span>
                  </div>
                  <div className={styles.actions}>
                    <button
                      className="btn small"
                      disabled={disabled || busyId === f.id}
                      onClick={() => checkInvoice(f)}
                      type="button"
                    >
                      {busyId === f.id ? '…' : 'Check invoice'}
                    </button>
                  </div>
                </div>
              ))}
              {shownOrders.length === 0 && (
                <p className={styles.empty}>No matching order files.</p>
              )}
            </div>
          </section>

          {/* ── Quotations ─────────────────────────── */}
          <section className={styles.col} aria-label="Quotation files">
            <div className={styles.colHead}>
              <h3 className={styles.colTitle}>💲 Quotations</h3>
              <span className={styles.count}>{quotations.length} files</span>
            </div>
            <input
              className={styles.filter}
              placeholder="Filter quotations…"
              value={quoteFilter}
              onChange={(e) => setQuoteFilter(e.target.value)}
              aria-label="Filter quotation files"
            />
            <div className={styles.list}>
              {shownQuotes.map((f) => {
                const active = activeQuoteId
                  ? f.id === activeQuoteId
                  : activeQuoteName === f.name
                return (
                  <div
                    key={f.id}
                    className={
                      active ? `${styles.item} ${styles.itemActive}` : styles.item
                    }
                  >
                    <div className={styles.name}>
                      <span>
                        {active ? '✅ ' : ''}
                        {f.name}
                      </span>
                      <span className={styles.meta}>{fmtDate(f.modifiedTime)}</span>
                    </div>
                    <div className={styles.actions}>
                      {active ? (
                        <span className="badge ok">Active</span>
                      ) : (
                        <button
                          className="btn ghost small"
                          disabled={disabled || busyId === f.id}
                          onClick={() => makeQuotation(f)}
                          type="button"
                        >
                          {busyId === f.id ? '…' : 'Use'}
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
              {shownQuotes.length === 0 && (
                <p className={styles.empty}>No matching quotation files.</p>
              )}
            </div>
          </section>
        </div>
      )}

      {status && status.configured && !loading && (
        <p className={styles.statusLine}>
          Active quotation:{' '}
          <strong>
            {activeQuoteName || status.active_quotation || 'none'}
          </strong>
          {status.service_account_email
            ? ` · service account: ${status.service_account_email}`
            : ''}
        </p>
      )}
    </div>
  )
}
