import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import Page from '../components/Page.jsx'
import Card from '../components/Card.jsx'
import Button from '../components/Button.jsx'
import Input from '../components/Input.jsx'
import Tag from '../components/Tag.jsx'
import EmptyState from '../components/EmptyState.jsx'
import Menu, { MenuItem, MenuSep } from '../components/Menu.jsx'
import { ts } from '../format.js'
import { projectsChanged } from '../projectsChanged.js'

// A project is a card: its name is the way in (the Workspace), the slug and
// the remote say what it is, and everything rarer — rename, load/unload,
// delete — is behind ⋯. The list this replaces put six controls on every
// row, the same six at the same weight whether you were about to open a
// project or about to delete it. The per-project autonomy dial is not shown:
// the operator found it confusing, so every project runs at the backend's
// default (NULL == `full`, backend/autonomy.py).

export default function Projects() {
  const [projects, setProjects] = useState([])
  const [deleted, setDeleted] = useState([])
  const [active, setActive] = useState(null)
  const [name, setName] = useState('')
  const [summary, setSummary] = useState('')
  const [error, setError] = useState(null)
  const [repoUrl, setRepoUrl] = useState('')
  const [creating, setCreating] = useState(false)
  const [menuFor, setMenuFor] = useState(null)   // slug whose ⋯ menu is open
  const [binOpen, setBinOpen] = useState(false)    // Recently deleted, expanded
  const ask = useAsk()

  async function refresh() {
    const r = await api('/api/projects')
    setProjects(r.projects)
    setDeleted(r.deleted || [])
    setActive(r.active)
  }
  useEffect(() => { refresh() }, [])
  // every change also tells the sidebar and the chip menu (they keep their own
  // copy of the list), which a plain refresh() here never reached
  const changed = () => refresh().then(projectsChanged).catch(() => {})

  // One form, both paths: a GitHub URL turns the create into a clone-import,
  // and name/description ride along either way (the import derives a name
  // from the repo when the field is left empty).
  async function create(e) {
    e.preventDefault()
    setError(null)
    setCreating(true)
    const url = repoUrl.trim()
    try {
      if (url) {
        await api('/api/projects/import', {
          method: 'POST',
          body: JSON.stringify({ url, name: name.trim() || undefined,
                                 summary: summary.trim() || undefined }),
        })
      } else {
        await api('/api/projects', {
          method: 'POST',
          body: JSON.stringify({ name, summary: summary || undefined }),
        })
      }
      setName(''); setSummary(''); setRepoUrl('')
      changed()
    } catch (err) {
      // the slug is taken by a project in the bin: say so and open the bin
      const taken = /'([^']+)'/.exec(err.detail || '')?.[1]
      if (taken && /already exists/.test(err.detail || '')
          && deleted.some((d) => d.slug === taken)) {
        setError(`"${taken}" is in Recently deleted. Restore it or delete it forever, `
          + 'or pick another name.')
        setBinOpen(true)
      } else setError(err.detail || String(err))
    }
    setCreating(false)
  }

  async function rename(p) {
    const next = await ask.prompt('Rename project', p.name, { confirmLabel: 'Rename' })
    if (next === null || !next.trim()) return
    try {
      await api(`/api/projects/${p.slug}/name`, {
        method: 'PUT', body: JSON.stringify({ name: next.trim() }) })
      changed()
    } catch (err) { setError(err.detail || String(err)) }
  }
  // load / unload / delete / restore used to let a failed call surface as an
  // unhandled rejection; the reason lands in the form's error line instead
  async function act(fn) {
    setError(null)
    try { await fn(); changed() } catch (err) { setError(err.detail || String(err)) }
  }
  const load = (slug) => act(() => api(`/api/projects/${slug}/load`, { method: 'POST' }))
  const unload = () => act(() => api('/api/projects/unload', { method: 'POST' }))
  async function softDelete(slug) {
    if (!await ask.confirm(`Move "${slug}" to recently deleted?`,
                           { confirmLabel: 'Move to bin' })) return
    act(() => api(`/api/projects/${slug}`, { method: 'DELETE' }))
  }
  const restore = (slug) =>
    act(() => api(`/api/projects/${slug}/restore`, { method: 'POST' }))
  async function purge(slug) {
    if (!await ask.confirm(`Permanently delete "${slug}" and all its files?`,
                           { body: 'This cannot be undone.',
                             confirmLabel: 'Delete forever', danger: true })) return
    act(() => api(`/api/projects/${slug}/purge`, { method: 'DELETE' }))
  }

  const cloning = !!repoUrl.trim()
  return (
    <Page title="Projects">
      <Card as="form" className="create-project" onSubmit={create}>
        <Input placeholder={cloning ? 'project name (optional)' : 'project name'}
               value={name} onChange={(e) => setName(e.target.value)}
               required={!cloning} />
        <Input placeholder="what are you building?" value={summary}
               onChange={(e) => setSummary(e.target.value)} />
        <Input className="gh-url" type="url"
               placeholder="GitHub URL (optional)"
               title="https://github.com/owner/repo — optional: the project starts as a clone of it"
               value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
        <Button type="submit" disabled={creating}>
          {creating && cloning ? 'cloning…' : cloning ? 'Clone & create' : 'Create'}</Button>
        {error && <span className="error">{error}</span>}
      </Card>

      <div className="project-grid">
        {projects.map((p) => (
          <ProjectCard key={p.slug} p={p} inContext={active === p.slug}
                       menuOpen={menuFor === p.slug}
                       setMenuOpen={(open) => setMenuFor(open ? p.slug : null)}
                       onRename={() => rename(p)}
                       onToggleContext={() => (active === p.slug ? unload() : load(p.slug))}
                       onDelete={() => softDelete(p.slug)} />
        ))}
        {projects.length === 0 && (
          <EmptyState pad className="project-grid-empty">
            no projects yet — create one above</EmptyState>)}
      </div>

      {deleted.length > 0 && (
        <details className="deleted-fold" open={binOpen}
                 onToggle={(e) => setBinOpen(e.currentTarget.open)}>
          <summary>
            Recently deleted ({deleted.length})
            <span className="chev" aria-hidden="true">›</span>
          </summary>
          <div className="project-grid">
            {deleted.map((p) => (
              <Card as="article" key={p.slug} className="project-card deleted">
                <div className="project-card-head">
                  <span className="project-name">{p.name}</span>
                </div>
                <code className="project-slug">{p.slug}</code>
                <span className="dim small project-when">deleted {ts(p.deleted_at)}</span>
                <div className="project-card-foot">
                  <span className="grow" />
                  <Button variant="ghost" onClick={() => restore(p.slug)}>Restore</Button>
                  <Button variant="ghost" danger onClick={() => purge(p.slug)}>
                    Delete forever</Button>
                </div>
              </Card>
            ))}
          </div>
        </details>
      )}
    </Page>
  )
}

