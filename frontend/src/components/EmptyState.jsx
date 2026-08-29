// "Nothing here yet", written eight different ways across the pages:
//
//   <li className="dim">no projects yet</li>                 Projects
//   <li className="dim">none yet</li>                        Skills, Agents
//   <li className="dim" style={{cursor:'default'}}>…</li>    Network x2, Logs
//   <p className="dim">registry is empty</p>                 Tools
//   <p className="dim">none yet — set one up on the left</p> Schedules
//   <p className="dim center-pad">pick a file…</p>           Artifacts
//   <div className="dim center-pad">…</div>                  Skills, Review,
//                                                            Logs x3, Network x2
//
// Two shapes, not eight: a quiet row inside a list, and a centred block in an
// otherwise empty pane. `as` picks the element (a <ul> needs an <li> child);
// `pad` picks the shape. The inline cursor:default that four call sites carry
// is baked into .empty-row — a <li> in a clickable list inherits a pointer,
// and an unclickable row must not claim one.
export default function EmptyState({
  as, pad = false, hint, className = '', children,
}) {
  const El = as || (pad ? 'div' : 'p')
  const cls = [pad ? 'empty-pad' : 'empty-row', className]
    .filter(Boolean).join(' ')
  return (
    <El className={cls}>
      {children}
      {hint && <div className="field-hint empty-hint">{hint}</div>}
    </El>
  )
}
