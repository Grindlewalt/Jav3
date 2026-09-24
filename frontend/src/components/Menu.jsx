import { useLayoutEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
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
//
// `floating` is for a menu whose trigger sits inside something that clips —
// a Workspace panel (overflow:hidden), a card near the screen edge. The
// popover is portalled to <body>, fixed-positioned from the trigger's rect and
// clamped inside the viewport with a 16px gutter, flipping above the trigger
// when there is more room there. The nav's menus keep the in-place absolute
// recipe: their rules position them against the bar and the rail.
const NOOP = () => {}
const GUTTER = 16
const OFFSET = 8

function place(wrap, pop, align, up) {
  const t = wrap.getBoundingClientRect()
  const vw = document.documentElement.clientWidth
  const vh = window.innerHeight
  const w = pop.offsetWidth
  let left = align === 'left' ? t.left : t.right - w
  left = Math.max(GUTTER, Math.min(left, vw - GUTTER - w))
  pop.style.maxHeight = 'none'
  const h = pop.offsetHeight
  const below = vh - t.bottom - OFFSET - GUTTER
  const above = t.top - OFFSET - GUTTER
  const openUp = up ? (above >= h || above > below) : (below < h && above > below)
  const room = Math.max(120, openUp ? above : below)
  const shown = Math.min(h, room)
  pop.style.left = `${left}px`
  pop.style.maxHeight = `${room}px`
  pop.style.top = `${openUp ? t.top - OFFSET - shown : t.bottom + OFFSET}px`
  pop.dataset.side = openUp ? 'up' : 'down'   // not className: React owns that
}

export default function Menu({
  open, onClose = NOOP, trigger, align = 'right', up = false, width,
  label, className = '', wrapClassName = '', floating = false, children,
}) {
  const popRef = useRef(null)
  const ref = useDismiss(open, onClose, floating ? popRef : null)
  const cls = ['menu', up ? 'up' : '', align === 'left' ? 'left' : '',
               floating ? 'floating' : '', className].filter(Boolean).join(' ')

  // before paint, so the menu never flashes at its unplaced spot; a scroll
  // anywhere (the board, a panel, the page) or a resize moves the trigger
  useLayoutEffect(() => {
    if (!open || !floating) return undefined
    const run = () => {
      if (ref.current && popRef.current) place(ref.current, popRef.current, align, up)
    }
    run()
    window.addEventListener('resize', run)
    window.addEventListener('scroll', run, true)
    return () => {
      window.removeEventListener('resize', run)
      window.removeEventListener('scroll', run, true)
    }
  }, [open, floating, align, up, ref])

  const pop = open && (
    <div ref={popRef} className={cls} role="menu" aria-label={label}
         style={width ? { '--menu-w': `${width}px` } : undefined}>
      {children}
    </div>
  )
  return (
    <div className={['menu-wrap', wrapClassName].filter(Boolean).join(' ')} ref={ref}>
      {trigger}
      {floating ? (pop && createPortal(pop, document.body)) : pop}
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