function ProjectCard({
  p, inContext, menuOpen, setMenuOpen, onRename, onToggleContext, onDelete,
}) {
  const close = () => setMenuOpen(false)
  const pick = (fn) => () => { close(); fn() }
  return (
    <Card as="article" className={inContext ? 'project-card in-context' : 'project-card'}>
      <div className="project-card-head">
        {/* the name is the way in — the Workspace is the project */}
        <Link to={`/projects/${p.slug}`} className="project-name"
              title={`open ${p.name}`}>{p.name}</Link>
        {inContext && <Tag tone="running">in context</Tag>}
        <Menu floating open={menuOpen} onClose={close} label={`${p.name} actions`} width={210}
              trigger={(
                <Button variant="icon" aria-haspopup="menu" aria-expanded={menuOpen}
                        aria-label={`${p.name} actions`} title="more"
                        onClick={() => setMenuOpen(!menuOpen)}>⋯</Button>
              )}>
          <MenuItem onClick={pick(onRename)}>Rename</MenuItem>
          <MenuItem onClick={pick(onToggleContext)}>
            {inContext ? 'Unload from context' : 'Load into context'}</MenuItem>
          <MenuSep />
          <MenuItem danger onClick={pick(onDelete)}>Delete</MenuItem>
        </Menu>
      </div>
      <code className="project-slug">{p.slug}</code>
      {p.github_remote && (
        <span className="dim small ellipsis project-remote" title={p.github_remote}>
          {p.github_remote}</span>)}
    </Card>
  )
}
