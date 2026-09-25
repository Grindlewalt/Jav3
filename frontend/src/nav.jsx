// The navigation, written once.
//
// It used to be written three times: the top bar, the rail the Chat page
// publishes when its sidebar collapses, and a separately-authored mobile
// drawer that had drifted into carrying its own theme toggle and its own
// logout button. Adding a destination meant editing three lists and hoping.
// Everything below — bar, rail, drawer, and the ⋯ overflow menu in all three —
// is rendered from NAV_ITEMS by NavItem, so a destination is one entry.
//
// The bar carries six primaries — the surfaces work starts from, Review (the
// one link that asks the operator for something, so it wears the pending
// count), and Settings. Two more sit behind ⋯: Memory (what Jav3 reads
// before every turn) and Schedules. Voice and Artifacts are deliberately
// absent — their routes still work, they are just not advertised — and
// Network and Logs are Review's sub-tabs now, not destinations of their own.
import { createContext } from 'react'
import { NavLink } from 'react-router-dom'
import { badge } from './format.js'

// ---- icons ----
// One glyph per destination — 24x24, 1.7 stroke, round caps, currentColor.
// The nav lives in two places and the icons FLY between them (the FLIP in
// App.jsx), so every placement must draw the same mark. ToolActivity borrows
// these for its activity rows rather than drawing a second set.
export const PATHS = {
  chat: <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4L3 21l1.2-3.6A8.4 8.4 0 1 1 21 11.5Z" />,
  projects: <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7.5a1.5
                      1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17.5Z" />,
  agents: <><circle cx="12" cy="8.6" r="3.4" /><path d="M5.5 19.4a6.5 6.5 0 0 1 13 0" /></>,
  review: <path d="M12 3.2 19.2 6v5.6c0 4-3 7.2-7.2 9.2-4.2-2-7.2-5.2-7.2-9.2V6Zm-2.6
                   8.6 2 2.1 4-4.2" />,
  // a wrench: the tool registry. A single closed outline — open jaws, a round
  // head, a straight handle — because a thinner path rendered as a diagonal
  // squiggle at the 19px it is actually drawn at.
  tools: <path d="M14.8 6.4a1 1 0 0 0 0 1.4l1.4 1.4a1 1 0 0 0 1.4 0l3.3-3.3a5.4
                  5.4 0 0 1-7.1 7.1l-6.2 6.2a1.9 1.9 0 0 1-2.7-2.7l6.2-6.2a5.4
                  5.4 0 0 1 7.1-7.1Z" />,
  // a gear: eight even teeth on a ring (generated, not hand-drawn — a freehand
  // gear wobbled at 19px), drawn as one outline; the hub is a second circle
  settings: <><path d="M10.2 4.9 10.6 2.7 13.4 2.7 13.8 4.9 15.8 5.7 17.6 4.4 19.6 6.4
                       18.3 8.2 19.1 10.2 21.3 10.6 21.3 13.4 19.1 13.8 18.3 15.8 19.6
                       17.6 17.6 19.6 15.8 18.3 13.8 19.1 13.4 21.3 10.6 21.3 10.2 19.1
                       8.2 18.3 6.4 19.6 4.4 17.6 5.7 15.8 4.9 13.8 2.7 13.4 2.7 10.6
                       4.9 10.2 5.7 8.2 4.4 6.4 6.4 4.4 8.2 5.7Z" />
              <circle cx="12" cy="12" r="3" /></>,
  // an open book: the memory files Jav3 reads before every turn
  memory: <path d="M12 6.6C10.4 5.3 8.4 4.7 5 4.7v12.9c3.4 0 5.4.6 7 1.9 1.6-1.3
                   3.6-1.9 7-1.9V4.7c-3.4 0-5.4.6-7 1.9Zm0 0v12.9" />,
  // a calendar with one marked day
  schedules: <><rect x="3.5" y="5.2" width="17" height="15.3" rx="2.2" />
               <path d="M3.5 10h17M8 3.5v3.4M16 3.5v3.4M8.4 14.3h3" /></>,
  // a clock turning back: the chat history sheet (the drawer's way into the
  // Chat sidebar on a phone, where the sidebar is off-canvas)
  history: <path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 4.5v3.2h3.2M12 8v4l2.8 1.8" />,
}

export function NavIcon({ name, innerRef }) {
  return (
    <span className="nav-ico" ref={innerRef} aria-hidden="true">
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        {PATHS[name]}
      </svg>
    </span>
  )
}

// The overflow button's own glyph. It is not a destination, so it is not in
// PATHS, but it has to draw at the same weight in a 38px rail cell.
export const MoreIcon = () => (
  <span className="nav-ico" aria-hidden="true">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor">
      <circle cx="5.5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" />
      <circle cx="18.5" cy="12" r="1.6" />
    </svg>
  </span>
)

// ---- the destinations ----
// `end` is react-router's exact flag: Chat is `/`, so it must not stay lit on
// every other route. Projects has no `end` on purpose — /projects/:slug is the
// Workspace, the most important surface in the app, and this is how it stays
// located in the nav while it has no row of its own. `count` names the live
// count the item wears (App.jsx's notices).
export const NAV_ITEMS = [
  { to: '/', label: 'Chat', icon: 'chat', end: true, primary: true },
  { to: '/projects', label: 'Projects', icon: 'projects', primary: true },
  { to: '/agents', label: 'Agents', icon: 'agents', primary: true },
  { to: '/security', label: 'Security', icon: 'review', primary: true, count: 'review' },
  { to: '/tools', label: 'Tools', icon: 'tools', primary: true },
  { to: '/settings', label: 'Settings', icon: 'settings', primary: true },
  { to: '/memory', label: 'Memory', icon: 'memory' },
  { to: '/schedules', label: 'Schedules', icon: 'schedules' },
]

// What earns a slot on the bar itself, and what the ⋯ menu holds. The drawer
// shows both: on a phone it is the only navigation there is, so it has to be
// the whole map, not the leftovers.
export const PRIMARY_ITEMS = NAV_ITEMS.filter((i) => i.primary)
export const OVERFLOW_ITEMS = NAV_ITEMS.filter((i) => !i.primary)

// The nav's two homes exchange it through this: the Chat page hands up the
// DOM node inside its collapsed sidebar, and App portals the links into it.
// A slot means "render as a rail" — no second source of truth to keep in sync.
export const NavSlotContext = createContext(() => {})

// ---- one link, every placement ----
// The bar, the rail, the menu and the drawer all render this. They differ by
// the container's class and by CSS, never by markup — which is exactly what
// lets the icons fly between the bar and the rail, and what stops the drawer
// drifting away from the bar again.
export function NavItem({
  item, className, iconRef, count = 0, onClick, tabIndex, role,
}) {
  return (
    <NavLink to={item.to} end={item.end} title={item.label}
             className={className} onClick={onClick} tabIndex={tabIndex} role={role}>
      <NavIcon name={item.icon} innerRef={iconRef} />
      <span className="nav-label">{item.label}</span>
      {count > 0 && <span className="nav-count">{badge(count)}</span>}
    </NavLink>
  )
}

// A run of items — the body of the ⋯ menu and the body of the drawer, which
// are the same list in two containers. `counts` maps an item's `count` key to
// its live number.
export function NavList({
  items, itemClassName, counts = {}, onNavigate, tabIndex, itemRole,
}) {
  return items.map((item) => (
    <NavItem key={item.to} item={item} className={itemClassName}
             count={counts[item.count] || 0} role={itemRole}
             onClick={onNavigate} tabIndex={tabIndex} />
  ))
}
