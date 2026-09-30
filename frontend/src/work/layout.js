// The project board's split layout as plain data, and every change to it as a
// pure function: no React, no DOM, so it runs under `node` alone
// (see __tests__/layout.test.mjs).
//
// A tree. A leaf is one card:             { id, type }
// A split holds two or more children:     { id, dir, children, sizes }
//   dir    'row' (children side by side) or 'col' (stacked)
//   sizes  one fraction per child, summing to 1
//
// The board in memory:
//   { root, focus, maximized, minimized, panels: [{ id, type, state, ...extra }] }
// `minimized` lists cards collapsed to the strip above the windows: they keep
// their leaf (and so their size) in the tree, and geometry() lays the rest out
// as if they were not there. At most MAX_PANELS cards (addCard refuses more).
// A panel may carry `project`: a card of another project than the board's.
// `panels` is the same list the board has always saved (and the agent's
// workspace_panel tool edits server-side); the tree only says where each
// panel sits. A panel's `state` is its card's own state (setState), as before.
//
// Saved (PUT /api/projects/{slug}/layout):
//   { v: 2, panels, tree, focus, maximized, minimized }
// Each saved panel still carries x/y/w/h/z, computed from the tree at a
// nominal board size, so the backend's tool (which tiles and appends by those
// numbers) and an older client both keep working on a v2 file.

export const VERSION = 2
export const MAX_PANELS = 4
export const isSplit = (n) => !!(n && Array.isArray(n.children))

let seq = 0
export function makeId(prefix = 'p') {
  seq += 1
  return `${prefix}${Date.now().toString(36)}${seq.toString(36)}${Math.random().toString(36).slice(2, 5)}`
}

export const makeLeaf = (type, id = makeId('p')) => ({ id, type })

// Every leaf, in reading order (depth first).
export function leaves(root) {
  if (!root) return []
  if (!isSplit(root)) return [root]
  return root.children.flatMap(leaves)
}

export function findLeaf(root, id) {
  return leaves(root).find((l) => l.id === id) || null
}

export function findSplit(node, id) {
  if (!isSplit(node)) return null
  if (node.id === id) return node
  for (const c of node.children) {
    const hit = findSplit(c, id)
    if (hit) return hit
  }
  return null
}

// Tidy a tree: a split with one child becomes that child, a split inside a
// split of the same direction is flattened into it, sizes are renormalised.
export function normalize(node) {
  if (!node) return null
  if (!isSplit(node)) return node
  const children = []
  let sizes = []
  node.children.forEach((c, i) => {
    const n = normalize(c)
    if (!n) return
    const s = node.sizes?.[i] > 0 ? node.sizes[i] : 1 / node.children.length
    if (isSplit(n) && n.dir === node.dir) {
      n.children.forEach((cc, j) => { children.push(cc); sizes.push(s * n.sizes[j]) })
    } else {
      children.push(n)
      sizes.push(s)
    }
  })
  if (!children.length) return null
  if (children.length === 1) return children[0]
  const total = sizes.reduce((a, b) => a + b, 0) || 1
  sizes = sizes.map((s) => s / total)
  return { id: node.id, dir: node.dir, children, sizes }
}

// Put `leaf` beside the card `targetId`: dir 'row' = to its right (left with
// `before`), 'col' = below it (above with `before`). The two share the space
// the target had. With no tree the leaf becomes the tree; with an unknown
// target it goes beside the last card.
export function split(root, targetId, leaf, dir = 'row', before = false) {
  if (!root) return leaf
  const target = findLeaf(root, targetId) ? targetId : leaves(root).at(-1).id
  const pair = (node) => ({
    id: makeId('s'), dir, sizes: [0.5, 0.5],
    children: before ? [leaf, node] : [node, leaf],
  })
  const rec = (node) => {
    if (!isSplit(node)) return node.id === target ? pair(node) : node
    const i = node.children.findIndex((c) => !isSplit(c) && c.id === target)
    if (i >= 0 && node.dir === dir) {
      const half = node.sizes[i] / 2
      const children = [...node.children]
      const sizes = [...node.sizes]
      children.splice(before ? i : i + 1, 0, leaf)
      sizes.splice(i, 1, half, half)
      return { ...node, children, sizes }
    }
    return { ...node, children: node.children.map(rec) }
  }
  return normalize(rec(root))
}

