// The Git page's pure parts (pages/Git.jsx): the words for the push status, the
// +/- of a pull request, the date column, which Access rows can be changed, which
// project opens first, and a diff cut into lines that know their colour. No React
// and no fetch, so node can test them (src/__tests__/gitPage.test.mjs).

const s7 = (sha) => String(sha || '').slice(0, 7)
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

// The Push status row: a Tag tone, a short state word, one sentence, and the
// button the state earns (null when there is nothing to do or nothing safe to
// offer). `s` is GET /api/gitea/repos/{slug}/sync.
export function syncView(s) {
  if (!s) return null
  const host = s7(s.host)
  const gitea = s7(s.gitea)
  switch (s.state) {
    case 'in_sync':
      return { tone: 'done', word: 'in sync', text: `host main ${host} = Gitea main`, action: null }
    case 'host_ahead':
      return {
        tone: 'pending', word: 'host ahead',
        text: gitea
          ? `host main ${host} is ${plural(s.ahead, 'commit', 'commits')} ahead of Gitea main ${gitea}`
          : `host main ${host}; Gitea has no main yet`,
        action: {
          label: 'Push main',
          title: 'Pushes the host’s main to Gitea as you. Main is protected there: only you can.',
        },
      }
    case 'gitea_ahead':
      return {
        tone: 'pending', word: 'Gitea ahead',
        text: host
          ? `Gitea main ${gitea} is ${plural(s.behind, 'commit', 'commits')} ahead of host main ${host}`
          : `Gitea main ${gitea}; the host has no commits yet`,
        action: {
          label: 'Update host',
          title: 'Moves the host’s main up to Gitea’s. Your working files are not touched.',
        },
      }
    case 'diverged':
      return {
        tone: 'error', word: 'diverged',
        text: `host main ${host} and Gitea main ${gitea} have each moved on `
          + `(${s.ahead} ahead, ${s.behind} behind). They have to be reconciled by hand.`,
        action: null,
      }
    case 'empty':
      return { tone: '', word: 'no commits', text: 'neither the host nor Gitea has a commit yet', action: null }
    default:
      return { tone: 'error', word: 'unknown', text: s.error || 'could not compare the two mains', action: null }
  }
}

// "+120 −4", or '' when the request row had no stat line to read.
export function diffStat(p) {
  if (!p || p.added == null) return ''
  return `+${p.added} −${p.removed ?? 0}`
}

// "3 files", "1 file", or ''.
export function filesText(p) {
  return p && p.files != null ? plural(p.files, 'file', 'files') : ''
}

// The date column. Gitea sends the author's own offset (2026-10-01T15:52:00+01:00),
// so the first ten characters are the date the author saw: no timezone sum here.
export function shortDate(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(iso || ''))
  return m ? m[1] : ''
}

// An Access row is editable when it is a person: not the owner, not the agent
// bot, and at a level this page can set (a collaborator made `admin` in Gitea
// itself is shown, not changed).
export const ACCESS_LEVELS = ['read', 'write']
export function accessEditable(row) {
  if (!row || row.owner || row.bot) return false
  return row.permission === 'none' || ACCESS_LEVELS.includes(row.permission)
}

// Which project opens first: the one in the address, then the last one looked
// at, then one with a pull request waiting, then any with a repo, then any.
// `projects` are {slug}; `repos` are {slug, open_prs} from /api/gitea/repos.
export function pickSlug({ route, remembered, projects = [], repos = [] }) {
  const slugs = projects.map((p) => p.slug)
  const have = new Set(slugs)
  for (const s of repos) have.add(s.slug)
  if (route && have.has(route)) return route
  if (remembered && have.has(remembered)) return remembered
  const waiting = repos.find((r) => r.open_prs > 0)
  if (waiting) return waiting.slug
  if (repos.length) return repos[0].slug
  return slugs[0] || null
}

// The project select's options: every Jav3 project, marked when it has no repo
// yet, then any repo Gitea holds that is not a project (a deleted one).
export function projectOptions(projects = [], repos = []) {
  const withRepo = new Set(repos.map((r) => r.slug))
  const out = projects.map((p) => ({
    value: p.slug, label: withRepo.has(p.slug) ? (p.name || p.slug) : `${p.name || p.slug} (no repo yet)`,
  }))
  const known = new Set(projects.map((p) => p.slug))
  for (const r of repos) if (!known.has(r.slug)) out.push({ value: r.slug, label: `${r.slug} (not a project)` })
  return out
}

// The note under the header when Gitea's links point at the host's LAN address.
export function urlNote(st) {
  if (!st || !st.configured || st.url_configured !== false) return ''
  return `Links open Gitea at ${st.url}, the host’s address on your network. To open it from `
    + 'elsewhere, set JARVIS_GITEA_URL to the address you reach it at (in '
    + '~/.config/jarvis/env) and restart Jav3.'
}

// A unified diff cut into lines with the class the view paints: file headers,
// hunks, additions, removals, and context.
export function diffLines(text) {
  return String(text || '').split('\n').map((t) => {
    let cls = ''
    // `--- a/x` and `+++ b/x`, not a removed line that happens to start `-- `
    if (t.startsWith('diff --git') || t.startsWith('index ')
        || /^(---|\+\+\+) (a\/|b\/|\/dev\/null)/.test(t)) cls = 'file'
    else if (t.startsWith('@@')) cls = 'hunk'
    else if (t.startsWith('+')) cls = 'add'
    else if (t.startsWith('-')) cls = 'del'
    return { cls, text: t || ' ' }
  })
}
