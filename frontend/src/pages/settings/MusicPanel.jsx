import { useEffect, useState } from 'react'
import { api } from '../../api.js'
import { notifyError } from '../../notify.js'
import { Button, Card, Input } from '../../components/index.js'

// --- music server --------------------------------------------------------------

export default function MusicPanel() {
  const [tm, setTm] = useState({ url: '' })
  const [saved, setSaved] = useState('')     // the url as the server holds it
  const [test, setTest] = useState(null)
  useEffect(() => {
    api('/api/media/tarmac').then((r) => { setTm(r); setSaved(r.url || '') })
      .catch(() => {})
  }, [])

  async function save(e) {
    e.preventDefault()
    setTest(null)
    try {
      const r = await api('/api/media/tarmac', {
        method: 'PUT', body: JSON.stringify({ url: tm.url }) })
      setTm(r); setSaved(r.url || '')
    } catch (err) { notifyError(err) }
  }
  async function probe() {
    setTest({ testing: true })
    try {
      setTest(await api('/api/media/tarmac/test', { method: 'POST' }))
    } catch (err) { setTest({ ok: false, error: err.detail || String(err) }) }
  }
  const dirty = (tm.url || '') !== saved
  return (
    <Card title="Music server" headingLevel={2} id="music">
      {/* the field takes the line; Save and Test travel together, so on a
          phone they drop under it as a pair instead of Test wrapping alone */}
      <form className="settings-inline" onSubmit={save}>
        <Input aria-label="Music server address" placeholder="http://<host>:<port>"
               value={tm.url || ''} onChange={(e) => setTm({ ...tm, url: e.target.value })} />
        <div className="settings-inline-actions">
          <Button type="submit" disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</Button>
          <Button variant="ghost" onClick={probe} disabled={!saved || test?.testing}>
            Test</Button>
        </div>
      </form>
      {test && (
        <p className={`small settings-note ${test.ok ? 'dim' : 'error'}`}>
          {test.testing ? 'Asking…' : test.ok
            ? `${test.status?.tracks ?? '?'} tracks · `
              + `${test.status?.players_connected ?? 0} player(s) open`
            : test.error}
        </p>
      )}
    </Card>
  )
}
