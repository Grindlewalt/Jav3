// The pure half of /status and /details: the rows of the status card and the
// tool-row toggle, with no fetch and no React, so node can test them
// (src/slash/__tests__/status.test.mjs). The terminal client's /status is a
// table of the same kind (clients/jav3cli/jav3, c_status); this one leaves out
// what only a terminal has (the working directory, its theme, pending !output)
// and adds what only the server side has (the boxes).

const num = (n) => Number(n || 0).toLocaleString('en-US')

// "41,000 in · 1,200 out · $0.0040 · 4 calls"
export function usageLine(info) {
  if (!info) return ''
  const parts = [`${num(info.input_tokens)} in`, `${num(info.output_tokens)} out`,
                 `$${Number(info.cost_usd || 0).toFixed(4)}`]
  if (info.calls) parts.push(`${info.calls} call${info.calls === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

// "41,000 of 100,000 tokens (41%)"; just the used count when the window is unknown
export function contextLine(ctx) {
  if (!ctx || !ctx.used) return ''
  if (!ctx.window) return `${num(ctx.used)} tokens`
  return `${num(ctx.used)} of ${num(ctx.window)} tokens (${Math.round((100 * ctx.used) / ctx.window)}%)`
}

// "a.py, b.py and 2 more" — the files this chat wrote or edited
export function filesLine(files) {
  const paths = (files || []).map((f) => f.path).filter(Boolean)
  if (!paths.length) return ''
  const shown = paths.slice(0, 3).join(', ')
  return paths.length > 3 ? `${shown} and ${paths.length - 3} more` : shown
}

// listBoxes()'s answer as one line: "2 of 3 running: shared, p-demo · RAM 1536 / 6144 MB".
// Empty when the server said nothing about boxes.
export function boxesLine(r) {
  const boxes = r?.boxes
  if (!Array.isArray(boxes)) return ''
  if (r.legacy || !r.enabled) {
    const shared = boxes.find((b) => b.kind === 'shared') || boxes[0]
    return shared ? `shared VM ${shared.state || 'unknown'}` : ''
  }
  const running = boxes.filter((b) => b.state === 'running')
  const names = running.slice(0, 3).map((b) => b.id).join(', ')
  let s = `${running.length} of ${boxes.length} running${names ? `: ${names}` : ''}`
  if (running.length > 3) s += ` and ${running.length - 3} more`
  const b = r.budget
  if (b && b.ram_mb_cap) s += ` · RAM ${num(b.ram_mb_used)} / ${num(b.ram_mb_cap)} MB`
  return s
}

// The card's rows, [label, value] in reading order. Rows with nothing to say
// are left out, except the five that always answer: server, who, chat, model,
// project.
//   origin     location.origin
//   user       /api/auth/me's username
//   chat       {id, title} | null   (saved chat)
//   temporary  the composer's temporary switch
//   model      {label, id}
//   project    a label ("demo", "none", "follows the loaded project")
//   info       /api/conversations/{id}/info (usage, context, files, agent)
//   boxes      listBoxes()'s answer
export function statusRows({ origin, user, chat, temporary, model, project, info, boxes }) {
  const rows = [
    ['server', origin || '(unknown)'],
    ['signed in as', user || '(unknown)'],
    ['chat', chat ? `#${chat.id}${chat.title ? ` ${chat.title}` : ''}`
      : temporary ? 'new temporary chat (nothing is saved)' : 'new chat (not saved yet)'],
    ['model', model ? (model.label && model.label !== model.id
      ? `${model.label} (${model.id})` : model.id || model.label || '(none)') : '(none)'],
    ['project', project || 'none'],
  ]
  if (info?.agent) rows.push(['agent', info.agent])
  if (chat && temporary) rows.push(['temporary', 'on'])
  const usage = usageLine(info)
  if (usage) rows.push(['usage', usage])
  const ctx = contextLine(info?.context)
  if (ctx) rows.push(['context', ctx])
  const files = filesLine(info?.files)
  if (files) rows.push(['files', files])
  const b = boxesLine(boxes)
  if (b) rows.push(['boxes', b])
  return rows
}

// /details: flip every finished tool row of a chat between its one line and its
// args and result. The rows keep their own open/closed state (ToolActivity.jsx),
// so this clicks their headers, the way a hand would: if any is closed it opens
// the closed ones, otherwise it closes the open ones. `root` is the element that
// holds the messages. Returns {opened, closed}.
export function toggleToolRows(root) {
  const heads = [...root.querySelectorAll('.tool-row-head')]
    .filter((h) => h.querySelector('.chev'))               // a running row has no chevron
  const isOpen = (h) => !!h.querySelector('.chev.open')
  const shut = heads.filter((h) => !isOpen(h))
  const open = heads.filter(isOpen)
  const expand = shut.length > 0
  for (const h of expand ? shut : open) h.click()
  return { opened: expand ? shut.length : 0, closed: expand ? 0 : open.length }
}
