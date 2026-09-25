import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { notifyError } from '../notify.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// Git gate: the working tree, the diff, and Jav3's pending commit requests
// with approve (commit + push) / reject — all through the existing gitgate
// endpoints (this panel only *uses* the gate; the semantics live server-side).
export default function GitPanel({ slug }) {
  const [status, setStatus] = useState('')
  const ask = useAsk()
  const [requests, setRequests] = useState([])
  const [diff, setDiff] = useState(null)     // null = hidden
  const [busy, setBusy] = useState(false)
  const [remote, setRemote] = useState(null) // {url, has_token, ahead, behind}
  const [remoteUrl, setRemoteUrl] = useState('')

  const refresh = () => Promise.all([
    api(`/api/projects/${slug}/git/status`)
      .then((r) => setStatus(r.status || '(clean)'))
      .catch((e) => setStatus(`error: ${e.detail || e}`)),
    api(`/api/projects/${slug}/git/requests`)
      .then((r) => setRequests(r.requests)).catch(() => {}),
    api(`/api/projects/${slug}/git/remote`)
      .then(setRemote).catch(() => {}),
  ])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10000)
    const h = () => refresh()
    window.addEventListener('jarvis-files-changed', h)
    return () => { clearInterval(t); window.removeEventListener('jarvis-files-changed', h) }
  }, [slug]) // eslint-disable-line

  async function toggleDiff() {
    if (diff != null) { setDiff(null); return }
    try {
      const r = await api(`/api/projects/${slug}/git/diff`)
      setDiff(r.diff || '(no unstaged changes)')
    } catch (err) { setDiff(`error: ${err.detail || err}`) }
  }

  async function act(rid, verb) {
    if (verb === 'reject'
        && !await ask.confirm(`Reject commit request #${rid}?`,
                              { confirmLabel: 'Reject', danger: true })) return
    setBusy(true)
    try {
      await api(`/api/projects/${slug}/git/requests/${rid}/${verb}`, { method: 'POST' })
      await refresh()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  async function remoteOp(fn) {
    setBusy(true)
    try { await fn() } catch (err) { notifyError(err) }
    setBusy(false)
  }
  const connect = () => remoteOp(async () => {
    const r = await api(`/api/projects/${slug}/git/remote`, {
      method: 'PUT', body: JSON.stringify({ url: remoteUrl }) })
    setRemote({ ...remote, ...r }); setRemoteUrl(''); await refresh()
  })
  const disconnect = async () => {
    if (!await ask.confirm('Disconnect the remote?',
                           { body: 'Nothing is deleted on GitHub.',
                             confirmLabel: 'Disconnect' })) return
    remoteOp(async () => {
      await api(`/api/projects/${slug}/git/remote`, {
        method: 'PUT', body: JSON.stringify({ url: null }) })
      await refresh()
    })
  }
  const syncRemote = () => remoteOp(async () =>
    setRemote(await api(`/api/projects/${slug}/git/remote?fetch=1`)))
  const doPush = () => remoteOp(async () => {
    await api(`/api/projects/${slug}/git/push`, { method: 'POST' })
    await syncRemote()
  })
  const doPull = () => remoteOp(async () => {
    await api(`/api/projects/${slug}/git/pull`, { method: 'POST' })
    await refresh(); await syncRemote()
    window.dispatchEvent(new Event('jarvis-files-changed'))
  })

  const shortRemote = remote?.url
    ? remote.url.replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '') : null
  const pending = requests.filter((r) => r.status === 'pending')
  const decided = requests.filter((r) => r.status !== 'pending').slice(0, 6)
  return (
    <div className="pane-col">
      {shortRemote ? (
        <div className="row">
          <span className="grow ellipsis" title={remote.url}>⇄ {shortRemote}</span>
          {remote.ahead != null &&
            <span className="dim small" title="ahead / behind origin">
              ↑{remote.ahead} ↓{remote.behind}</span>}
          <button className="ghost" disabled={busy} title="fetch + recount ahead/behind"
                  onClick={syncRemote}>sync</button>
          <button className="ghost" disabled={busy} onClick={doPush}>push</button>
          <button className="ghost" disabled={busy} title="fast-forward only; refuses on a dirty tree"
                  onClick={doPull}>pull</button>
          <button className="ghost danger" disabled={busy} title="disconnect remote"
                  onClick={disconnect}>✕</button>
        </div>
      ) : (
        <div className="row">
          <input className="grow" placeholder="GitHub repo URL"
                 title="https://github.com/owner/repo"
                 value={remoteUrl} onChange={(e) => setRemoteUrl(e.target.value)} />
          <button className="ghost" disabled={busy || !remoteUrl.trim()}
                  onClick={connect}>connect</button>
        </div>
      )}
      {remote && !remote.has_token && (
        <div className="dim small">no GITHUB_TOKEN secret set — private repos and
          pushes will fail (add it in Secrets)</div>
      )}
      <div className="row">
        <span className="grow dim">working tree</span>
        <button className="ghost" onClick={toggleDiff}>{diff != null ? 'hide diff' : 'diff'}</button>
        <button className="ghost" onClick={refresh}>↻</button>
      </div>
      <pre className="git-status">{status || '…'}</pre>
      {diff != null && <pre className="git-diff">{diff}</pre>}
      <div className="dim small">commit requests — approving commits (and pushes, when a
        remote is set) on the host</div>
      <ul className="staged-list">
        {pending.length === 0 && <EmptyState as="li">nothing waiting on you</EmptyState>}
        {pending.map((r) => (
          <li key={r.id}>
            <span className="tag new">#{r.id}</span>
            {r.kind === 'remote' && <span className="tag">remote</span>}
            <span className="grow ellipsis" title={r.message}>{r.message}</span>
            {r.error && <span className="tag error" title={r.error}>retry</span>}
            <button className="win-btn ok" disabled={busy}
                    title={r.kind === 'remote'
                      ? 'approve: verify + connect + push existing commits'
                      : 'approve: commit + push'}
                    onClick={() => act(r.id, 'approve')}>✓</button>
            <button className="win-btn" title="reject" disabled={busy}
                    onClick={() => act(r.id, 'reject')}>✕</button>
          </li>
        ))}
        {decided.map((r) => (
          <li key={r.id} className="dim">
            <span className={`tag ${r.status === 'approved' ? 'done' : 'error'}`}>{r.status}</span>
            <span className="grow ellipsis" title={r.message}>{r.message}</span>
            {r.commit_sha && <span className="mono small">{r.commit_sha.slice(0, 7)}</span>}
            {r.error && <span className="tag error" title={r.error}>push failed</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}
