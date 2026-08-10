/* In-page confirm and prompt, in place of window.confirm / window.prompt.
 *
 * iOS standalone suppresses both. `confirm()` returns false there, which is the
 * dangerous half: a guard written as `if (!window.confirm(...)) return` becomes
 * "always cancel" on the phone, so the control is not merely dead, it is a
 * control the operator presses and which silently does nothing.
 *
 * The API mirrors the globals it replaces, so converting a call site is a local
 * edit rather than a redesign:
 *
 *     const ask = useAsk()
 *     if (!await ask.confirm('delete this?')) return
 *     const name = await ask.prompt('project name', 'untitled')   // null = cancelled
 *
 * Two invariants matter more than anything else here, because getting either
 * wrong is worse than the blocking dialogs were:
 *
 * 1. It resolves FALSY on every path that is not an explicit confirm — Escape,
 *    the scrim, an outside pointerdown, a replacing request, provider unmount.
 *    `Projects.jsx` asks "permanently delete and all its files? This cannot be
 *    undone." If a dismissal ever resolved truthy, dismissing the dialog would
 *    delete the project.
 * 2. It ALWAYS resolves. A dropped promise leaves the caller's `await` hanging
 *    forever: the handler never returns, its `busy` flag stays set, and nothing
 *    in the UI says why. That failure is invisible to `npm run build`, which is
 *    this frontend's only gate.
 *
 * `prompt` resolves null for cancel and '' for an empty submission, because
 * three call sites treat those differently — an empty mark clears a directory
 * label, an empty host list makes a secret deliberately unusable.
 */
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
} from 'react'
import { useDismiss } from './useDismiss.js'

const AskContext = createContext(null)

export function useAsk() {
  const ctx = useContext(AskContext)
  if (!ctx) {
    // Loud on purpose. The ErrorBoundary catches this and shows the page as
    // broken, which is far better than a silent fallback that makes every
    // destructive confirm quietly answer "no".
    throw new Error('useAsk() used outside <AskProvider>')
  }
  return ctx
}

export function AskProvider({ children }) {
  const [req, setReq] = useState(null)
  const pending = useRef(null)          // { resolve, cancel }

  const settle = useCallback((value) => {
    const p = pending.current
    pending.current = null
    setReq(null)
    if (p) p.resolve(value)
  }, [])

  const open = useCallback((spec) => new Promise((resolve) => {
    if (pending.current) {
      // a second ask while one is open: answer the first as cancelled rather
      // than leaving its caller awaiting a dialog that no longer exists
      const prev = pending.current
      pending.current = null
      prev.resolve(prev.cancel)
    }
    pending.current = { resolve, cancel: spec.cancel }
    setReq(spec)
  }), [])

  // the app is going away and someone is still awaiting an answer
  useEffect(() => () => {
    const p = pending.current
    pending.current = null
    if (p) p.resolve(p.cancel)
  }, [])

  const confirm = useCallback(
    (message, opts = {}) => open({ kind: 'confirm', message, cancel: false, ...opts }),
    [open])
  const prompt = useCallback(
    (message, defaultValue = '', opts = {}) =>
      open({ kind: 'prompt', message, defaultValue, cancel: null, ...opts }),
    [open])

  const value = useRef(null)
  if (!value.current) value.current = { confirm, prompt }

  return (
    <AskContext.Provider value={value.current}>
      {children}
      {req && <AskDialog req={req} settle={settle} />}
    </AskContext.Provider>
  )
}

function AskDialog({ req, settle }) {
  const [text, setText] = useState(req.defaultValue ?? '')
  const cancel = useCallback(() => settle(req.cancel), [settle, req.cancel])
  const boxRef = useDismiss(true, cancel)      // outside pointerdown + Escape
  const cancelRef = useRef(null)

  useEffect(() => {
    // A prompt wants its input. A confirm focuses CANCEL rather than the
    // confirm button: these dialogs guard deletes, and Enter landing on the
    // destructive choice is how a reflex becomes data loss.
    if (req.kind !== 'prompt') cancelRef.current?.focus()
  }, [req.kind])

  return (
    <div className="ask-scrim">
      <form className="ask-modal" ref={boxRef} role="alertdialog" aria-modal="true"
            onSubmit={(e) => {
              e.preventDefault()
              settle(req.kind === 'prompt' ? text : true)
            }}>
        <div className="ask-body">
          <p className="ask-msg">{req.message}</p>
          {req.body && <p className="ask-sub">{req.body}</p>}
          {req.kind === 'prompt' && (
            <input className="ask-input" value={text} autoFocus
                   type={req.password ? 'password' : 'text'}
                   placeholder={req.placeholder || ''}
                   onChange={(e) => setText(e.target.value)} />
          )}
        </div>
        <div className="ask-foot">
          <button type="button" className="ghost" ref={cancelRef} onClick={cancel}>
            {req.cancelLabel || 'Cancel'}
          </button>
          <button type="submit" className={req.danger ? 'ghost danger' : ''}>
            {req.confirmLabel || (req.kind === 'prompt' ? 'OK' : 'Confirm')}
          </button>
        </div>
      </form>
    </div>
  )
}
