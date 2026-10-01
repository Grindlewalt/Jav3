import { useEffect, useState } from 'react'
import { api, subscribeSse } from '../../api.js'
import { dndBody, dndPresets, dndUntil } from '../../dnd.js'
import { useAsk } from '../../ask.jsx'
import { notifyError } from '../../notify.js'
import { changedKinds, kindSummary, visibleKinds } from '../../notifKinds.js'
import { Button, Card, Input, Select, Toggle } from '../../components/index.js'

// --- notifications ---------------------------------------------------------------
// What interrupts, and what the operator did themselves, are the server's
// calls (backend/security.py); the web toasts and the terminal client's sidebar
// both follow them. None of it hides anything: every event is still in
// Security's queue and history, and the badge still counts what waits.
const LEVEL_OPTIONS = [
  { value: 'critical', label: 'Critical only' },
  { value: 'approvals', label: 'Needs my approval' },
  { value: 'all', label: 'Everything' },
]
const MODES = [['ping', 'Ping'], ['badge', 'Badge'], ['record', 'Record']]

export default function NotificationsPanel() {
  const ask = useAsk()
  const [s, setS] = useState(null)             // the server's view; false = could not load
  useEffect(() => {
    api('/api/notifications/settings').then(setS).catch(() => setS(false))
  }, [])
  // do not disturb can be switched from the terminal, or end by itself
  useEffect(() => subscribeSse('/api/security/stream', (ev) => {
    if (ev.type !== 'dnd_changed') return
    setS((v) => (v ? { ...v, dnd: { on: ev.on, since: ev.since, until: ev.until,
                                    break_critical: ev.break_critical } } : v))
  }), [])

  async function save(patch, optimistic) {
    const was = s
    if (optimistic) setS({ ...s, ...optimistic })
    try {
      setS(await api('/api/notifications/settings',
        { method: 'PUT', body: JSON.stringify(patch) }))
    } catch (e) { setS(was); notifyError(e) }
  }
  async function setDnd(choice) {
    const body = dndBody(choice)
    if (!body) return
    try {
      const d = await api('/api/notifications/dnd', { method: 'PUT', body: JSON.stringify(body) })
      setS((v) => ({ ...v, dnd: d }))
    } catch (e) { notifyError(e) }
  }
  // every choice back to null: the server drops the kind's own mode
  async function resetAll() {
    const changed = changedKinds(s?.kinds)
    if (!changed.length) return
    const ok = await ask.confirm(
      `Reset ${changed.length === 1 ? 'this kind' : `these ${changed.length} kinds`} to the default?`,
      { body: 'Each goes back to following “Ping me for”.', confirmLabel: 'Reset' })
    if (!ok) return
    save({ kinds: Object.fromEntries(changed.map((k) => [k.kind, null])) })
  }

  return <NotificationsView s={s} save={save} setDnd={setDnd} onResetAll={resetAll} />
}

// the card itself, from the server's view: exported so a test can render it
export function NotificationsView({ s, save, setDnd, onResetAll }) {
  if (s === null || s === false) {
    return (
      <Card title="Notifications" headingLevel={2} id="notifications">
        <p className="dim small settings-note">
          {s === null ? 'Loading…' : 'Could not load the notification settings.'}
        </p>
      </Card>
    )
  }
  const dnd = s.dnd || { on: false }
  const dndOptions = [
    { value: 'off', label: 'Off' },
    ...(dnd.on ? [{ value: 'current', label: `On ${dndUntil(dnd)}` }] : []),
    ...dndPresets(),
  ]
  return (
    <Card title="Notifications" headingLevel={2} id="notifications">
      <div className="settings-inline">
        <Select label="Ping me for" value={s.level}
                onChange={(e) => save({ level: e.target.value }, { level: e.target.value })}
                options={LEVEL_OPTIONS} />
        <span className="dim small notif-aside">critical always pings</span>
      </div>
      <div className="notif-row">
        <Toggle checked={s.self_quiet} label="Things I did myself: record only"
                onChange={(v) => save({ self_quiet: v }, { self_quiet: v })} />
        <span>Things I did myself: record only</span>
      </div>
      <p className="dim small settings-note">
        A profile edit, a LAN change, a package you approve, made by you in the web app or
        the terminal, is filed as “by you”, already acknowledged: no ping, not in the badge.
        The same change made by an agent still alerts. Critical alerts are never quieted.
      </p>

      <div className="settings-inline notif-dnd">
        <Select label="Do not disturb" value={dnd.on ? 'current' : 'off'}
                onChange={(e) => e.target.value !== 'current' && setDnd(e.target.value)}
                options={dndOptions} />
        <div className="notif-row">
          <Toggle checked={s.dnd_break_critical} label="Critical alerts break through do not disturb"
                  onChange={(v) => save({ dnd_break_critical: v }, { dnd_break_critical: v })} />
          <span>critical breaks through</span>
        </div>
      </div>
      <p className="dim small settings-note">
        While it is on nothing pings and the top bar says DND. Alerts still count in Security
        and everything is still recorded; approvals a chat is waiting on stay where they
        are. When it ends you get one summary.
      </p>

      <KindsTable kinds={s.kinds} save={save} onResetAll={onResetAll} />
      <p className="dim small settings-note">
        Ping: a toast, and the terminal's sidebar. Badge: counts in Security until you
        acknowledge it, never pings. Record: filed already acknowledged, kept in the history.
        A kind you have not set follows “Ping me for”.
      </p>
    </Card>
  )
}

