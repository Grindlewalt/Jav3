// The pure half of the slash commands: no React, no fetch, so node can test it
// (src/slash/__tests__). Mirrors the terminal client's rules:
//   "/"            opens the command list
//   "/na"          filters it
//   "/name arg"    the command's argument (and its options, if it has any)
//   "//text"       sends "/text" as an ordinary message
// Only a LEADING slash followed by a plain word is a command: "a/b", "see /x"
// and "/usr/bin/env" are messages.

const NAME = /^[a-z][a-z0-9-]*$/i

// null = not a command (send it as a message).
// {escaped, text}      "//text" — send `text` ("/text") as a message
// {name, arg, spaced}  a command; `spaced` = the name is finished (a space
//                      followed it), so the popup moves on to the argument
export function parseInput(text) {
  if (typeof text !== 'string' || !text.startsWith('/')) return null
  if (text.startsWith('//')) return { escaped: true, text: text.slice(1) }
  const m = /^\/(\S*)(?:\s([\s\S]*))?$/.exec(text)
  if (!m) return null
  const name = m[1]
  if (name !== '' && !NAME.test(name)) return null
  const spaced = m[2] !== undefined
  return { name: name.toLowerCase(), arg: spaced ? m[2].trim() : '', spaced }
}

// name or alias -> command
export function indexCommands(list) {
  const by = new Map()
  for (const c of list) {
    by.set(c.name, c)
    for (const a of c.aliases || []) by.set(a, c)
  }
  return by
}

export function findCommand(list, name) {
  return indexCommands(list).get((name || '').toLowerCase()) || null
}

// The popup's command rows for a partial name, best first: the name starts
// with it, then an alias does (shown as "via"), then the name contains it.
// Each command appears once.
export function matchCommands(list, query) {
  const q = (query || '').toLowerCase()
  const rows = []
  for (const c of list) {
    let rank = null
    let via = null
    if (c.name.startsWith(q)) rank = c.name === q ? 0 : 1
    else {
      const a = (c.aliases || []).find((x) => x.startsWith(q))
      if (a) { rank = a === q ? 0 : 2; via = a }
      else if (q.length > 1 && c.name.includes(q)) rank = 3   // one letter: prefixes only
    }
    if (rank !== null) rows.push({ cmd: c, via, rank })
  }
  // stable within a rank: the registry's order is the help order
  return rows.sort((x, y) => x.rank - y.rank)
}

// Argument options narrowed by what has been typed: value/label starting with
// it first, then containing it. Empty query = all of them.
export function filterOptions(options, query) {
  const q = (query || '').toLowerCase()
  if (!q) return options || []
  const head = []
  const rest = []
  for (const o of options || []) {
    const v = String(o.value).toLowerCase()
    const l = String(o.label || '').toLowerCase()
    if (v.startsWith(q) || l.startsWith(q)) head.push(o)
    else if (v.includes(q) || l.includes(q) || String(o.meta || '').toLowerCase().includes(q))
      rest.push(o)
  }
  return [...head, ...rest]
}

// What Tab (or Enter on a row that isn't final) puts in the composer.
export function completion(row, cmd) {
  if (row.kind === 'option') return `/${cmd.name} ${row.value}`
  const c = row.cmd
  return c.args || c.usage ? `/${c.name} ` : `/${c.name}`
}

// A chat's messages as markdown — the terminal's transcript_markdown.
export function transcriptMarkdown(title, messages) {
  const parts = [`# ${title}\n`]
  for (const m of messages || []) {
    if (m.role !== 'user' && m.role !== 'assistant') continue
    parts.push(`## ${m.role === 'user' ? 'You' : 'Jav3'}\n`)
    for (const a of m.activity || [])
      parts.push(`- ${a.name}${a.ok === false ? ' (failed)' : ''}`)
    parts.push(`${m.content || ''}\n`)
  }
  return parts.join('\n')
}