// Remove a card. Its space goes to the neighbour before it (or after it, if
// it was first). Closing the last card gives null.
export function close(root, id) {
  if (!root) return null
  const rec = (node) => {
    if (!isSplit(node)) return node.id === id ? null : node
    const i = node.children.findIndex((c) => !isSplit(c) && c.id === id)
    if (i >= 0) {
      const children = node.children.filter((_, j) => j !== i)
      const sizes = node.sizes.filter((_, j) => j !== i)
      sizes[i > 0 ? i - 1 : 0] += node.sizes[i]
      return { ...node, children, sizes }
    }
    return { ...node, children: node.children.map(rec) }
  }
  return normalize(rec(root))
}

// Swap the card `id` for `leaf` in the same place.
export function replace(root, id, leaf) {
  const rec = (node) => {
    if (!isSplit(node)) return node.id === id ? leaf : node
    return { ...node, children: node.children.map(rec) }
  }
  return root ? rec(root) : leaf
}

// Move the boundary after child `index` of split `splitId` by `delta` (a
// fraction of that split). Both children keep at least `min`. Call it on the
// tree as it was when the drag started, with the drag's total delta, so a
// drag never accumulates rounding.
// `next` is the child on the other side of the boundary: index + 1, unless
// minimized children between the two are out of the layout.
export function resize(root, splitId, index, delta, min = 0.05, next = index + 1) {
  const rec = (node) => {
    if (!isSplit(node)) return node
    if (node.id !== splitId) return { ...node, children: node.children.map(rec) }
    if (index < 0 || next <= index || next >= node.children.length) return node
    const a = node.sizes[index], b = node.sizes[next]
    const m = Math.min(min, (a + b) / 2)
    const na = Math.max(m, Math.min(a + b - m, a + delta))
    const sizes = [...node.sizes]
    sizes[index] = na
    sizes[next] = a + b - na
    return { ...node, sizes }
  }
  return root ? rec(root) : root
}

// Where everything is, in fractions of the board (0..1 on both axes):
//   leaves    { [id]: { x, y, w, h } }
//   dividers  [{ splitId, index, next, scale, dir, pos, split: {x,y,w,h} }]
// `hidden` (ids: the minimized cards) are left out and the rest share their
// space in proportion. A divider sits between children `index` and `next` of
// its split; `scale` is the share of the split those visible children hold,
// so a drag of d (a fraction of the split on screen) is resize(.., d * scale).
export function geometry(root, hidden = []) {
  const out = { leaves: {}, dividers: [] }
  const gone = new Set(hidden)
  const shown = (n) => (isSplit(n) ? n.children.some(shown) : !gone.has(n.id))
  const rec = (node, r) => {
    if (!isSplit(node)) { if (!gone.has(node.id)) out.leaves[node.id] = r; return }
    const row = node.dir === 'row'
    let at = row ? r.x : r.y
    const span = row ? r.w : r.h
    const vis = node.children.map((c, i) => i).filter((i) => shown(node.children[i]))
    const scale = vis.reduce((a, i) => a + node.sizes[i], 0) || 1
    vis.forEach((i, k) => {
      const len = span * node.sizes[i] / scale
      const c = node.children[i]
      rec(c, row ? { x: at, y: r.y, w: len, h: r.h } : { x: r.x, y: at, w: r.w, h: len })
      at += len
      if (k < vis.length - 1) {
        out.dividers.push({ splitId: node.id, index: i, next: vis[k + 1], scale,
                            dir: node.dir, pos: at, split: r })
      }
    })
  }
  if (root) rec(root, { x: 0, y: 0, w: 1, h: 1 })
  return out
}

// The card next to `id` across an edge ('left' | 'right' | 'up' | 'down').
export function neighbor(root, id, direction, hidden = []) {
  const { leaves: g } = geometry(root, hidden)
  const r = g[id]
  if (!r) return null
  const eps = 1e-6
  const horiz = direction === 'left' || direction === 'right'
  let best = null, bestD = Infinity
  for (const [cid, c] of Object.entries(g)) {
    if (cid === id) continue
    const overlap = horiz
      ? Math.min(r.y + r.h, c.y + c.h) - Math.max(r.y, c.y)
      : Math.min(r.x + r.w, c.x + c.w) - Math.max(r.x, c.x)
    if (overlap <= eps) continue
    let d
    if (direction === 'right') d = c.x - (r.x + r.w)
    else if (direction === 'left') d = r.x - (c.x + c.w)
    else if (direction === 'down') d = c.y - (r.y + r.h)
    else d = r.y - (c.y + c.h)
    if (d < -eps) continue
    const score = d * 1000 - overlap
    if (score < bestD) { bestD = score; best = cid }
  }
  return best
}

