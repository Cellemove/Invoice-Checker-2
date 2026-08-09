import { useState } from 'react'
import { supabase } from '../lib/supabaseClient'
import styles from './Auth.module.css'

export default function Auth() {
  const [mode, setMode] = useState('signin') // 'signin' | 'signup'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    setLoading(true)
    try {
      if (mode === 'signin') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
      } else {
        const { data, error } = await supabase.auth.signUp({ email, password })
        if (error) throw error
        if (!data.session) {
          setMessage('Check your email to confirm your account, then sign in.')
          setMode('signin')
        }
      }
    } catch (err) {
      setError(err.message || 'Authentication failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={`card ${styles.card}`}>
        <h1 className={styles.brand}>📋 Invoice Checker</h1>
        <p className={styles.sub}>Reconcile invoices against live Shopify orders.</p>

        <div className={styles.tabs} role="tablist" aria-label="Authentication mode">
          <button
            className={
              mode === 'signin' ? `${styles.tab} ${styles.tabActive}` : styles.tab
            }
            onClick={() => setMode('signin')}
            type="button"
            role="tab"
            aria-selected={mode === 'signin'}
          >
            Sign in
          </button>
          <button
            className={
              mode === 'signup' ? `${styles.tab} ${styles.tabActive}` : styles.tab
            }
            onClick={() => setMode('signup')}
            type="button"
            role="tab"
            aria-selected={mode === 'signup'}
          >
            Sign up
          </button>
        </div>

        <form onSubmit={handleSubmit} className={styles.form}>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
              required
              minLength={6}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
            />
          </label>

          {error && <div className="alert error">{error}</div>}
          {message && <div className="alert success">{message}</div>}

          <button
            className={`btn primary ${styles.submit}`}
            disabled={loading}
            type="submit"
          >
            {loading
              ? 'Please wait…'
              : mode === 'signin'
                ? 'Sign in'
                : 'Create account'}
          </button>
        </form>
      </div>
    </div>
  )
}
