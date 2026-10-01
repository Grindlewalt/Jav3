/* Git: one project's repo on this host's Gitea. Where main stands against the
 * host's, the agent's pull requests (with a diff, Approve and Reject), branches,
 * recent commits, who can open the repo, and the Gitea accounts.
 *
 * Everything is read from /api/gitea (operator-only; the admin token stays on the
 * host). Approve and Reject are NOT new: they are the Review Center's own
 * /api/projects/{slug}/git/requests/{id}/approve|reject, so the same request gets
 * the same decision from either place. Only the agent's own pull requests show:
 * a pull request opened by hand in Gitea is Gitea's to merge.
 *
 * Nothing polls. Listing the agent's pull requests asks Gitea about each waiting
 * one, so the page reads on open, on Refresh, and after something it did. */
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { notifyError } from '../notify.js'
import { GiteaAccounts } from '../GiteaPanel.jsx'
import Page from '../components/Page.jsx'
import { Button, Card, EmptyState, Select, Tag } from '../components/index.js'
import {
  ACCESS_LEVELS, accessEditable, diffLines, diffStat, filesText, pickSlug, projectOptions,
  shortDate, syncView, urlNote,
} from '../gitPage.js'

const REMEMBER = 'jarvis.git.last'
const remembered = () => { try { return localStorage.getItem(REMEMBER) } catch { return null } }
const remember = (s) => { try { localStorage.setItem(REMEMBER, s) } catch { /* private mode */ } }

// GET `path` (null = nothing to ask). `dep` re-reads it. Returns [state, reload].
function useGet(path, dep = 0) {
  const [s, setS] = useState({ data: null, error: null, loading: Boolean(path) })
  const [n, setN] = useState(0)
  useEffect(() => {
    if (!path) { setS({ data: null, error: null, loading: false }); return undefined }
    let live = true
    setS((o) => ({ ...o, loading: true, error: null }))
    api(path)
      .then((data) => { if (live) setS({ data, error: null, loading: false }) })
      .catch((e) => { if (live) setS({ data: null, error: e.detail || String(e), loading: false }) })
    return () => { live = false }
  }, [path, dep, n])
  return [s, useCallback(() => setN((x) => x + 1), [])]
}

const Ext = ({ href, children, ...rest }) => (
  <a href={href} target="_blank" rel="noreferrer" {...rest}>{children}</a>
)

// ---- the agent's pull requests ----

function Diff({ slug, number }) {
  const [s] = useGet(`/api/gitea/repos/${slug}/pulls/${number}/diff`)
  if (s.loading) return <p className="dim small">loading the diff…</p>
  if (s.error) return <p className="warn">Could not read the diff: {s.error}</p>
  return (
    <>
      <pre className="mem-diff git-diff-view">
        {diffLines(s.data.diff).map((l, i) => (
          <span key={i} className={`mem-dl ${l.cls}`}>{l.text}</span>
        ))}
      </pre>
      {s.data.truncated && (
        <p className="dim small">Cut short. <Ext href={s.data.pr_url}>Open it in Gitea</Ext> for the rest.</p>
      )}
    </>
  )
}

function Pull({ slug, p, busy, onDecide }) {
  const [open, setOpen] = useState(false)
  const stat = diffStat(p)
  return (
    <li className="git-pull">
      <div className="git-pull-row">
        <span className="tag new" title={`Gitea pull request #${p.pr_number}, Jav3 request #${p.id}`}>
          #{p.pr_number}</span>
        <strong className="grow ellipsis" title={p.title}>{p.title || '(no title)'}</strong>
        <code className="small ellipsis git-branch-name" title={p.branch}>{p.branch}</code>
        {stat && <span className="mono small">{stat}</span>}
        {filesText(p) && <span className="dim small">{filesText(p)}</span>}
      </div>
      <div className="git-pull-actions">
        <Button variant="ghost" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Hide diff' : 'Diff'}</Button>
        {p.pr_url && <Ext className="small" href={p.pr_url}>Open in Gitea</Ext>}
        <span className="grow" />
        <Button variant="ghost" danger disabled={busy} onClick={() => onDecide(p, 'reject')}>
          Reject</Button>
        <Button disabled={busy} onClick={() => onDecide(p, 'approve')}
                title="Merges the pull request into main as you, then moves the host's main up to it">
          Approve</Button>
      </div>
      {p.error && <p className="warn">{p.error}</p>}
      {open && <Diff slug={slug} number={p.pr_number} />}
    </li>
  )
}