// ---- the board: tree + panels ------------------------------------------------

const col = (children, sizes) => normalize({ id: makeId('s'), dir: 'col', children, sizes })
const row = (children, sizes) => normalize({ id: makeId('s'), dir: 'row', children, sizes })

// The old board's default: chat · task board · git over network.
export function classicBoard() {
  const panels = [
    { id: 'p1', type: 'chat', state: {} }, { id: 'p2', type: 'board', state: {} },
    { id: 'p3', type: 'git', state: {} }, { id: 'p4', type: 'network', state: {} },
  ]
  const [a, b, c, d] = panels.map((p) => makeLeaf(p.type, p.id))
  return { root: row([a, b, col([c, d], [0.54, 0.46])], [0.36, 0.29, 0.35]),
           focus: 'p1', maximized: null, minimized: [], panels }
}

// A fresh project on the Work page: no windows. The chat is the main area,
// and the first window the operator opens splits in to its right.
export const emptyBoard = () => ({ root: null, focus: null, maximized: null, minimized: [], panels: [] })

// The fixed default every board got before it was fitted. A saved layout that
// is still exactly this was never arranged by anyone (the autosave persisted
// the default), so it loads as a fresh board.
const LEGACY_DEFAULT = [
  ['p1', 'chat', 16, 16, 460, 560], ['p2', 'board', 492, 16, 400, 560],
  ['p3', 'git', 908, 16, 540, 300], ['p4', 'network', 908, 332, 540, 244],
]
export const isLegacyDefault = (ps) => ps.length === LEGACY_DEFAULT.length &&
  LEGACY_DEFAULT.every(([id, type, x, y, w, h]) => ps.some((p) =>
    p.id === id && p.type === type && p.x === x && p.y === y && p.w === w && p.h === h))

const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d)

// An old free-floating board (panels with x/y/w/h) as a split tree: panels are
// grouped into columns by where their centres fall (a panel whose centre lies
// inside the running column's x-span joins it), each column stacks its
// panels top to bottom, and sizes follow the old widths and heights. The
// fitted three-column default comes back as chat | board | git over network.
export function treeFromLegacy(panels) {
  if (!panels.length) return null
  const boxes = panels.map((p) => {
    const x = num(p.x, 0), y = num(p.y, 0)
    const w = Math.max(1, num(p.w, 400)), h = Math.max(1, num(p.h, 400))
    return { id: p.id, type: p.type, x, y, w, h, cx: x + w / 2 }
  }).sort((a, b) => a.cx - b.cx || a.y - b.y)
  const cols = []
  for (const b of boxes) {
    const c = cols.at(-1)
    if (c && b.cx >= c.x0 && b.cx <= c.x1) {
      c.items.push(b)
      c.x0 = Math.min(c.x0, b.x); c.x1 = Math.max(c.x1, b.x + b.w)
    } else cols.push({ x0: b.x, x1: b.x + b.w, items: [b] })
  }
  const columns = cols.map((c) => {
    const items = c.items.sort((a, b) => a.y - b.y || a.x - b.x)
    return {
      node: col(items.map((i) => makeLeaf(i.type, i.id)), items.map((i) => i.h)),
      w: Math.max(...items.map((i) => i.w)),
    }
  })
  return row(columns.map((c) => c.node), columns.map((c) => c.w))
}

// Make the tree and the panel list agree: the panel list is the truth (the
// agent adds and removes panels server-side without knowing about the tree).
// Leaves with no panel go; a leaf's type is its panel's; panels with no leaf
// are split in to the right of the focused card (or the last one).
export function reconcile(root, panels, focus) {
  const byId = new Map(panels.map((p) => [p.id, p]))
  const clean = (n) => {
    if (!n) return null
    if (isSplit(n)) return { ...n, children: n.children.map(clean).filter(Boolean) }
    const p = byId.get(n.id)
    return p ? makeLeaf(p.type, p.id) : null
  }
  let r = normalize(clean(root))
  const placed = new Set(leaves(r).map((l) => l.id))
  let at = placed.has(focus) ? focus : leaves(r).at(-1)?.id
  for (const p of panels) {
    if (placed.has(p.id)) continue
    r = split(r, at, makeLeaf(p.type, p.id), 'row')
    placed.add(p.id)
    at = p.id
  }
  return r
}

