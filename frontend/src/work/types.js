// ---- window registry: add a capability = one entry here + one case in windows.jsx ----
// These are the old project board's cards (and the backend's workspace_panel
// tool names them the same way: backend/gui.py PANEL_SIZES). `title` is the
// window header, `label` the + menu row.
export const WINDOW_TYPES = {
  plan: { title: 'Plan', label: 'Plan — dump, checklist, agents' },
  board: { title: 'Task board', label: 'Task board — goal / plan / runs' },
  git: { title: 'Git', label: 'Git — review, approve, push' },
  terminal: { title: 'Terminal', label: 'Terminal — shell in the guest VM' },
  journal: { title: 'Journal', label: 'Journal — project.md' },
  editor: { title: 'Editor', label: 'Editor — text & markdown' },
  renderer: { title: 'Renderer', label: 'Renderer — html / pdf / images' },
  organizer: { title: 'Files', label: 'File organizer' },
  run: { title: 'Run', label: 'Run — python sandbox' },
  todos: { title: 'To-dos', label: 'To-dos' },
  context: { title: 'Context', label: 'Context files — load into Jav3' },
  agent: { title: 'Agent', label: 'Run an agent' },
  research: { title: 'Research', label: 'Research bots — live' },
  chat: { title: 'Chat', label: 'Chat — Jav3 or an agent, on this project' },
  review: { title: 'Security', label: 'Security — approvals & alerts' },
  network: { title: 'Network', label: 'Network — egress & host approvals' },
  secrets: { title: 'Secrets', label: 'Secrets — key grants for this project' },
  vm: { title: 'Project VM', label: 'Project VM — context, disk, persist' },
}

export const isWindowType = (t) => Object.prototype.hasOwnProperty.call(WINDOW_TYPES, t)

// Other spellings of the same cards (the slash commands' names), so either
// works in openWindow.
export const ALIASES = { todo: 'todos', taskboard: 'board', grants: 'secrets' }
