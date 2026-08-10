/* The one thing standing between a render-time throw and a white screen.
 *
 * There is no lint step, no type checker and no test runner in this frontend —
 * `npm run build` is the entire gate, and vite only catches syntax errors and
 * unresolved imports. A hook-order violation, a null deref in a render, a
 * provider that throws on mount: all of those build perfectly and blank the
 * whole app, on a Pi, usually while nobody is watching.
 *
 * So the boundary is per-route, not per-app. A page that throws should cost
 * that page and nothing else — Chat still works while Workspace is broken,
 * which is the difference between a bug and an outage.
 *
 * It resets on navigation rather than remounting on it. Keying the boundary by
 * pathname would be shorter, but it would also tear down and rebuild the whole
 * route subtree on every navigation, quietly changing what survives a move
 * between pages. `resetKey` only clears state that is already an error.
 */
import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // The console is the only place this is recoverable from after the fact:
    // these throws never reach the backend, so they are absent from the Logs
    // tab and from the journal.
    console.error('[jarvis] render failed', error, info?.componentStack)
  }

  componentDidUpdate(prev) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="err-boundary" role="alert">
        <h2>This page hit an error.</h2>
        <p className="err-b-hint">
          The rest of Jarvis is still running — the navigation above still
          works, and moving to another page clears this.
        </p>
        <pre className="err-b-detail">{String(error?.message || error)}</pre>
        <div className="row">
          <button type="button" onClick={() => this.setState({ error: null })}>
            Try this page again
          </button>
          <button type="button" className="ghost"
                  onClick={() => window.location.reload()}>
            Reload Jarvis
          </button>
        </div>
      </div>
    )
  }
}