// Parse what GET /layout returned. `known(type)` filters out card types this
// client no longer has. Returns { board, converted } where `converted` says
// the saved file was not a v2 tree (so the first save upgrades it).
//   fresh       the board for a project with nothing saved (and for the old
//               never-arranged default)
//   legacyDrop  card types an old free-floating board loses on conversion
//               (Work drops 'chat': the main chat replaced that card)
export function fromSaved(raw, known = () => true,
                          { fresh = emptyBoard, legacyDrop = [] } = {}) {
  const src = raw && typeof raw === 'object' ? raw : {}
  const seen = new Set()
  const panels = (Array.isArray(src.panels) ? src.panels : []).filter((p) => {
    if (!p || typeof p !== 'object' || typeof p.id !== 'string' || !known(p.type)) return false
    if (seen.has(p.id)) return false
    seen.add(p.id)
    return true
  }).map((p) => ({ ...p, state: p.state && typeof p.state === 'object' ? p.state : {} }))

  if (src.v === VERSION && src.tree !== undefined) {
    const root = reconcile(cleanTree(src.tree), panels, src.focus)
    const ids = new Set(leaves(root).map((l) => l.id))
    return {
      converted: false,
      board: {
        root,
        panels: panels.filter((p) => ids.has(p.id)),
        focus: ids.has(src.focus) ? src.focus : (leaves(root)[0]?.id ?? null),
        maximized: ids.has(src.maximized) ? src.maximized : null,
        minimized: (Array.isArray(src.minimized) ? src.minimized : [])
          .filter((id, i, a) => ids.has(id) && a.indexOf(id) === i),
      },
    }
  }
  if (!panels.length || isLegacyDefault(panels)) return { converted: true, board: fresh() }
  const kept = panels.filter((p) => !legacyDrop.includes(p.type))
  if (!kept.length) return { converted: true, board: emptyBoard() }
  const root = treeFromLegacy(kept)
  // the panel on top (highest z) was the one last touched: focus it
  const top = [...kept].sort((a, b) => num(b.z, 0) - num(a.z, 0))[0]
  return { converted: true, board: { root, panels: kept, focus: top.id, maximized: null, minimized: [] } }
}

function cleanTree(n) {
  if (!n || typeof n !== 'object') return null
  if (Array.isArray(n.children)) {
    if (n.dir !== 'row' && n.dir !== 'col') return null
    const kids = n.children.map(cleanTree)
    const keep = kids.map((k, i) => [k, Number(n.sizes?.[i]) > 0 ? Number(n.sizes[i]) : 1])
      .filter(([k]) => k)
    if (!keep.length) return null
    return { id: typeof n.id === 'string' ? n.id : makeId('s'), dir: n.dir,
             children: keep.map(([k]) => k), sizes: keep.map(([, s]) => s) }
  }
  return typeof n.id === 'string' ? makeLeaf(String(n.type || ''), n.id) : null
}

// The nominal board the saved x/y/w/h are computed against.
export const NOMINAL = { w: 1440, h: 880, pad: 16 }

export function toSaved(board) {
  const g = geometry(board.root).leaves
  const order = leaves(board.root).map((l) => l.id)
  const byId = new Map(board.panels.map((p) => [p.id, p]))
  const { w: W, h: H, pad } = NOMINAL
  const panels = order.filter((id) => byId.has(id)).map((id, i) => {
    const r = g[id]
    return {
      ...byId.get(id),
      x: Math.round(pad + r.x * W), y: Math.round(pad + r.y * H),
      w: Math.round(r.w * W), h: Math.round(r.h * H), z: i + 1,
    }
  })
  return {
    v: VERSION,
    panels,
    tree: board.root || null,
    focus: board.focus || null,
    maximized: board.maximized || null,
    minimized: board.minimized || [],
  }
}

// ---- board operations (return a new board) ----------------------------------

export const canAdd = (board) => !!board && board.panels.length < MAX_PANELS
const mins = (board) => board?.minimized || []

// Add a card of `type` beside `target` (default: the focused card). `extra`
// goes on the panel entry ({ project } for another project's card, { state }).
// A full board (MAX_PANELS) comes back unchanged.
export function addCard(board, type, target = board.focus, dir = 'row', extra = {}) {
  if (!canAdd(board)) return board
  const leaf = makeLeaf(type)
  const at = mins(board).includes(target) ? visibleIds(board).at(-1) : target
  return {
    ...board,
    root: split(board.root, at, leaf, dir),
    panels: [...board.panels, { id: leaf.id, type, state: {}, ...extra }],
    focus: leaf.id,
    maximized: null,
  }
}

