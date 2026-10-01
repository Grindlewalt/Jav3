// The Tools page's built-in list, grouped the way the model sees it.
//
// GET /api/tools gives each built-in row (one per tools/<name>/ folder) its
// `section`, its `action` inside the merged tool it folds into (`merged_into`,
// null for a tool the model sees by its own name), `core`, `internal` and
// `gating`; rows arrive in the model's own order and `sections` describes the
// sections in use. This file only groups: sections -> tools -> actions, where a
// merged tool is ONE tool with an action per folder. Pure, so it is node-tested
// (src/__tests__/toolGroups.test.mjs).

// The first sentence of a description, cut at a word near `max` when it runs on.
export function oneLine(text, max = 140) {
  const s = String(text || '').replace(/\s+/g, ' ').trim()
  let stop = -1
  const end = /[.!?](?:\s+|$)/g
  for (let m = end.exec(s); m; m = end.exec(s)) {
    if (/\b(e\.g|i\.e|etc|vs)\.$/i.test(s.slice(0, m.index + 1))) continue   // not a sentence end
    stop = m.index
    break
  }
  const first = stop > 0 ? s.slice(0, stop + 1) : s
  if (first.length <= max) return first
  const cut = first.slice(0, max)
  return `${cut.slice(0, Math.max(cut.lastIndexOf(' '), max / 2))}…`
}

// Every word of the query must be somewhere in the row: its folder (old) name,
// its action, the merged tool, the section, what it says it does, its gating.
// "browser click" and "browser_click" both find browser(action=click).
export function rowMatches(row, query) {
  const words = String(query || '').toLowerCase().split(/[\s_]+/).filter(Boolean)
  if (!words.length) return true
  const hay = [row.name, row.action, row.merged_into, row.section, row.description,
    row.when_to_use, ...(row.gating || [])].join(' ').toLowerCase().replace(/_/g, ' ')
  return words.every((w) => hay.includes(w))
}

// Labels every action of a merged tool has go on the tool; the rest stay on the action.
function splitGating(rows) {
  const first = rows[0]?.gating || []
  const common = first.filter((g) => rows.every((r) => (r.gating || []).includes(g)))
  return { common, rest: rows.map((r) => (r.gating || []).filter((g) => !common.includes(g))) }
}

// rows: the built-in rows. sections: GET /api/tools `sections`.
// opts.internal: also show the harness-only tools. opts.query: filter by a search.
// -> { sections: [{ name, about, tools: [{ name, merged, core, about, gating,
//      offered, reason, actions: [{ row, gating }] }] }],
//      tools, actions        what the page lists with no search (the heading),
//      shownTools, shownActions   what the search leaves,
//      internalTools, internalActions   what the toggle would add }
export function groupTools(rows, sections = [], opts = {}) {
  const { internal = false, query = '' } = opts
  const secMeta = new Map((sections || []).map((s) => [s.name, s]))
  const count = (list) => {
    const units = new Set(list.map((r) => r.merged_into || r.name))
    return { tools: units.size, actions: list.length }
  }
  const all = rows || []
  const visible = internal ? all : all.filter((r) => !r.internal)
  const shown = visible.filter((r) => rowMatches(r, query))
  const total = count(visible)
  const withInternal = count(all)
  const left = count(shown)

  const order = []                      // sections, in the order rows first name them
  const bySection = new Map()
  for (const r of shown) {
    const sec = r.section || 'other'
    if (!bySection.has(sec)) { bySection.set(sec, new Map()); order.push(sec) }
    const units = bySection.get(sec)
    const key = r.merged_into || r.name
    if (!units.has(key)) units.set(key, [])
    units.get(key).push(r)
  }
  const out = order.map((sec) => {
    const meta = secMeta.get(sec) || {}
    const tools = [...bySection.get(sec)].map(([name, list]) => {
      const merged = !!list[0].merged_into
      const { common, rest } = splitGating(list)
      const why = new Set(list.map((r) => r.reason || ''))
      // one reason for the whole merged tool (the extension is not connected)
      // is said once on the tool, not under every action
      const reason = merged && why.size === 1 ? [...why][0] : ''
      return {
        name, merged, core: list.some((r) => r.core),
        about: merged ? (meta.merged || '') : oneLine(list[0].description),
        // a standalone tool carries all its labels; a merged one the shared ones
        gating: merged ? common : list[0].gating || [],
        offered: list.filter((r) => r.offered).length, reason,
        actions: list.map((row, i) => ({ row, gating: merged ? rest[i] : [] })),
      }
    })
    return { name: sec, about: meta.about || '', tools }
  })
  return {
    sections: out,
    tools: total.tools, actions: total.actions,
    shownTools: left.tools, shownActions: left.actions,
    internalTools: withInternal.tools - total.tools,
    internalActions: withInternal.actions - total.actions,
  }
}

// "26 tools · 81 actions"
export function heading(g) {
  const n = (k, one, many) => `${k} ${k === 1 ? one : many}`
  return `${n(g.tools, 'tool', 'tools')} · ${n(g.actions, 'action', 'actions')}`
}
