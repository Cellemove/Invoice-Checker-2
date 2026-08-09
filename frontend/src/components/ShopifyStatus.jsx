import { useCallback, useEffect, useState } from 'react'
import { getShopifyStatus, connectShopify } from '../lib/api'
import styles from './ShopifyStatus.module.css'

function fmtExpiry(epochSeconds) {
  if (!epochSeconds) return null
  const d = new Date(epochSeconds * 1000)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString()
}

const SOURCE_LABEL = {
  env_access_token: 'static token (.env)',
  client_credentials: 'client credentials',
  client_credentials_available: 'client credentials',
  none: 'not configured',
}

/**
 * Shows whether the backend holds a usable Shopify token for the selected store
 * and lets the user fetch/refresh one via the client credentials grant.
 */
export default function ShopifyStatus({ store }) {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    if (!store) return
    setLoading(true)
    setError('')
    try {
      setStatus(await getShopifyStatus(store))
    } catch (err) {
      setError(err.message || 'Could not read Shopify status.')
      setStatus(null)
    } finally {
      setLoading(false)
    }
  }, [store])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function handleConnect(force) {
    setWorking(true)
    setError('')
    try {
      setStatus(await connectShopify(store, force))
    } catch (err) {
      setError(err.message || 'Failed to connect to Shopify.')
    } finally {
      setWorking(false)
    }
  }

  const connected = status?.connected
  const isEnvToken = status?.source === 'env_access_token'
  const canConnect = status?.can_connect

  const wrapClass = [
    styles.wrap,
    connected ? styles.connected : styles.disconnected,
  ].join(' ')

  return (
    <div className={wrapClass}>
      <div className={styles.left}>
        <span
          className={`${styles.dot} ${connected ? styles.dotOk : styles.dotBad}`}
          aria-hidden="true"
        />
        <div className={styles.text}>
          <span className={styles.title}>
            Shopify · {store}
            {connected ? ' connected' : ' not connected'}
          </span>
          <span className={styles.detail}>
            {loading
              ? 'Checking…'
              : status
                ? `via ${SOURCE_LABEL[status.source] || status.source}` +
                  (status.scope ? ` · scopes: ${status.scope}` : '') +
                  (connected && status.expires_at
                    ? ` · expires ${fmtExpiry(status.expires_at)}`
                    : '') +
                  (status.detail ? ` · ${status.detail}` : '')
                : ''}
          </span>
        </div>
      </div>

      <div className={styles.actions}>
        {/* Static .env tokens need no fetch; only client-credential stores do. */}
        {canConnect && !isEnvToken && (
          <button
            className="btn small"
            onClick={() => handleConnect(!connected ? false : true)}
            disabled={working}
            type="button"
          >
            {working
              ? 'Working…'
              : connected
                ? '↻ Refresh token'
                : '🔌 Connect Shopify'}
          </button>
        )}
        <button
          className="btn ghost small"
          onClick={refresh}
          disabled={loading || working}
          type="button"
        >
          Check
        </button>
      </div>

      {error && <div className={`alert error ${styles.error}`}>{error}</div>}
    </div>
  )
}
