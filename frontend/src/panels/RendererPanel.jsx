import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { cspMediaSources } from '../mediaHosts.js'
import EmptyState from '../components/EmptyState.jsx'
import { MEDIA_EXT, rawUrl } from './shared.js'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// The Renderer runs untrusted, agent-authored HTML. sandbox="allow-scripts" lets
// scripts run but does NOT stop the frame reaching the network, so a script or an
// <img> could beacon data out. The CSP below closes that: scripts/styles may be
// inline but connect-src is denied (no fetch/XHR/WebSocket), and images/media/
// fonts load only from data: or the operator's media allowlist — the same policy
// chat uses, so a dashboard can show trusted media but can't exfiltrate.
function renderCsp() {
  const media = cspMediaSources()
  return "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
    `img-src ${media}; media-src ${media}; font-src ${media}; ` +
    "base-uri 'none'; form-action 'none'"
}

function withCsp(html) {
  if (!html) return ''
  const meta = `<meta http-equiv="Content-Security-Policy" content="${renderCsp()}">`
  if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, (m) => m + meta)
  return `<!doctype html><head>${meta}</head>${html}`
}

export default function RendererPanel({ slug, state, setState, onToggleExpand }) {
  const [files, setFiles] = useState([])
  const [html, setHtml] = useState('')
  const path = state.path || ''

  const refresh = useCallback(() =>
    api(`/api/projects/${slug}/files`).then((r) =>
      setFiles(r.files.map((f) => f.path).filter((p) => MEDIA_EXT.test(p)))), [slug])
  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    if (path && /\.html?$/i.test(path)) {
      api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`)
        .then((r) => setHtml(r.content || ''))
        .catch(() => setHtml(''))
    }
  }, [slug, path])

  const url = path && rawUrl(slug, path)
  return (
    <div className="pane-col">
      <div className="row">
        <select className="grow" value={path} onChange={(e) => setState({ path: e.target.value })}>
          <option value="">— pick html / pdf / image —</option>
          {files.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="ghost" onClick={refresh} title="refresh">↻</button>
        {url && <a className="ghost-link" href={url} target="_blank" rel="noreferrer">raw</a>}
      </div>
      <div className="render-area" onDoubleClick={onToggleExpand}>
        {!path ? (
          <EmptyState pad>nothing selected — plots, PDFs and pages the run sandbox
            produces show up in this list</EmptyState>
        ) : /\.html?$/i.test(path) ? (
          <iframe className="preview-frame" sandbox="allow-scripts" title="preview" srcDoc={withCsp(html)} />
        ) : path.endsWith('.pdf') ? (
          <embed className="preview-frame" src={url} type="application/pdf" />
        ) : (
          <div className="preview-scroll"><img src={url} alt={path} /></div>
        )}
      </div>
    </div>
  )
}
