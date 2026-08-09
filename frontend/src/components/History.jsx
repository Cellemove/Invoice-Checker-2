import { useCallback, useEffect, useState } from 'react'
import { fetchHistory } from '../lib/api'
import styles from './History.module.css'

function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export default function History({ refreshKey }) {
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // History is read from the backend /history endpoint (service-role backed),
  // so it works without an authenticated session.
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await fetchHistory(50)
      setRuns(Array.isArray(data) ? data : [])
    } catch (err) {
      setError(err.message || 'Failed to load history.')
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  return (
    <div className="card">
      <div className={styles.head}>
        <h2>Comparison history</h2>
        <button className="btn ghost small" onClick={load} type="button">
          ↻ Refresh
        </button>
      </div>

      {loading && <p className="muted small">Loading history…</p>}
      {error && <div className={`alert error ${styles.alertGap}`}>{error}</div>}
      {!loading && !error && runs.length === 0 && (
        <p className={styles.empty}>No comparison runs yet.</p>
      )}

      {runs.length > 0 && (
        <div className="table-scroll">
          <table className="results">
            <thead>
              <tr>
                <th>Date</th>
                <th>File</th>
                <th className="num">Matched</th>
                <th className="num">Mismatches</th>
                <th className="num">Missing</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>{fmtDate(r.run_at)}</td>
                  <td className="wrapcell">{r.filename}</td>
                  <td className="num ok">{r.matched_count}</td>
                  <td className="num warn-text">{r.mismatch_count}</td>
                  <td className="num bad">{r.missing_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
