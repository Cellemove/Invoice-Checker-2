import { useCallback, useEffect, useState } from 'react'
import { uploadCheckInvoice, listStores, checkDriveInvoice } from './lib/api'

// Shown in the footer; bump alongside payload-limit fixes so a stale deployed
// bundle is immediately recognisable.
const BUILD = '2026-07-18.2'
import FileUpload from './components/FileUpload'
import DrivePanel from './components/DrivePanel'
import ShopifyStatus from './components/ShopifyStatus'
import Summary from './components/Summary'
import ResultsTable from './components/ResultsTable'
import History from './components/History'
import styles from './App.module.css'

// NOTE: This is an internal tool — authentication is intentionally disconnected.
// The Supabase login flow (./components/Auth.jsx + ./lib/supabaseClient.js) is
// kept in the codebase but unused. To re-enable it, gate the return below on a
// Supabase session and set AUTH_REQUIRED=true on the backend.

export default function App() {
  // Workflow state
  const [stores, setStores] = useState([])
  const [store, setStore] = useState('auto')
  const [parsed, setParsed] = useState(null)
  const [result, setResult] = useState(null)

  const [checkPrices, setCheckPrices] = useState(false) // match orders/SKUs only by default
  const [source, setSource] = useState('drive') // drive | upload — invoice input source

  // Selected quotation ({id, name} of the Drive file). Sent with every compare
  // so the backend stays stateless; persisted so the choice survives reloads.
  const [quotation, setQuotationState] = useState(() => {
    try {
      const raw = localStorage.getItem('active_quotation')
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  })
  const setQuotation = useCallback((q) => {
    setQuotationState(q)
    try {
      localStorage.setItem('active_quotation', JSON.stringify(q))
    } catch {
      /* storage unavailable — selection just won't persist */
    }
  }, [])
  const [stage, setStage] = useState('idle') // idle | uploading | comparing
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [historyKey, setHistoryKey] = useState(0)
  const [tab, setTab] = useState('check') // check | history

  // Theme (light/dark) — initial value is applied pre-paint in index.html.
  const [theme, setTheme] = useState(() =>
    document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
  )
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('theme', theme)
    } catch {
      /* storage unavailable — theme just won't persist */
    }
  }, [theme])

  // Load configured Shopify stores on mount.
  useEffect(() => {
    listStores()
      .then((res) => {
        const keys = res.stores && res.stores.length ? res.stores : ['default']
        setStores(keys)
        // Keep 'auto' as the default so each line routes to its own store.
      })
      .catch(() => setStores(['default']))
  }, [])

  // Shared: surface a finished check's summary + invoice metadata.
  function showCheckResult(inv, cmp) {
    setResult(cmp)
    setHistoryKey((k) => k + 1)
    const sheetNote =
      inv.sheets_used && inv.sheets_used.length > 1
        ? ` (${inv.sheets_used.length} sheets combined)`
        : ''
    setNotice(
      `Checked ${inv.filename} — ${inv.row_count} row(s)${sheetNote}: ` +
        `${cmp.summary.matched_count} matched, ` +
        `${cmp.summary.mismatch_count} price mismatch(es), ` +
        `${cmp.summary.missing_count} without a quotation.`,
    )
  }

  // Local file: uploaded, parsed AND reconciled server-side in one request —
  // parsed line items never travel back through the browser (serverless-safe).
  async function handleFileSelected(file) {
    setError('')
    setNotice(`Checking "${file.name}"…`)
    setResult(null)
    setParsed(null)
    setStage('uploading')
    try {
      const res = await uploadCheckInvoice(file, quotation?.id)
      const inv = res.invoice || {}
      setParsed({ ...inv, manual_file: file, line_items: null })
      showCheckResult(inv, res.result)
    } catch (err) {
      setError(err.message || 'Something went wrong.')
    } finally {
      setStage('idle')
    }
  }

  // Drive file: parsed AND reconciled server-side in a single request, so
  // line items never cross the wire (Vercel caps request bodies at 4.5 MB).
  async function checkDriveFile(file) {
    setError('')
    setNotice(`Checking "${file.name}"…`)
    setResult(null)
    setParsed(null)
    setStage('comparing')
    try {
      const res = await checkDriveInvoice(file.id, quotation?.id)
      const inv = res.invoice || {}
      setParsed({ ...inv, drive_file_id: file.id, line_items: null })
      showCheckResult(inv, res.result)
    } catch (err) {
      setError(err.message || 'Something went wrong.')
    } finally {
      setStage('idle')
    }
  }

  async function rerun() {
    if (!parsed) return
    if (parsed.drive_file_id) {
      await checkDriveFile({ id: parsed.drive_file_id, name: parsed.filename })
    } else if (parsed.manual_file) {
      await handleFileSelected(parsed.manual_file)
    }
  }

  const busy = stage !== 'idle'

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <span className={styles.brandMark} aria-hidden="true">
            📋
          </span>
          <span className={styles.brandName}>Invoice Checker</span>
        </div>

        <nav className={styles.nav} aria-label="Main">
          <button
            className={
              tab === 'check'
                ? `${styles.navlink} ${styles.navlinkActive}`
                : styles.navlink
            }
            onClick={() => setTab('check')}
            type="button"
          >
            Check
          </button>
          <button
            className={
              tab === 'history'
                ? `${styles.navlink} ${styles.navlinkActive}`
                : styles.navlink
            }
            onClick={() => setTab('history')}
            type="button"
          >
            History
          </button>
        </nav>

        <div className={styles.userBox}>
          <span className={styles.envChip}>Internal tool</span>
          <button
            className={styles.themeToggle}
            onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
            type="button"
            aria-pressed={theme === 'light'}
            aria-label={
              theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
            }
            title={
              theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
            }
          >
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
        </div>
      </header>

      <main className={styles.container}>
        {error && <div className="alert error">{error}</div>}
        {notice && !error && <div className="alert success">{notice}</div>}

        {tab === 'check' && (
          <>
            <div
              className={styles.sourceSwitch}
              role="tablist"
              aria-label="Invoice source"
            >
              <button
                className={
                  source === 'drive'
                    ? `${styles.sourceBtn} ${styles.sourceBtnActive}`
                    : styles.sourceBtn
                }
                onClick={() => setSource('drive')}
                type="button"
                role="tab"
                aria-selected={source === 'drive'}
                disabled={busy}
              >
                📁 Google Drive
              </button>
              <button
                className={
                  source === 'upload'
                    ? `${styles.sourceBtn} ${styles.sourceBtnActive}`
                    : styles.sourceBtn
                }
                onClick={() => setSource('upload')}
                type="button"
                role="tab"
                aria-selected={source === 'upload'}
                disabled={busy}
              >
                ⬆️ Manual upload
              </button>
            </div>

            {source === 'drive' && (
              <DrivePanel
                onCheckInvoice={checkDriveFile}
                disabled={busy}
                quotation={quotation}
                onQuotationChosen={setQuotation}
              />
            )}

            {source === 'upload' && (
              <>
                {store && store !== 'auto' && (
                  <ShopifyStatus key={store} store={store} />
                )}

                <FileUpload
                  onFileSelected={handleFileSelected}
                  disabled={busy}
                  stores={stores}
                  store={store}
                  onStoreChange={setStore}
                  checkPrices={checkPrices}
                  onCheckPricesChange={setCheckPrices}
                />
              </>
            )}

            {busy && (
              <div className={`card ${styles.progressCard}`} role="status">
                <div className="spinner" aria-hidden="true" />
                <span>
                  {stage === 'uploading'
                    ? 'Uploading and parsing invoice…'
                    : 'Comparing against the quotation price list…'}
                </span>
              </div>
            )}

            {result && !busy && (
              <div className={`row between wrap ${styles.rerunRow}`}>
                <span className="muted small">
                  Showing results for <strong>{parsed?.filename}</strong>
                </span>
                <button className="btn ghost small" onClick={rerun} type="button">
                  ↻ Re-run comparison
                </button>
              </div>
            )}

            {result && <Summary summary={result.summary} store={result.store} />}
            {result && <ResultsTable rows={result.rows} />}
          </>
        )}

        {tab === 'history' && <History refreshKey={historyKey} />}
      </main>

      <footer className={styles.footer}>
        <span className="muted small">
          Invoice Checker · invoices reconciled against the quotation price list
          · build {BUILD}
        </span>
      </footer>
    </div>
  )
}
