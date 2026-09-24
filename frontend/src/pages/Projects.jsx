import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import Page from '../components/Page.jsx'
import Card from '../components/Card.jsx'
import Button from '../components/Button.jsx'
import Input from '../components/Input.jsx'
import Select from '../components/Select.jsx'
import Tag from '../components/Tag.jsx'
import EmptyState from '../components/EmptyState.jsx'
import Menu, { MenuItem, MenuSep } from '../components/Menu.jsx'
import { AUTONOMY, autonomyHint } from '../autonomy.js'
import { ts } from '../format.js'

// A project is a card: its name is the way in (the Workspace), the slug and
// the remote say what it is, the autonomy dial is the one setting worth
// having on the card, and everything rarer — rename, load/unload, delete —
// is behind ⋯. The list this replaces put six controls on every row, the
// same six at the same weight whether you were about to open a project or
// about to delete it. The dial's levels are autonomy.js's, shared with the
// Workspace header.

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
  const ask = useAsk()

  async function refresh() {
    const r = await api('/api/projects')
    setProjects(r.projects)
    setDeleted(r.deleted || [])
    setActive(r.active)
  }
  useEffect(() => { refresh() }, [])

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
      refresh()
    } catch (err) { setError(err.detail) }
    setCreating(false)
  }

  async function rename(p) {
    const next = await ask.prompt('Rename project', p.name, { confirmLabel: 'Rename' })
    if (next === null || !next.trim()) return
    try {
      await api(`/api/projects/${p.slug}/name`, {
        method: 'PUT', body: JSON.stringify({ name: next.trim() }) })
      refresh()
    } catch (err) { setError(err.detail || String(err)) }
  }
  async function load(slug) {
    await api(`/api/projects/${slug}/load`, { method: 'POST' })
    refresh()
  }
  async function unload() {
    await api('/api/projects/unload', { method: 'POST' })
    refresh()
  }
  async function softDelete(slug) {
    if (!await ask.confirm(`Move "${slug}" to recently deleted?`,
                           { confirmLabel: 'Move to bin' })) return
    await api(`/api/projects/${slug}`, { method: 'DELETE' })
    refresh()
  }
  async function restore(slug) {
    await api(`/api/projects/${slug}/restore`, { method: 'POST' })
    refresh()
  }
  async function purge(slug) {
    if (!await ask.confirm(`Permanently delete "${slug}" and all its files?`,
                           { body: 'This cannot be undone.',
                             confirmLabel: 'Delete forever', danger: true })) return
    await api(`/api/projects/${slug}/purge`, { method: 'DELETE' })
    refresh()
  }
  async function setAutonomy(slug, level) {
    await api(`/api/projects/${slug}/autonomy`, {
      method: 'PUT', body: JSON.stringify({ level }),
    })
    refresh()
  }

  const cloning = !!repoUrl.trim()
  return (
    <Page title="Projects">
      <Card as="form" className="create-project" onSubmit={create}>
        <Input placeholder={cloning ? 'project name (repo name if empty)' : 'project name'}
               value={name} onChange={(e) => setName(e.target.value)}
               required={!cloning} />
        <Input placeholder="what are you building?" value={summary}
               onChange={(e) => setSummary(e.target.value)} />
        <Input className="gh-url" type="url"
               placeholder="GitHub URL to clone (optional)"
               title="https://github.com/owner/repo — the project starts as a clone of it"
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
                       onDelete={() => softDelete(p.slug)}
                       onAutonomy={(level) => setAutonomy(p.slug, level)} />
        ))}
        {projects.length === 0 && (
          <EmptyState pad className="project-grid-empty">
            no projects yet — create one above</EmptyState>)}
      </div>

      {deleted.length > 0 && (
        <details className="deleted-fold">
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
  p, inContext, menuOpen, setMenuOpen, onRename, onToggleContext, onDelete, onAutonomy,
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
      <div className="project-card-foot">
        <label className="project-autonomy dim small">
          autonomy
          <Select className="autonomy-sel" value={p.autonomy || 'full'} options={AUTONOMY}
                  title={`how much the agent may do unattended here — ${autonomyHint(p.autonomy || 'full')}`}
                  onChange={(e) => onAutonomy(e.target.value)} />
        </label>
      </div>
    </Card>
  )
}
