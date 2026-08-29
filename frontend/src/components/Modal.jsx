import { useEffect } from 'react'
import { useDismiss } from '../useDismiss.js'
import Button from './Button.jsx'

// The scrim + glass box + head/body/foot recipe that ask.jsx, ComputerUse's
// Setup dialog and SecurityBoard each spell out for themselves. Same values,
// one implementation, and the two behaviours those three had to get right
// individually — Escape and click-away — come from the existing useDismiss
// hook rather than a fourth hand-rolled listener.
//
// Deliberately kept from the existing dialogs:
//  - the scrim sits at z-index 4000, above .notices (3000). A toast must never
//    be clickable through an open dialog: tapping one navigates away
//    mid-question.
//  - the body is the ONLY scroll container.
//  - the modal is a <form>, so Enter submits. ask.jsx relies on this.
//
// Not kept: ask.jsx focuses Cancel rather than Confirm on a destructive
// question, on purpose ("Enter landing on the destructive choice is how a
// reflex becomes data loss"). That is ask.jsx's policy, not every dialog's,
// so it stays there.
export default function Modal({
  open = true, onClose, title, width = 480, children, footer,
  labelledBy, className = '', onSubmit,
}) {
  const boxRef = useDismiss(open, onClose || (() => {}))

  // The page behind must not scroll while a dialog is up: on iOS the shell is
  // position:fixed already, but a desktop board keeps its scrollbar.
  useEffect(() => {
    if (!open) return undefined
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [open])

  if (!open) return null
  const submit = (e) => {
    e.preventDefault()
    if (onSubmit) onSubmit(e)
  }
  return (
    <div className="modal-scrim">
      <form
        ref={boxRef}
        className={['modal', className].filter(Boolean).join(' ')}
        style={{ '--modal-w': `${width}px` }}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : title}
        aria-labelledby={labelledBy}
        onSubmit={submit}
      >
        {title && (
          <div className="modal-head">
            <h2>{title}</h2>
            {onClose && (
              <Button variant="icon" onClick={onClose} aria-label="close">×</Button>
            )}
          </div>
        )}
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </form>
    </div>
  )
}
