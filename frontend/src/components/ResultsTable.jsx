import { useMemo, useState } from 'react'
import styles from './ResultsTable.module.css'

const STATUS_META = {
  matched: { label: 'Matched', cls: 'badge ok' },
  'price mismatch': { label: 'Price mismatch', cls: 'badge warn' },
  'quantity mismatch': { label: 'Qty mismatch', cls: 'badge warn' },
  'not billed': { label: 'Not billed', cls: 'badge' },
  'no quotation': { label: 'No quotation', cls: 'badge bad' },
  'missing in Shopify': { label: 'Missing in Shopify', cls: 'badge bad' },
  'missing in invoice': { label: 'Missing in invoice', cls: 'badge bad' },
}

function money(n) {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
  })
}

function num(n) {
  return n === null || n === undefined ? '—' : n
}

const FILTERS = [
  ['all', 'All'],
  ['matched', 'Matched'],
  ['mismatch', 'Price mismatch'],
  ['notbilled', 'Not billed'],
  ['noquote', 'No quotation'],
]

export default function ResultsTable({ rows }) {
  const [filter, setFilter] = useState('all')

  const filtered = useMemo(() => {
    if (filter === 'all') return rows
    if (filter === 'matched') return rows.filter((r) => r.status === 'matched')
    if (filter === 'mismatch')
      return rows.filter((r) => r.status.includes('mismatch'))
    if (filter === 'notbilled')
      return rows.filter((r) => r.status === 'not billed')
    if (filter === 'noquote')
      return rows.filter((r) => r.status === 'no quotation')
    return rows
  }, [rows, filter])

  if (!rows || rows.length === 0) return null

  return (
    <div className="card">
      <div className={styles.head}>
        <h2>Comparison results</h2>
        <div className={styles.headRight}>
          <span className={styles.count}>
            {filtered.length} of {rows.length} orders
          </span>
          <div className="filter-tabs">
            {FILTERS.map(([key, label]) => (
              <button
                key={key}
                className={filter === key ? 'chip active' : 'chip'}
                onClick={() => setFilter(key)}
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="table-scroll">
        <table className="results">
          <thead>
            <tr>
              <th>Status</th>
              <th>Order</th>
              <th>Country</th>
              <th>Customer</th>
              <th>Address</th>
              <th>Products</th>
              <th className="num">Qty</th>
              <th className="num">Price on invoice</th>
              <th className="num">Price it should be</th>
              <th className="num">Variance</th>
              <th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => {
              const meta = STATUS_META[r.status] || { label: r.status, cls: 'badge' }
              const rowCls =
                r.status === 'matched' || r.status === 'not billed'
                  ? ''
                  : r.status === 'no quotation' || r.status.includes('missing')
                    ? 'row-bad'
                    : 'row-warn'
              return (
                <tr key={i} className={rowCls}>
                  <td>
                    <span className={meta.cls}>{meta.label}</span>
                  </td>
                  <td>{r.order_id || '—'}</td>
                  <td>{r.country || '—'}</td>
                  <td className="wrapcell">{r.customer_name || '—'}</td>
                  <td className="wrapcell">{r.address || '—'}</td>
                  <td className="wrapcell products">{r.products || '—'}</td>
                  <td className="num">{num(r.invoice_quantity)}</td>
                  <td className="num">{money(r.invoice_line_total)}</td>
                  <td className="num">{money(r.expected_price)}</td>
                  <td
                    className={`num ${
                      r.variance && Math.abs(r.variance) > 0.001 ? 'bad' : ''
                    }`}
                  >
                    {money(r.variance)}
                  </td>
                  <td className="notes">{r.notes || ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {filtered.length === 0 && (
        <p className={styles.empty}>No rows match this filter.</p>
      )}
    </div>
  )
}