// The direction a new card really splits `target` in. Asked for 'row' (beside
// it), it still goes below when the two halves would each be under `min` px on
// a stage `stageW` px wide: the second and third window used to squeeze to
// 90-190 px, a word per line with the header buttons clipped. Below is always
// allowed; 'col' comes back unchanged.
export const MIN_WIN_W = 320
export function fitDir(board, target, stageW, dir = 'row', min = MIN_WIN_W) {
  if (dir !== 'row' || !board?.root || !(stageW > 0)) return dir
  const at = mins(board).includes(target) ? visibleIds(board).at(-1) : target
  const id = findLeaf(board.root, at) ? at : leaves(board.root).at(-1)?.id
  const mine = id && geometry(board.root, mins(board)).leaves[id]
  return mine && (mine.w * stageW) / 2 < min ? 'col' : dir
}

// Turn the card `id` into a `type` card in the same place. It is a new card
// (new id, empty state): the old card's state belonged to the old type.
export function switchCard(board, id, type, extra = {}) {
  if (!findLeaf(board.root, id)) return board
  const leaf = makeLeaf(type)
  return {
    ...board,
    root: replace(board.root, id, leaf),
    panels: board.panels.map((p) => (p.id === id ? { id: leaf.id, type, state: {}, ...extra } : p)),
    minimized: mins(board).filter((m) => m !== id),
    focus: leaf.id,
    maximized: board.maximized === id ? leaf.id : board.maximized,
  }
}

// Close a card; focus moves to the one that took its space.
const nextFocus = (board, id) => {
  const h = mins(board)
  return neighbor(board.root, id, 'left', h) || neighbor(board.root, id, 'up', h)
    || neighbor(board.root, id, 'right', h) || neighbor(board.root, id, 'down', h)
}

export function closeCard(board, id) {
  if (!findLeaf(board.root, id)) return board
  const next = board.focus === id ? nextFocus(board, id) : board.focus
  const root = close(board.root, id)
  const minimized = mins(board).filter((m) => m !== id)
  return {
    ...board, root, minimized,
    panels: board.panels.filter((p) => p.id !== id),
    focus: next || leaves(root).find((l) => !minimized.includes(l.id))?.id || null,
    maximized: board.maximized === id ? null : board.maximized,
  }
}

// The cards on show (not minimized), in reading order.
export const visibleIds = (board) =>
  leaves(board?.root).map((l) => l.id).filter((id) => !mins(board).includes(id))

// Collapse a card to the strip; its leaf (and so its size) stays in the tree.
export function minimizeCard(board, id) {
  if (!findLeaf(board.root, id) || mins(board).includes(id)) return board
  const focus = board.focus === id ? nextFocus(board, id) : board.focus
  return {
    ...board,
    minimized: [...mins(board), id],
    focus: focus || null,
    maximized: board.maximized === id ? null : board.maximized,
  }
}

// Back from the strip, at the size it had, and focused.
export function restoreCard(board, id) {
  if (!findLeaf(board.root, id)) return board
  return { ...board, minimized: mins(board).filter((m) => m !== id), focus: id }
}

// Tab: the next (step 1) or previous (-1) card on show, wrapping.
export function cycleFocus(board, step = 1) {
  const ids = visibleIds(board)
  if (!ids.length) return board
  const i = ids.indexOf(board.focus)
  const focus = i < 0 ? ids[step > 0 ? 0 : ids.length - 1]
    : ids[(i + step + ids.length) % ids.length]
  return focus === board.focus ? board : { ...board, focus }
}

// Tab+arrow: the card across that edge of the focused one.
export function focusToward(board, direction) {
  const id = board.focus && neighbor(board.root, board.focus, direction, mins(board))
  return id ? { ...board, focus: id } : board
}

// Swap two cards' places; each takes the other's slot (and its size).
export function swapLeaves(root, a, b) {
  const la = findLeaf(root, a), lb = findLeaf(root, b)
  if (!la || !lb || a === b) return root
  const rec = (node) => {
    if (!isSplit(node)) return node.id === a ? lb : node.id === b ? la : node
    return { ...node, children: node.children.map(rec) }
  }
  return rec(root)
}

// Shift+arrow: swap the focused card with its neighbour that way.
export function moveCard(board, direction, id = board.focus) {
  const other = id && neighbor(board.root, id, direction, mins(board))
  return other ? { ...board, root: swapLeaves(board.root, id, other) } : board
}


export function patchCardState(board, id, patch) {
  return {
    ...board,
    panels: board.panels.map((p) => (p.id === id ? { ...p, state: { ...p.state, ...patch } } : p)),
  }
}
