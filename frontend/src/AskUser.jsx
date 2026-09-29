import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import {
  answerBody, foldAsks, freeLabel, initAsk, initialMode, keyToAction, MODE_HINT, MODE_LABEL, nextMode,
  PERMISSION_MODES, reduceAsk,
} from './askUser.js'

// The chat's open ask_user / permission asks (backend/operator_ask.py). Fed
// from the turn's stream; a re-attached tail replays the ones still waiting.
export function useOperatorAsks(cid) {
  const [asks, setAsks] = useState([])
  const prev = useRef(cid)
  // switching chats drops the old chat's asks; a new chat getting its id
  // (null -> id, on `start`) keeps the ask that may already have arrived
  useEffect(() => {
    if (prev.current != null && prev.current !== cid) setAsks([])
    prev.current = cid
  }, [cid])
  const onEvent = useCallback((ev) => {
    if (ev.type === 'ask_user' || ev.type === 'ask_done') setAsks((a) => foldAsks(a, ev))
  }, [])
  const drop = useCallback((id) => setAsks((a) => a.filter((x) => x.id !== id)), [])
  return { asks, onEvent, drop }
}

// The first open ask, answered in place above the composer. Keys as in the
// terminal: ↑↓ move, 1-5 pick, space toggles (multi-select), typing goes to
// the free-text option, enter confirms and moves on, esc skips.
export function AskPanel({ asks, cid, compact = false }) {
  const ask = asks.asks[0]
  if (!ask || cid == null) return null
  return <AskCard key={ask.id} ev={ask} cid={cid} compact={compact}
                  onDone={() => asks.drop(ask.id)} />
}

function AskCard({ ev, cid, compact, onDone }) {
  const questions = ev.questions || []
  const [st, setSt] = useState(initAsk)
  const [err, setErr] = useState('')
  const box = useRef(null)
  const text = useRef(null)
  const q = questions[st.i] || {}
  const options = q.options || []
  const multi = !!q.multi_select

  // The card takes the keys when it appears, unless the reader is mid-sentence
  // in a field: an ask that landed while they typed their next message used to
  // swallow the rest of it (and Enter answered it).
  useEffect(() => {
    const a = document.activeElement
    const typing = a && a !== document.body && a !== box.current
      && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT' || a.isContentEditable)
      && String(a.value ?? a.textContent ?? '').length > 0
    if (!typing) box.current?.focus({ preventScroll: true })
  }, [])

  async function submit(answers) {
    try {
      await api(`/api/chat/${cid}/answer`,
                { method: 'POST', body: JSON.stringify(answerBody(ev.id, answers)) })
      onDone()
    } catch (e) {
      if (e.status === 404) onDone()          // answered elsewhere or gone
      else setErr(e.detail || String(e))
    }
  }

  function act(action) {
    if (action === 'skip') { submit(null); return }
    if (action === 'focus-text') { text.current?.focus(); return }
    const next = reduceAsk(st, questions, action)
    setSt(next)
    if (next.done) submit(next.done)
    else if (next.i !== st.i) box.current?.focus()
  }

  function onKey(e) {
    if (e.target === text.current) {
      if (e.key === 'Enter') { e.preventDefault(); act({ type: 'confirm' }) }
      else if (e.key === 'Escape') { e.preventDefault(); act('skip') }
      else if (e.key === 'ArrowUp') { e.preventDefault(); box.current?.focus(); act({ type: 'move', d: -1 }) }
      return
    }
    const a = keyToAction(e.key, st, questions)
    if (a == null || e.metaKey || e.ctrlKey || e.altKey) return
    e.preventDefault()
    if (a === 'focus-text' && e.key.length === 1 && e.key !== ' ') {
      act({ type: 'text', value: st.text + e.key })
    }
    act(a)
    if (a.type === 'move' && st.cur + a.d >= options.length) text.current?.focus()
  }

  const perm = ev.kind === 'permission'
  return (
    <div className={`ask-card${compact ? ' compact' : ''}${perm ? ' perm' : ''}`}
         role="dialog" aria-label={perm ? 'permission request' : 'the agent asks'}
         tabIndex={0} ref={box} onKeyDown={onKey}>
      <div className="ask-head">
        <b>{perm ? 'Permission' : 'The agent asks'}</b>
        {perm && ev.reason && <span className="dim"> · {ev.reason}</span>}
        {questions.length > 1 && <span className="dim"> · {st.i + 1}/{questions.length}</span>}
        {ev.conversation_id != null && ev.conversation_id !== cid &&
          <span className="dim"> · agent #{ev.conversation_id}</span>}
      </div>
      <div className="ask-q">{q.question}</div>
      {ev.detail && <pre className="ask-detail">{ev.detail}</pre>}
      <ul className="ask-opts" role={multi ? 'group' : 'radiogroup'}>
        {options.map((o, j) => (
          <li key={o} className={j === st.cur ? 'cur' : ''}
              onClick={() => act({ type: 'pick', j })}>
            <span className="ask-mark">{multi ? (st.sel.includes(j) ? '☑' : '☐')
                                              : (st.sel.includes(j) ? '◉' : '○')}</span>
            <span className="dim">{j + 1}.</span> {o}
          </li>
        ))}
        <li className={st.cur === options.length ? 'cur' : ''}>
          <span className="ask-mark">{st.text.trim() ? (multi ? '☑' : '◉') : (multi ? '☐' : '○')}</span>
          <input ref={text} className="grow" placeholder={freeLabel(ev)} value={st.text}
                 onKeyDown={onKey}
                 onChange={(e) => setSt((s) => reduceAsk(s, questions, { type: 'text', value: e.target.value }))} />
        </li>
      </ul>
      {err && <div className="error">{err}</div>}
      <div className="row ask-actions">
        <span className="grow dim">↑↓ · 1-5 · {multi ? 'space toggles · ' : ''}enter · esc skips</span>
        <button type="button" className="ghost" onClick={() => act('skip')}>Skip</button>
        <button type="button" onClick={() => act({ type: 'confirm' })}>
          {st.i + 1 < questions.length ? 'Next' : 'Answer'}</button>
      </div>
    </div>
  )
}

const MODE_KEY = 'jav3.permission_mode'   // the last mode picked here: where a new chat starts

// The chat toolbar's permission-mode selector, stored per conversation
// (PUT /api/chat/{cid}/permission_mode). Before the chat exists the choice is
// held here and sent with the first message (`permission_mode`).
export function PermissionModeSelect({ cid, value, onChange }) {
  async function set(mode) {
    onChange(mode)
    try { localStorage.setItem(MODE_KEY, mode) } catch { /* private mode */ }
    if (cid == null) return
    try {
      await api(`/api/chat/${cid}/permission_mode`,
                { method: 'PUT', body: JSON.stringify({ mode }) })
    } catch { /* the next open re-reads it */ }
  }
  return (
    <select className={`perm-mode ${value}`} value={value} title={MODE_HINT[value]}
            aria-label="permission mode" onChange={(e) => set(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Tab' && e.shiftKey) { e.preventDefault(); set(nextMode(value)) } }}>
      {PERMISSION_MODES.map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
    </select>
  )
}

// the conversation's mode, re-read when the chat changes
export function usePermissionMode(cid) {
  const [mode, setMode] = useState(() => {
    try { return initialMode(localStorage.getItem(MODE_KEY)) } catch { return 'yolo' }
  })
  useEffect(() => {
    if (cid == null) return
    let live = true
    api(`/api/chat/${cid}/permission_mode`).then((r) => { if (live) setMode(r.mode) })
      .catch(() => {})
    return () => { live = false }
  }, [cid])
  return [mode, setMode]
}
