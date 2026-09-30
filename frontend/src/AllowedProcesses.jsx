import { api } from './api.js'
import { useAsk } from './ask.jsx'
import { Button } from './components/index.js'
import { notifyError } from './notify.js'
import { useLoad } from './boxes/ui.jsx'
import { allowedLine } from './securityCopy.js'

// Security > Persistent > "Allowed from alerts": the programs and units you
// allowed from an unexpected-process alert (POST /api/security/events/{id}/baseline),
// each with a Remove. Shown only when there is something on the list, and
// folded shut: it is a place to undo, not a page section.
//
// The exe and unit come from the guest (the alert's detail): text nodes only.

export default function AllowedProcesses() {
  const { data, reload } = useLoad(() => api('/api/security/baseline').then((r) => r.entries || []))
  const ask = useAsk()
  if (!data || data.length === 0) return null

  async function remove(e) {
    const line = allowedLine(e)
    if (!await ask.confirm(`Take ${line.what} off the list?`, {
      body: 'It alerts again the next time a box boots and it runs.',
      confirmLabel: 'Take off', danger: true })) return
    try {
      await api('/api/security/baseline/remove', {
        method: 'POST', body: JSON.stringify({ exe: e.exe, unit: e.unit || '' }) })
      reload()
    } catch (err) { notifyError(err) }
  }

  return (
    <details className="sbx-sec allowed-procs">
      <summary className="small">Allowed from alerts ({data.length})</summary>
      <p className="dim small">Programs you allowed from an alert. They no longer count as
        unexpected in any box.</p>
      <ul className="staged-list rev-list">
        {data.map((e) => {
          const line = allowedLine(e)
          return (
            <li key={`${e.exe}|${e.unit}`}>
              <span className="grow ellipsis mono small" title={line.what}>{line.what}</span>
              <span className="dim small">{line.when}</span>
              <Button variant="ghost" onClick={() => remove(e)}>Take off</Button>
            </li>
          )
        })}
      </ul>
    </details>
  )
}
