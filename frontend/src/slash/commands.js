// The web composer's slash commands — the terminal client's set
// (clients/jav3cli/jav3, _commands()) where it makes sense in a browser.
//
// Each command: { name, aliases, usage, help, busyOk, args?, run }.
//   args(env)      -> [{value, label, meta}] (or a promise of one): the
//                     argument's options, filtered as the operator types
//   run(arg, env)  -> optional string, shown under the composer
//
// Every command goes through a path the page already has — the host's own
// functions (the same ones its buttons call) or the same endpoint the
// component that owns the feature calls. Nothing here is a new API.
//
// env: { host, work, navigate, say, later, afterRender, setNextChat, openDump }
//   host   what Chat.jsx passes to useSlash (see useSlash.jsx)
//   work   WorkContext: { openWindow, closeWindow, project }

import { api } from '../api.js'
import { loadModel, modelCaption, modelOption, setModel } from '../modelInfo.js'
import { transcriptMarkdown } from './parse.js'

// the Work page's window (card) types — the board's PANEL_TYPES
export const WINDOW_TYPES = [
  ['journal', 'Journal — project.md'],
  ['editor', 'Editor — text & markdown'],
  ['renderer', 'Renderer — html / pdf / images'],
  ['organizer', 'File organizer'],
  ['run', 'Run — python sandbox'],
  ['context', 'Context files — load into Jav3'],
  ['agent', 'Run an agent'],
  ['research', 'Research bots — live'],
  ['git', 'Git — review, approve, push'],
  ['grants', 'Secrets — key grants for this project'],
  ['terminal', 'Terminal — shell in the guest VM'],
  ['taskboard', 'Task board — goal / plan / runs'],
  ['todo', 'To-dos'],
  ['plan', 'Plan — dump, checklist, agents'],
]

const SECURITY_TABS = [
  ['queue', 'approvals and alerts waiting on you'],
  ['network', 'egress and host approvals'],
  ['logs', 'the security log'],
  ['secrets', 'secrets and key grants'],
]

const need = (cond, msg) => { if (!cond) throw new Error(msg) }

function currentConvo(host) {
  return host.conversations.find((c) => c.id === host.conversationId) || null
}

function projectOptions(host, extra = []) {
  return [
    ...extra,
    ...host.projects.map((p) => ({ value: p.slug, label: p.name, meta: p.slug })),
  ]
}

