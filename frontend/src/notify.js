/* Fire-and-forget operator messages, in place of window.alert.
 *
 * iOS standalone suppresses alert() outright, so every `window.alert(...)` in
 * this app is a message the phone silently never shows. Most of them are the
 * tail of a catch — the only thing that tells the operator an action failed —
 * so on the phone those actions appear to do nothing at all.
 *
 * A window event rather than a prop chain or a context: this app already uses
 * five of them (`jarvis-files-changed`, `jarvis-player`, `jarvis-layout-changed`,
 * `jarvis-model-changed`, `jarvis-tab-renamed`), and a toast needs no reply, so
 * the one-way channel is the honest shape. `useNotices` listens and renders
 * these next to the security and queue cards.
 *
 * Severity keeps the existing language: red is critical SECURITY, amber is
 * everything else. An action that failed is amber — making it red would dilute
 * the one colour that currently means "look at this now".
 */

/** Show a toast. `title` is the line the operator reads; keep it short. */
export function notify(title, opts = {}) {
  window.dispatchEvent(new CustomEvent('jarvis-notice', {
    detail: { title: String(title ?? ''), ...opts },
  }))
}

/** The catch-tail case: whatever the server said about why this failed.
 *  `notifyError(err)` is the drop-in for `window.alert(err.detail || String(err))`. */
export function notifyError(err, fallback = 'that did not work') {
  const text = (err && (err.detail || err.message)) || String(err ?? '') || fallback
  notify(text, { sev: 'warn', life: 12 })
}
