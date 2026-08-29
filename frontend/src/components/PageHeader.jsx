// The top of a page: a heading, an optional one-line lede, optional actions.
//
// Modelled on ComputerUse's `.cu-head`, which is the only page that already
// lays this out properly (h1, flex:1, an action button on the right). Nine
// other pages open with a bare <h2>, and six have no page-level heading at
// all — WP3 owns fixing that; this component is what they will use.
//
// `level` defaults to 2 because that is what nine of the eleven headed pages
// use today, and changing it here would silently reshuffle the whole document
// outline. WP3 picks the real levels.
export default function PageHeader({
  title, lede, actions, level = 2, className = '', children,
}) {
  const H = `h${level}`
  return (
    <>
      <div className={['page-head', className].filter(Boolean).join(' ')}>
        <H>{title}</H>
        {actions}
      </div>
      {lede && <p className="page-lede">{lede}</p>}
      {children}
    </>
  )
}
