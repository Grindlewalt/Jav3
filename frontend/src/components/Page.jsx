import PageHeader from './PageHeader.jsx'

// The page shell. THE CONTRACT, for every page that adopts it:
//
//   <Page title="…" [variant] [lede] [actions] [className]>children</Page>
//
//   - `title` is rendered as the page's ONE <h1>. Sections inside are <h2>,
//     cards <h3>; a page must not carry a second <h1> of its own. A page whose
//     heading lives elsewhere (Chat's greeting, the Workspace header) simply
//     does not pass `title` and owns its <h1> itself.
//   - `variant` is the page's HEIGHT MODEL, a layout fact rather than taste:
//       doc    the element itself scrolls; 960px, centred (.page).
//              Projects, Tools, Settings, NotFound.
//       split  full-height flex; an <aside> and a <main> own their own
//              scrolling (.page-shell > .split-layout). Schedules; the
//              file-editor pages (Memory, Agents, Skills) once they migrate.
//       fill   full-height flex; the children own the scrolling. Review.
//     `doc` keeps its heading inside the column. `split` and `fill` fill the
//     viewport, so their heading becomes a bar across the top — the shape the
//     Workspace header already had (`.page-shell > .page-head, .ws-head`).
//   - `actions` go to the right of the heading (a Tabs strip, a select, a
//     button). `lede` is one dim line under it.
//   - Nothing here paints a background: the shell is layout only.
//
// Before this there were three shells plus five bespoke layouts, and no
// agreement on whether a page even had a title: two opened with <h1>, five
// with <h2>, and six named themselves with a <div className="side-title"> in
// a sidebar — a div, so those pages had no heading at all.
const VARIANTS = {
  doc: 'page',
  split: 'page-shell',
  fill: 'page-shell',
}

export default function Page({
  variant = 'doc', title, lede, actions, className = '', children, ...rest
}) {
  const cls = [VARIANTS[variant] ?? VARIANTS.doc, className].filter(Boolean).join(' ')
  const body = variant === 'split'
    ? <div className="split-layout">{children}</div>
    : children
  return (
    <div className={cls} {...rest}>
      {title && <PageHeader level={1} title={title} lede={lede} actions={actions} />}
      {body}
    </div>
  )
}
