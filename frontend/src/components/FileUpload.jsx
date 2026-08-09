import { useRef, useState } from 'react'
import styles from './FileUpload.module.css'

const ACCEPT = '.csv,.xlsx,.xlsm'
const MAX_MB = 10

export default function FileUpload({
  onFileSelected,
  disabled,
  stores,
  store,
  onStoreChange,
  checkPrices,
  onCheckPricesChange,
}) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)
  const [localError, setLocalError] = useState('')
  const [fileName, setFileName] = useState('')

  function validateAndSend(file) {
    setLocalError('')
    if (!file) return
    const lower = file.name.toLowerCase()
    if (!/\.(csv|xlsx|xlsm)$/.test(lower)) {
      setLocalError('Please choose a .csv or .xlsx file.')
      return
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      setLocalError(`File is larger than ${MAX_MB} MB.`)
      return
    }
    setFileName(file.name)
    onFileSelected(file)
  }

  function handleDrop(e) {
    e.preventDefault()
    setDragging(false)
    if (disabled) return
    const file = e.dataTransfer.files?.[0]
    validateAndSend(file)
  }

  function handleKeyDown(e) {
    if (disabled) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      inputRef.current?.click()
    }
  }

  const dropzoneClass = [
    styles.dropzone,
    dragging ? styles.dragging : '',
    disabled ? styles.disabled : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className="card">
      <div className={styles.head}>
        <h2>Upload invoice</h2>
        <div className={styles.controls}>
          <label className={styles.selectLabel}>
            Reconcile
            <select
              value={checkPrices ? 'full' : 'match'}
              onChange={(e) => onCheckPricesChange(e.target.value === 'full')}
              disabled={disabled}
              title="Match-only ignores price differences (use when the invoice amount is a cost, not the sale price)"
            >
              <option value="match">Orders &amp; SKUs only</option>
              <option value="full">Full (incl. prices)</option>
            </select>
          </label>
          <label className={styles.selectLabel}>
            Shopify store
            <select
              value={store}
              onChange={(e) => onStoreChange(e.target.value)}
              disabled={disabled}
              title="Auto routes each line to the store matching its region"
            >
              <option value="auto">Auto — route by region</option>
              {(stores || []).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div
        className={dropzoneClass}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={handleKeyDown}
        role="button"
        tabIndex={0}
        aria-disabled={disabled || undefined}
        aria-label="Upload an invoice file"
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          hidden
          disabled={disabled}
          onChange={(e) => validateAndSend(e.target.files?.[0])}
        />
        <div className={styles.icon} aria-hidden="true">
          ⬆️
        </div>
        <p className={styles.title}>
          <strong>Click to browse</strong> or drag &amp; drop
        </p>
        <p className={styles.hint}>.csv or .xlsx · up to {MAX_MB} MB</p>
        {fileName && <span className={styles.filePill}>📄 {fileName}</span>}
      </div>

      {localError && (
        <div className={`alert error ${styles.alertGap}`}>{localError}</div>
      )}
    </div>
  )
}
