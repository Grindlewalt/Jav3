import { NavLink } from 'react-router-dom'
import { badge } from '../format.js'

// The tab strip: a segmented control, the shape .voice-tier-switch already
// draws — a glass pill track with the chosen segment lifted on --panel-2 under
// an accent ring. Two uses, one look:
//
//   value/onChange   an in-page view switch, like Logs' transcripts|cost pair
//                    (two bare buttons today). Renders role=tablist with the
//                    ARIA keyboard pattern: arrows move, Home/End jump, and
//                    moving selects, so there is one focus stop in the strip.
//   items[].to       sub-pages with their own URL (Review's queue|network|logs).
//                    Renders a <nav> of NavLinks, so the browser owns focus and
//                    history and a tab is a link you can open in a new window.
//
// `count` renders the red pill the Review nav link wears (.nav-count), because
// the number a tab carries is that same pending count. `panel` is the id of
// the tabpanel a tab controls, for the tablist form.
export default function Tabs({
  items, value, onChange, label, className = '', ...rest
}) {
  const cls = ['tabs', className].filter(Boolean).join(' ')
  const count = (n) => n > 0 && <span className="nav-count">{badge(n)}</span>

  if (items.some((t) => t.to)) {
    return (
      <nav className={cls} aria-label={label} {...rest}>
        {items.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} title={t.title}>
            {t.label}{count(t.count)}
          </NavLink>
        ))}
      </nav>
    )
  }

  const onKey = (e) => {
    const idx = items.findIndex((t) => t.id === value)
    if (idx < 0) return
    const n = items.length
    let next = null
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % n
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + n) % n
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = n - 1
    if (next === null) return
    e.preventDefault()
    onChange(items[next].id)
    e.currentTarget.querySelectorAll('[role="tab"]')[next]?.focus()
  }
  return (
    <div className={cls} role="tablist" aria-label={label} onKeyDown={onKey} {...rest}>
      {items.map((t) => {
        const on = t.id === value
        return (
          <button key={t.id} type="button" role="tab" aria-selected={on}
                  aria-controls={t.panel} tabIndex={on ? 0 : -1}
                  className={on ? 'on' : undefined} title={t.title}
                  onClick={() => onChange(t.id)}>
            {t.label}{count(t.count)}
          </button>
        )
      })}
    </div>
  )
}