function Pulls({ slug, pulls, onChanged }) {
  const ask = useAsk()
  const [busy, setBusy] = useState(false)
  const d = pulls.data
  async function decide(p, verb) {
    if (verb === 'reject'
        && !await ask.confirm(`Reject pull request #${p.pr_number}?`,
                              { body: 'It is closed in Gitea and its branch is deleted.',
                                confirmLabel: 'Reject', danger: true })) return
    setBusy(true)
    try {
      await api(`/api/projects/${slug}/git/requests/${p.id}/${verb}`, { method: 'POST' })
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { notifyError(e) }
    setBusy(false)
    onChanged()
  }
  const count = d ? d.pulls.length : 0
  return (
    <Card title={`Agent pull requests${d ? ` (${count})` : ''}`} headingLevel={2}
          actions={<Link className="small git-card-link" to="/security">Review Center ›</Link>}>
      {pulls.loading && !d ? <EmptyState>loading…</EmptyState>
        : pulls.error ? <p className="warn">Could not read them: {pulls.error}</p>
        : count === 0 ? (
          <EmptyState>Nothing waiting. The agent opens a pull request with git_push_request.</EmptyState>
        ) : (
          <ul className="git-pulls">
            {d.pulls.map((p) => <Pull key={p.id} slug={slug} p={p} busy={busy} onDecide={decide} />)}
          </ul>
        )}
      {d && d.recent.length > 0 && (
        <details className="git-recent">
          <summary className="dim small">Recently decided ({d.recent.length})</summary>
          <ul className="git-pulls">
            {d.recent.map((p) => (
              <li key={p.id} className="git-pull-row dim">
                <Tag tone={p.status === 'approved' ? 'done' : 'error'}>{p.status}</Tag>
                <span className="grow ellipsis" title={p.title}>#{p.pr_number} {p.title}</span>
                {diffStat(p) && <span className="mono small">{diffStat(p)}</span>}
                <span className="small">{shortDate(p.decided_at)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  )
}

// ---- push status ----

function PushStatus({ slug, sync, onChanged }) {
  const [busy, setBusy] = useState(false)
  const v = syncView(sync.data)
  async function run() {
    setBusy(true)
    try {
      await api(`/api/gitea/repos/${slug}/push`, { method: 'POST' })
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { notifyError(e) }
    setBusy(false)
    onChanged()
  }
  return (
    <Card title="Push status" headingLevel={2}>
      {sync.loading && !sync.data ? <EmptyState>checking…</EmptyState>
        : sync.error ? <p className="warn">Could not compare the two mains: {sync.error}</p>
        : v && (
          <div className="git-sync">
            <Tag tone={v.tone}>{v.word}</Tag>
            <span className="grow">{v.text}</span>
            {v.action && (
              <Button disabled={busy} title={v.action.title} onClick={run}>{v.action.label}</Button>
            )}
          </div>
        )}
    </Card>
  )
}

// ---- branches and history ----

function Branches({ branches, branch, onPick }) {
  const rows = branches.data?.branches || []
  return (
    <Card title="Branches" headingLevel={2}>
      {branches.loading && !branches.data ? <EmptyState>loading…</EmptyState>
        : branches.error ? <p className="warn">Could not read them: {branches.error}</p>
        : (
          <ul className="git-chips">
            {rows.map((b) => (
              <li key={b.name}>
                <button type="button" className={`ghost git-chip${b.name === branch ? ' on' : ''}`}
                        aria-pressed={b.name === branch} onClick={() => onPick(b.name)}
                        title={b.subject ? `${b.short} ${b.subject}` : b.short}>
                  {b.name}
                  {b.protected && <span className="dim"> protected</span>}
                  {b.pr_number != null && <span className="dim"> PR #{b.pr_number}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
    </Card>
  )
}

function Commits({ slug, repoUrl, branch, rev }) {
  const [rows, setRows] = useState(null)
  const [more, setMore] = useState(false)
  const [page, setPage] = useState(1)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback((n, replace) => {
    setBusy(true)
    return api(`/api/gitea/repos/${slug}/commits?branch=${encodeURIComponent(branch)}&page=${n}&limit=10`)
      .then((r) => {
        setRows((o) => (replace || !o ? r.commits : [...o, ...r.commits]))
        setMore(r.more); setPage(n); setError(null)
      })
      .catch((e) => setError(e.detail || String(e)))
      .finally(() => setBusy(false))
  }, [slug, branch])
  useEffect(() => { setRows(null); load(1, true) }, [load, rev])
  return (
    <Card title={`Recent commits on ${branch}`} headingLevel={2}>
      {error ? <p className="warn">Could not read them: {error}</p>
        : rows === null ? <EmptyState>loading…</EmptyState>
        : rows.length === 0 ? <EmptyState>No commits yet.</EmptyState>
        : (
          <ul className="git-commits">
            {rows.map((c) => (
              <li key={c.sha}>
                <Ext className="mono small" href={`${repoUrl}/commit/${c.sha}`}>{c.short}</Ext>
                <span className="grow ellipsis" title={c.subject}>{c.subject}</span>
                <span className="dim small">{shortDate(c.date)}</span>
              </li>
            ))}
          </ul>
        )}
      {more && !error && (
        <Button variant="ghost" disabled={busy} onClick={() => load(page + 1, false)}>Show more</Button>
      )}
    </Card>
  )
}

// ---- access ----

function Access({ slug, rev }) {
  const [s, reload] = useGet(`/api/gitea/repos/${slug}/access`, rev)
  async function set(login, permission) {
    try {
      await api(`/api/gitea/repos/${slug}/access/${encodeURIComponent(login)}`, {
        method: 'PUT', body: JSON.stringify({ permission }) })
    } catch (e) { notifyError(e) }
    reload()
  }
  const rows = s.data?.access || []
  return (
    <Card title="Access" headingLevel={2}>
      {s.loading && !s.data ? <EmptyState>loading…</EmptyState>
        : s.error ? <p className="warn">Could not read it: {s.error}</p>
        : (
          <ul className="git-access">
            {rows.map((r) => (
              <li key={r.login}>
                <strong className="ellipsis">{r.login}</strong>
                {r.disabled && <Tag>disabled</Tag>}
                {accessEditable(r) ? (
                  <Select aria-label={`Access for ${r.login}`} value={r.permission === 'none' ? '' : r.permission}
                          placeholder={r.permission === 'none' ? 'no access' : undefined}
                          options={ACCESS_LEVELS} onChange={(e) => set(r.login, e.target.value)} />
                ) : (
                  <span className="dim">({r.owner ? 'owner' : r.permission})</span>
                )}
              </li>
            ))}
          </ul>
        )}
      <p className="dim small git-note">
        Everyone who can sign in reads every repo. Write lets them push branches and open pull
        requests; main stays yours, because only you can push or merge there.
      </p>
    </Card>
  )
}

// ---- one project ----

function RepoView({ slug, repo, rev }) {
  const [sync, reloadSync] = useGet(`/api/gitea/repos/${slug}/sync`)
  const [pulls, reloadPulls] = useGet(`/api/gitea/repos/${slug}/pulls`)
  const [branches, reloadBranches] = useGet(`/api/gitea/repos/${slug}/branches`)
  const [branch, setBranch] = useState(repo.default_branch || 'main')
  const [commitsRev, setCommitsRev] = useState(0)
  const all = () => { reloadSync(); reloadPulls(); reloadBranches(); setCommitsRev((n) => n + 1) }
  return (
    <>
      <div className="git-refresh">
        <Button variant="ghost" onClick={all}>Refresh</Button>
      </div>
      <PushStatus slug={slug} sync={sync} onChanged={all} />
      <Pulls slug={slug} pulls={pulls} onChanged={all} />
      <Branches branches={branches} branch={branch} onPick={setBranch} />
      <Commits slug={slug} repoUrl={repo.url} branch={branch} rev={commitsRev} />
      <Access slug={slug} rev={rev} />
    </>
  )
}

// ---- the page ----

export default function Git() {
  const { slug: routeSlug } = useParams()
  const navigate = useNavigate()
  const [st, setSt] = useState(null)
  const [repos, setRepos] = useState(null)
  const [projects, setProjects] = useState(null)
  const [rev, setRev] = useState(0)          // accounts changed: the Access row reads again
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api('/api/gitea/status').then((s) => {
      setSt(s)
      if (s.configured && s.running) {
        api('/api/gitea/repos').then((r) => setRepos(r.repos || [])).catch(() => setRepos([]))
      } else setRepos([])
    }).catch(() => setSt({ configured: false, unknown: true }))
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => setProjects([]))
  }, [])

  const ready = st && repos && projects
  const slug = ready ? pickSlug({ route: routeSlug, remembered: remembered(), projects, repos }) : null
  const repo = ready && slug ? repos.find((r) => r.slug === slug) : null
  const options = ready ? projectOptions(projects, repos) : []
  const note = urlNote(st)

  function choose(s) {
    remember(s)
    navigate(`/git/${s}`, { replace: true })
  }
  async function createRepo() {
    setBusy(true)
    try {
      await api(`/api/gitea/repos/${slug}`, { method: 'POST' })
      load()
    } catch (e) { notifyError(e) }
    setBusy(false)
  }

  let body = null
  if (!st) body = null
  else if (st.unknown) {
    body = <p className="dim small">Couldn't load Gitea's status from Jav3. Reload the page to try again.</p>
  } else if (!st.configured) {
    body = (
      <Card title="Gitea" headingLevel={2}>
        <p className="dim small settings-note">
          Not set up{st.missing?.length ? ` (missing: ${st.missing.join(', ')})` : ''}.
          Agents can still ask for commits. To give them pull requests for you to review, run{' '}
          <code>python -m backend.cli gitea-setup</code> on the host, then restart Jav3.
        </p>
      </Card>
    )
  } else if (!st.running) {
    body = (
      <Card title="Gitea isn't answering" headingLevel={2}>
        <p className="warn settings-note">
          Agents' push requests will fail until it is back. Start it on the host with{' '}
          <code>systemctl --user start jav3-gitea-{st.port}</code>{' '}
          <Button variant="ghost" onClick={load}>Check again</Button>
        </p>
      </Card>
    )
  } else if (!ready) {
    body = <EmptyState>loading…</EmptyState>
  } else if (!slug) {
    body = <EmptyState>No projects yet. Make one in Work, then its repo shows up here.</EmptyState>
  } else if (!repo) {
    body = (
      <Card title="No repo yet" headingLevel={2}>
        <p className="dim small settings-note">
          {slug} has no repo in Gitea. One is made the first time an agent files a pull request,
          or you can make it now.
        </p>
        <Button disabled={busy} onClick={createRepo}>Create repo</Button>
      </Card>
    )
  } else {
    body = <RepoView key={slug} slug={slug} repo={repo} rev={rev} />
  }

  return (
    <Page title="Git" className="git-page"
          lede="A project's repo on this host's Gitea: what the agent has proposed, what is on main, and who can open it.">
      {st?.configured && st.running && (
        <div className="git-bar">
          {options.length > 0 && (
            <Select aria-label="Project" value={slug || ''} options={options}
                    onChange={(e) => choose(e.target.value)} />
          )}
          <Tag tone="done">Gitea running{st.version ? ` ${st.version}` : ''}</Tag>
          <span className="grow" />
          <Ext href={repo?.url || st.url}>Open in Gitea</Ext>
        </div>
      )}
      {routeSlug && ready && slug !== routeSlug && (
        <p className="warn">There is no project called {routeSlug}.</p>
      )}
      {note && <p className="dim small git-note">{note}</p>}
      {body}
      {st?.configured && st.running && (
        <Card title="Accounts" headingLevel={2}>
          <p className="dim small git-note">
            Gitea is private: people sign in at <Ext href={st.url}>{st.url}</Ext>. Everyone here who
            can sign in reads every repo.
          </p>
          <GiteaAccounts st={st} onChange={() => setRev((n) => n + 1)} />
        </Card>
      )}
    </Page>
  )
}