function lastOf(host, role) {
  const m = [...host.messages].reverse().find((x) => x.role === role && !x.streaming)
  return m?.content || ''
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')   // no secure context
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

let agentsCache = null
async function agentOptions() {
  const r = await api('/api/agents')
  agentsCache = r.agents || []
  return agentsCache.map((a) => ({ value: a.slug, label: a.name || a.slug,
                                   meta: a.description || a.slug }))
}

export const COMMANDS = [
  {
    name: 'help', busyOk: true,
    help: 'commands and keys',
    run: (_, env) => {
      env.host.setInput('/')
      return '↑↓ move · Tab completes · Enter runs · Esc closes · //text sends a message '
        + 'that starts with a slash · Enter while a turn runs messages the agent'
    },
  },
  {
    name: 'new', aliases: ['clear'],
    help: 'start a fresh chat',
    run: (_, env) => { env.host.newChat() },
  },
  {
    name: 'sessions', aliases: ['resume', 'history'], usage: '[chat]',
    help: 'switch to another chat',
    args: (env) => env.host.conversations.map((c) => ({
      value: String(c.id),
      label: c.summary || `Chat #${c.id}`,
      meta: [c.starred ? '★' : '', c.project_slug || '', `#${c.id}`]
        .filter(Boolean).join(' · '),
    })),
    run: (arg, env) => {
      if (!arg) { env.host.openList(); return }
      const id = Number(arg.replace(/^#/, ''))
      const hit = env.host.conversations.find((c) => c.id === id)
        || env.host.conversations.find((c) =>
          (c.summary || '').toLowerCase().includes(arg.toLowerCase()))
      need(hit, `no chat matches “${arg}”`)
      env.host.openChat(hit.id)
    },
  },
  {
    name: 'models', aliases: ['model'], usage: '[provider/id]', busyOk: true,
    help: 'pick the model for new turns',
    args: async () => {
      const m = await loadModel()
      return (m?.models || []).map((x) => ({
        value: x.id, label: x.label || x.id,
        meta: [x.id === m.active ? 'current' : '', modelCaption(x)].filter(Boolean).join(' · '),
      }))
    },
    run: async (arg) => {
      const m = await loadModel()
      if (!arg) return `model: ${modelOption(m, m?.active).label || 'none'}`
      const x = (m?.models || []).find((y) => y.id === arg)
        || (m?.models || []).find((y) => y.id.endsWith(`/${arg}`)
                                    || (y.label || '').toLowerCase() === arg.toLowerCase())
      need(x, `no enabled model “${arg}” — Settings › providers turns models on`)
      await setModel(x.id)
      return `model: ${x.label || x.id}`
    },
  },
  {
    name: 'provider', aliases: ['providers'], busyOk: true,
    help: 'add a provider’s API key / base URL (Settings)',
    run: (_, env) => {
      env.navigate('/settings')
      // the Settings page's provider card carries id="providers"
      setTimeout(() => document.getElementById('providers')
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 120)
    },
  },
  {
    name: 'agent', usage: '[slug]',
    help: 'start a new chat as an agent',
    args: agentOptions,
    run: async (arg, env) => {
      need(arg, 'which agent? /agent <slug>')
      const list = agentsCache || (await agentOptions(), agentsCache)
      const a = list.find((x) => x.slug === arg)
        || list.find((x) => (x.name || '').toLowerCase() === arg.toLowerCase())
      need(a, `no agent “${arg}”`)
      env.host.newChat()
      env.setNextChat({ agent: a.slug, label: `agent: ${a.name || a.slug}` })
      return `the next message starts a chat as ${a.name || a.slug}`
    },
  },
  {
    name: 'agents', aliases: ['agents-view'], busyOk: true,
    help: 'the Agents page',
    run: (_, env) => { env.navigate('/agents') },
  },
  {
    name: 'project', usage: '[slug|none|follow|create <name>]',
    help: 'pin this chat to a project (or make a new one)',
    args: (env) => projectOptions(env.host, [
      { value: 'follow', label: 'follow', meta: 'use whatever project is loaded' },
      { value: 'none', label: 'none', meta: 'no project — files go to this chat’s artifacts' },
      { value: 'create ', label: 'create <name>', meta: 'make a new project and pin to it' },
    ]),
    run: async (arg, env) => {
      const h = env.host
      if (!arg) {
        const c = currentConvo(h)
        return c?.project_slug ? `project: ${c.project_slug}`
          : `project: ${h.pendingProject || (c?.project_locked ? 'none' : 'follow')}`
      }
      if (arg === 'none' || arg === 'follow') {
        await h.pickProject(arg, '')
        return `project: ${arg}`
      }
      const create = /^create\s+(.+)$/i.exec(arg)
      if (create) {
        const r = await api('/api/projects', {
          method: 'POST', body: JSON.stringify({ name: create[1].trim() }) })
        await h.refreshProjects()
        await h.pickProject('pin', r.slug)
        return `created ${r.slug} and pinned this chat to it`
      }
      need(h.projects.some((p) => p.slug === arg),
           `no project “${arg}” — /project create ${arg} makes it`)
      await h.pickProject('pin', arg)
      return `project: ${arg}`
    },
  },
  {
    name: 'temp',
    help: 'toggle temporary chats (nothing saved)',
    run: (_, env) => {
      const h = env.host
      // only a chat that doesn't exist yet can be temporary — like the switch
      if (h.conversationId) {
        h.newChat()
        env.afterRender((host) => host.setTemporary(true))
        return 'new temporary chat — nothing is saved'
      }
      h.setTemporary(!h.temporary)
      return h.temporary ? 'temporary chat: off' : 'temporary chat — nothing is saved'
    },
  },
  {
    name: 'local', busyOk: true,
    help: 'local chats (files and shell on your machine)',
    run: () => 'only in the jav3 terminal client — it runs the tools where it is',
  },
  {
    name: 'rename', usage: '[title]',
    help: 'rename this chat',
    run: async (arg, env) => {
      const h = env.host
      need(h.conversationId, 'no saved chat to rename')
      if (!arg) { await h.rename(); return }
      await api(`/api/conversations/${h.conversationId}`, {
        method: 'PATCH', body: JSON.stringify({ title: arg }) })
      await h.refresh()
      return `renamed: ${arg}`
    },
  },
  {
    name: 'star',
    help: 'star or unstar this chat',
    run: async (_, env) => {
      const h = env.host
      const c = currentConvo(h)
      need(c, 'no saved chat to star')
      // ChatGroups' star menu item: the same PATCH
      await api(`/api/conversations/${c.id}`, {
        method: 'PATCH', body: JSON.stringify({ starred: !c.starred }) })
      await h.refresh()
      return c.starred ? 'unstarred' : 'starred'
    },
  },
  {
    name: 'delete',
    help: 'delete this chat (asks first)',
    run: async (_, env) => {
      need(env.host.conversationId, 'no saved chat to delete')
      await env.host.deleteChat()
    },
  },
  {
    name: 'retry',
    help: 'send your last message again',
    run: (_, env) => {
      const t = lastOf(env.host, 'user')
      need(t, 'nothing to retry')
      env.host.send(t)
    },
  },
  {
    name: 'stop', busyOk: true,
    help: 'stop the running turn',
    run: (_, env) => {
      if (!env.host.busy) return 'nothing is running'
      env.markStop()
      env.host.stop()
    },
  },
  {
    name: 'copy', busyOk: true,
    help: 'copy the last reply',
    run: async (_, env) => {
      const t = lastOf(env.host, 'assistant')
      need(t, 'no reply to copy yet')
      await copyText(t)
      return 'copied the last reply'
    },
  },
  {
    name: 'export', busyOk: true, usage: '[file]',
    help: 'save this chat as markdown',
    run: async (arg, env) => {
      const h = env.host
      const c = currentConvo(h)
      const title = c?.summary || (h.conversationId ? `Jav3 chat #${h.conversationId}` : 'Jav3 chat')
      // the saved transcript when there is one (what the terminal exports),
      // else what is on screen (a temporary chat has nothing saved)
      const msgs = h.conversationId
        ? (await api(`/api/conversations/${h.conversationId}/messages`)).messages
        : h.messages
      need(msgs?.length, 'nothing to export')
      const name = arg || `jav3-${h.conversationId || 'chat'}.md`
      download(name.endsWith('.md') ? name : `${name}.md`, transcriptMarkdown(title, msgs))
      return `saved ${name}`
    },
  },
  {
    name: 'orchestration', aliases: ['orchestrate'], usage: '[project]',
    help: 'brain-dump a problem; an orchestrator sends agents at it',
    args: (env) => projectOptions(env.host),
    run: (arg, env) => {
      const h = env.host
      const slug = arg || currentConvo(h)?.project_slug || h.pendingProject || h.active
      need(slug, 'which project? /orchestration <project>')
      need(h.projects.some((p) => p.slug === slug), `no project “${slug}”`)
      env.openDump(slug)
    },
  },
  {
    name: 'security', aliases: ['review'], usage: '[queue|network|logs|secrets]', busyOk: true,
    help: 'the Security area: queue, network, logs, secrets',
    args: () => SECURITY_TABS.map(([value, meta]) => ({ value, label: value, meta })),
    run: (arg, env) => {
      const tab = (arg || 'queue').toLowerCase()
      need(SECURITY_TABS.some(([t]) => t === tab), `no Security tab “${arg}”`)
      env.navigate(tab === 'queue' ? '/security' : `/security/${tab}`)
    },
  },
  {
    name: 'settings', busyOk: true,
    help: 'the Settings page',
    run: (_, env) => { env.navigate('/settings') },
  },
  {
    name: 'memory', busyOk: true,
    help: 'the Memory page',
    run: (_, env) => { env.navigate('/memory') },
  },
  {
    name: 'window', aliases: ['open'], usage: '<card>', busyOk: true,
    help: 'open one of the project’s cards beside the chat',
    args: () => WINDOW_TYPES.map(([value, label]) => ({ value, label: value, meta: label })),
    run: (arg, env) => {
      need(arg, 'which card? /window <card>')
      const t = WINDOW_TYPES.find(([v]) => v === arg.toLowerCase())
      need(t, `no card “${arg}”`)
      const h = env.host
      const slug = env.work.project || currentConvo(h)?.project_slug || h.pendingProject || h.active
      env.work.openWindow(t[0], slug ? { slug } : {})
    },
  },
  {
    name: 'close', busyOk: true,
    help: 'close the focused window',
    // no id: the Work layout closes the focused one
    run: (_, env) => { env.work.closeWindow(env.work.focused ?? undefined) },
  },
]
