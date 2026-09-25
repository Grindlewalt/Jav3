import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// A live shell INSIDE the disposable guest VM — co-work beside the agent in the
// same sandbox its run_code executes in (no secrets there, nukeable). The WS
// broker pins the guest for the session and primes this project's files so the
// shell lands where the agent's tools operate. Reconnects on the Reconnect
// button, not automatically (a dead socket usually means the guest is off).
function termTheme() {
  const css = getComputedStyle(document.documentElement)
  const v = (name) => css.getPropertyValue(name).trim()
  const bg = v('--term-bg')
  return { background: bg, foreground: v('--term-fg'), cursor: v('--term-cursor'),
           cursorAccent: bg, selectionBackground: v('--term-selection') }
}

export default function TerminalPanel({ slug }) {
  const hostRef = useRef(null)
  const [status, setStatus] = useState('connecting')
  const [gen, setGen] = useState(0)   // bump to reconnect

  useEffect(() => {
    const term = new Terminal({
      fontSize: 13, cursorBlink: true, convertEol: false,
      fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
      theme: termTheme(),
    })
    // xterm paints to a canvas, so CSS can't restyle it: follow the theme
    // toggle (index.html / the nav stamp data-theme on <html>) by hand
    const themeWatch = new MutationObserver(() => { term.options.theme = termTheme() })
    themeWatch.observe(document.documentElement,
                       { attributes: true, attributeFilter: ['data-theme'] })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(hostRef.current)
    try { fit.fit() } catch { /* not laid out yet */ }

    const enc = new TextEncoder()
    const dec = new TextDecoder()
    const b64 = (u8) => btoa(String.fromCharCode(...u8))
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(
      `${proto}://${location.host}/api/guest/shell?slug=${encodeURIComponent(slug)}`)

    ws.onopen = () => {
      setStatus('connected')
      const { cols, rows } = term
      ws.send(JSON.stringify({ type: 'init', cols, rows, slug }))
      term.focus()
    }
    ws.onmessage = (m) => {
      let ev
      try { ev = JSON.parse(m.data) } catch { return }
      if (ev.type === 'o') {
        const bytes = Uint8Array.from(atob(ev.data), (c) => c.charCodeAt(0))
        term.write(dec.decode(bytes))
      } else if (ev.type === 'exit') {
        term.write(`\r\n[shell exited (${ev.code})]\r\n`)
        setStatus('closed')
      }
    }
    ws.onclose = () => setStatus((s) => (s === 'closed' ? s : 'disconnected'))
    ws.onerror = () => setStatus('disconnected')

    const onData = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'i', data: b64(enc.encode(d)) }))
    })
    const resize = () => {
      try { fit.fit() } catch { /* ignore */ }
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'r', cols: term.cols, rows: term.rows }))
    }
    const ro = new ResizeObserver(resize)
    ro.observe(hostRef.current)

    return () => {
      ro.disconnect(); themeWatch.disconnect(); onData.dispose()
      try { ws.close() } catch { /* ignore */ }
      term.dispose()
    }
  }, [slug, gen])

  return (
    <div className="pane-col">
      <div className="row">
        <span className={`dim small grow term-status ${status}`}>guest shell · {status}</span>
        {(status === 'disconnected' || status === 'closed') &&
          <button className="ghost" onClick={() => setGen((g) => g + 1)}>reconnect</button>}
      </div>
      <div ref={hostRef} className="term-host" />
    </div>
  )
}
