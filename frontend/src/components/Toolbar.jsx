// A horizontal strip of controls. Three near-identical shapes exist today:
//
//   .row       flex, gap 8, align center, wrap, 10px vertical margin
//   .pane-head flex, gap 10, align center, 10px bottom margin, h3 flex:1
//   ad-hoc     <div className="row" style={{gap:6, marginBottom:8}}>  (Logs)
//
// `.toolbar` is the .pane-head shape without the assumption that it heads a
// pane, so it works inside a Card, above a list, or as a form row. `variant`
// keeps the two legacy classes reachable so a migrating page can move markup
// first and styling second, rather than needing both in one commit.
//
// The title slot is `.toolbar-title`: flex:1 and ellipsised, which is what
// `.pane-head h3` already does (Artifacts and Skills put a file path there,
// and a long path used to push the Save button off the edge).
const VARIANTS = { toolbar: 'toolbar', row: 'row', pane: 'pane-head' }

export default function Toolbar({
  variant = 'toolbar', title, titleLevel = 3, as: El = 'div',
  className = '', children, ...rest
}) {
  const H = `h${titleLevel}`
  const cls = [VARIANTS[variant] ?? VARIANTS.toolbar, className]
    .filter(Boolean).join(' ')
  return (
    <El className={cls} {...rest}>
      {title && <H className="toolbar-title">{title}</H>}
      {children}
    </El>
  )
}

// Pushes everything after it to the right. The app spells this three ways
// today — `<span className="grow" />`, `margin-left: auto` baked into
// `button.link`, and `style={{ marginLeft: 'auto' }}`.
export function Spacer() { return <span className="grow" /> }
