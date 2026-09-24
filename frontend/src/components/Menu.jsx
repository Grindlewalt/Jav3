import { NavLink } from 'react-router-dom'
import { useDismiss } from '../useDismiss.js'

// The glass dropdown. The recipe — a translucent --glass panel, 16px blur,
// 14px corners, a 6px inset, --shadow-pop, dropping in from its own corner —
// was written out three times: .notif-drop (the bell, the VM chip and the
// nav's More menu), .proj-menu (the composer's project picker) and .model-menu
// (the composer's model picker, which opens upward). Same values, three names.
// `.menu` in styles.css is that recipe once; the three keep their classes
// until their call sites migrate.
//
// The caller keeps the open state and renders its own trigger, because every
// trigger in the app is styled for where it sits (.nav-more, .proj-chip,
// .model-chip, a .win-btn on a card row). What they share is the wrapper, the
// popover and its dismissal: Escape and click-away come from useDismiss, whose
// ref has to wrap the trigger AND the menu together — a pointerdown on the
// trigger must not count as "outside", or a toggle would close-then-reopen.
//
// `up` opens above the trigger (the model picker lives in a bar at the bottom
// of the screen); `align` picks the anchored edge, 'right' like every existing
// menu. The trigger should carry aria-haspopup="menu" and aria-expanded.
const NOOP = () => {}

export default function Menu({
  open, onClose = NOOP, trigger, align = 'right', up = false, width,
  label, className = '', wrapClassName = '', children,
}) {
  const ref = useDismiss(open, onClose)
  const cls = ['menu', up ? 'up' : '', align === 'left' ? 'left' : '', className]
    .filter(Boolean).join(' ')
  return (
    <div className={['menu-wrap', wrapClassName].filter(Boolean).join(' ')} ref={ref}>
      {trigger}
      {open && (
        <div className={cls} role="menu" aria-label={label}
             style={width ? { '--menu-w': `${width}px` } : undefined}>
          {children}
        </div>
      )}
    </div>
  )
}

// One row. A button by default; a router link when `to` is given (the nav's
// overflow items), which NavLink marks `.active` on the current route. `sub` is
// the second, dimmer line the project and model pickers put under a name.
// `checked` renders the accent dot those pickers use and switches the role to
// menuitemradio, so a screen reader hears which one is chosen — pass it as a
// boolean on every item of a radio group, not only the chosen one.
export function MenuItem({
  to, end, sub, checked, danger = false, className = '', children, ...rest
}) {
  const cls = ['menu-item', danger ? 'danger' : '', className].filter(Boolean).join(' ')
  const body = (
    <>
      {sub != null
        ? <span className="m-name">{children}<span className="m-sub">{sub}</span></span>
        : children}
      {checked && <span className="m-check" aria-hidden="true">●</span>}
    </>
  )
  if (to) {
    return (
      <NavLink to={to} end={end} role="menuitem" className={cls} {...rest}>
        {body}
      </NavLink>
    )
  }
  const radio = checked !== undefined
  return (
    <button type="button" className={cls}
            role={radio ? 'menuitemradio' : 'menuitem'}
            aria-checked={radio ? !!checked : undefined} {...rest}>
      {body}
    </button>
  )
}

// The hairline between groups (.proj-sep, between the binding modes and the
// project list in the composer's picker).
export function MenuSep() { return <div className="menu-sep" role="separator" /> }