// What each kind of alert does. Seventy-odd kinds do not belong in a card, so
// it opens on the ones the operator has changed; a search finds any kind to
// change, and "Show all" is the whole list grouped by name.
function KindsTable({ kinds, save, onResetAll }) {
  const [all, setAll] = useState(false)
  const [query, setQuery] = useState('')
  const changed = changedKinds(kinds).length
  const view = visibleKinds(kinds, { all, query })
  const shown = view.grouped ? view.groups.reduce((n, g) => n + g.kinds.length, 0) : view.rows.length
  const searching = query.trim() !== ''

  const row = (k) => (
    <tr key={k.kind} className={k.chosen ? 'chosen' : ''}>
      <th scope="row" className="mono small" title={`usually ${k.usual}`}>{k.kind}</th>
      {k.locked ? (
        <td colSpan={3} className="dim small">locked: always pings</td>
      ) : MODES.map(([m, label]) => (
        <td key={m} className="notif-mode">
          <input type="radio" name={`mode-${k.kind}`} checked={k.mode === m}
                 aria-label={`${k.kind}: ${label}`} title={m === k.default ? 'default' : ''}
                 onChange={() => save({ kinds: { [k.kind]: m } })} />
        </td>))}
      <td className="notif-reset">
        {k.chosen && !k.locked && (
          <button type="button" className="ghost small" title={`back to the default (${k.default})`}
                  onClick={() => save({ kinds: { [k.kind]: null } })}>reset</button>)}
      </td>
    </tr>)

  return (
    <div className="notif-kinds-wrap" role="group" aria-label="What each kind of alert does">
      <div className="notif-kinds-head">
        <h3 className="notif-kinds-title">What each kind does</h3>
        <span className="notif-kinds-rule" aria-hidden="true" />
        <span className="dim small" aria-live="polite">{kindSummary(kinds)}</span>
      </div>
      {shown === 0 ? (
        <p className="dim small settings-note">
          {searching ? `No kind matches “${query.trim()}”.`
            : 'No kind changed. Each follows “Ping me for” and its own default.'}
        </p>
      ) : (
        <div className="notif-kinds">
          <table>
            <thead>
              <tr>
                <th scope="col">Kind</th>
                {MODES.map(([m, label]) => <th key={m} scope="col" className="notif-mode">{label}</th>)}
                <th scope="col"><span className="sr-only">reset</span></th>
              </tr>
            </thead>
            {view.grouped ? view.groups.map((g) => (
              <tbody key={g.prefix}>
                <tr className="notif-group">
                  <th scope="rowgroup" colSpan={5}>{g.prefix}</th>
                </tr>
                {g.kinds.map(row)}
              </tbody>
            )) : <tbody>{view.rows.map(row)}</tbody>}
          </table>
        </div>
      )}
      <div className="notif-kinds-tools">
        <Input type="search" aria-label="Change a kind" placeholder="Change a kind…"
               value={query} onChange={(e) => setQuery(e.target.value)} />
        <Button variant="ghost" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? 'Show only changed' : `Show all ${kinds.length}`}
        </Button>
        <Button variant="ghost" onClick={onResetAll} disabled={changed === 0}>
          Reset all to defaults</Button>
      </div>
    </div>
  )
}
