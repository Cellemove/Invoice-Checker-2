import styles from './Summary.module.css'

function money(n) {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
  })
}

export default function Summary({ summary, store }) {
  if (!summary) return null
  const {
    total_rows,
    matched_count,
    mismatch_count,
    missing_count,
    sku_issues,
    invoice_total,
    shopify_total,
    expected_total,
    total_variance,
    orders_checked,
    orders_not_found,
    stores_used,
    unrouted_regions,
    skipped_no_order,
    not_billed_count,
    unpriced_invoice_total,
  } = summary

  return (
    <div className="card">
      <div className={styles.head}>
        <h2>Summary</h2>
        <span className={styles.refChip}>Reference · {store}</span>
      </div>

      <div className={styles.statGrid}>
        <Stat label="Orders" value={total_rows} />
        <Stat label="Matched" value={matched_count} tone="ok" />
        <Stat label="Price mismatch" value={mismatch_count} tone="warn" />
        <Stat label="Not billed" value={not_billed_count || 0} />
        <Stat label="No quotation" value={missing_count} tone="bad" />
      </div>

      <div className={styles.totals}>
        <div className={styles.totalBox}>
          <span className={styles.totalLabel}>Price on invoice (total)</span>
          <span className={styles.totalValue}>{money(invoice_total)}</span>
        </div>
        <div className={styles.totalBox}>
          <span className={styles.totalLabel}>Price it should be (total)</span>
          <span className={styles.totalValue}>{money(expected_total)}</span>
        </div>
        <div className={styles.totalBox}>
          <span className={styles.totalLabel}>
            Total variance{unpriced_invoice_total > 0 ? ' (priced orders)' : ''}
          </span>
          <span
            className={`${styles.totalValue} ${
              total_variance === 0 ? 'ok' : 'bad'
            }`}
          >
            {money(total_variance)}
          </span>
        </div>
      </div>

      {unpriced_invoice_total > 0 && (
        <p className={`muted small ${styles.totalsNote}`}>
          ⓘ {missing_count} order(s) with no quotation carry{' '}
          <strong>{money(unpriced_invoice_total)}</strong> on the invoice — that
          amount is included in “Price on invoice” but excluded from “Price it
          should be” and the variance.
        </p>
      )}

      <div className={styles.metaRow}>
        {stores_used && stores_used.length > 0 && (
          <p className="muted small">Stores matched: {stores_used.join(', ')}</p>
        )}
        {skipped_no_order > 0 && (
          <p className="muted small">
            Skipped {skipped_no_order} line(s) with no order number.
          </p>
        )}
        {unrouted_regions && unrouted_regions.length > 0 && (
          <div className="alert warn">
            No Shopify store configured for region(s): {unrouted_regions.join(', ')}.
            Those lines are shown as “missing in Shopify”.
          </div>
        )}
        {orders_not_found && orders_not_found.length > 0 && (
          <div className="alert warn">
            Orders not found in Shopify: {orders_not_found.length} (e.g.{' '}
            {orders_not_found.slice(0, 10).join(', ')}
            {orders_not_found.length > 10 ? '…' : ''})
          </div>
        )}
        {orders_checked && orders_checked.length > 0 && (
          <p className="muted small">
            Orders checked: {orders_checked.length}
          </p>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, tone }) {
  const toneClass = tone ? styles[`tone-${tone}`] : ''
  return (
    <div className={`${styles.stat} ${toneClass}`.trim()}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  )
}
