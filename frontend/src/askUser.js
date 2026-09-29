// Pure logic for the ask_user dialog (backend/operator_ask.py): one question
// at a time, a cursor over the options plus a last free-text option, and the
// keys the terminal client uses (↑↓ move, 1-5 pick, space toggles in
// multi-select, typing picks the free-text option, enter confirms and moves
// on, esc skips). Kept out of the component so node can test it.

export const FREE_TEXT = 'Type something…'
export const PERMISSION_MODES = ['yolo', 'auto', 'ask']
export const MODE_HINT = {
  yolo: 'everything runs in the VM without asking',
  auto: 'a cheap judge reviews each write/run; risky ones ask you',
  ask: 'every write/run asks you first',
}
// What the picker shows. The bare word says nothing to someone who has not read
// the docs, and this is the most important switch in the product: the meaning
// sits in the option, not only in a tooltip (WEB-15). The mode is still the
// first word, so a keyboard user typing "a" still lands on auto.
export const MODE_LABEL = {
  yolo: 'yolo: runs without asking',
  auto: 'auto: a judge checks, risky asks you',
  ask: 'ask: asks before every write or run',
}

// A new chat starts in the mode you last picked on this browser, else yolo (the
// server's own default): a choice you made is not thrown away, and the picker
// always shows the mode and what it means.
export const initialMode = (stored) => (PERMISSION_MODES.includes(stored) ? stored : 'yolo')

export function nextMode(mode) {
  const i = PERMISSION_MODES.indexOf(mode)
  return PERMISSION_MODES[(i + 1) % PERMISSION_MODES.length]
}

export function freeLabel(ev) {
  return (ev && ev.free_text_label) || FREE_TEXT
}

export function initAsk() {
  return { i: 0, cur: 0, sel: [], text: '', answers: [], done: null }
}

const opts = (q) => (q && Array.isArray(q.options) ? q.options : [])

// one answer for question q: {selected: [labels], text: str | null}, or null
// when nothing is chosen yet
export function answerFor(q, sel, text) {
  const t = (text || '').trim()
  const selected = [...sel].sort((a, b) => a - b).map((j) => opts(q)[j]).filter(Boolean)
  if (!selected.length && !t) return null
  return { selected, text: t || null }
}

// state x action -> state. Actions: {type: 'move', d} | {type: 'pick', j} |
// {type: 'toggle'} (space on the cursor row) | {type: 'text', value} |
// {type: 'confirm'}. A finished ask has `done` = the answers array.
export function reduceAsk(state, questions, action) {
  const q = questions[state.i]
  const n = opts(q).length
  const multi = !!(q && q.multi_select)
  switch (action.type) {
    case 'move':
      return { ...state, cur: Math.max(0, Math.min(n, state.cur + action.d)) }
    case 'pick': {
      const j = action.j
      if (j < 0 || j >= n) return state
      if (multi) {
        const sel = state.sel.includes(j) ? state.sel.filter((x) => x !== j) : [...state.sel, j]
        return { ...state, sel, cur: j }
      }
      return { ...state, sel: [j], text: '', cur: j }
    }
    case 'toggle':
      if (state.cur >= n) return state
      return reduceAsk(state, questions, { type: 'pick', j: state.cur })
    case 'text': {
      const value = action.value
      const next = { ...state, text: value }
      if (value.trim()) {
        next.cur = n
        if (!multi) next.sel = []
      }
      return next
    }
    case 'confirm': {
      let { sel } = state
      if (!sel.length && !state.text.trim()) {
        if (state.cur < n) sel = [state.cur]
        else return state            // the free-text row with nothing typed
      }
      const a = answerFor(q, sel, state.text)
      if (!a) return state
      const answers = [...state.answers, a]
      if (state.i + 1 >= questions.length) return { ...state, answers, done: answers }
      return { i: state.i + 1, cur: 0, sel: [], text: '', answers, done: null }
    }
    default:
      return state
  }
}

// a keydown (outside the text field) -> an action, 'skip', 'focus-text', or null
export function keyToAction(key, state, questions) {
  const n = opts(questions[state.i]).length
  if (key === 'ArrowUp') return { type: 'move', d: -1 }
  if (key === 'ArrowDown') return { type: 'move', d: 1 }
  if (key === 'Enter') return { type: 'confirm' }
  if (key === 'Escape') return 'skip'
  if (key === ' ') return state.cur < n ? { type: 'toggle' } : 'focus-text'
  if (/^[1-5]$/.test(key) && Number(key) <= n) return { type: 'pick', j: Number(key) - 1 }
  if (key.length === 1) return 'focus-text'
  return null
}

// the body POSTed to /api/chat/{cid}/answer
export function answerBody(id, answers) {
  return answers == null ? { id, skipped: true } : { id, answers }
}

// fold one stream event into the list of open asks
export function foldAsks(asks, ev) {
  if (ev.type === 'ask_user' && ev.id && !asks.some((a) => a.id === ev.id)) return [...asks, ev]
  if (ev.type === 'ask_done') return asks.filter((a) => a.id !== ev.id)
  return asks
}
