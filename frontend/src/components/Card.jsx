// A panel surface: --panel background, hairline border, --radius corners.
//
// The style is `.tool-card`'s, which is what every "card" in the app already
// looks like when it looks like anything. Until now `.panel` had no base rule
// at all, so ComputerUse's seven <section className="panel"> blocks rendered
// as bare divs even though the CSS comment above them describes them as cards;
// `.card` and `.panel` are now the same rule, which fixes those in place.
//
// `flush` drops the padding for a card whose own children own their insets
// (`.cu-machine` does this today).
export default function Card({
  as: Tag = 'section', title, actions, flush = false, className = '',
  headingLevel = 3, children, ...rest
}) {
  const H = `h${headingLevel}`
  const cls = ['card', flush ? 'flush' : '', className].filter(Boolean).join(' ')
  return (
    <Tag className={cls} {...rest}>
      {(title || actions) && (
        <div className="toolbar card-head">
          {title && <H className="toolbar-title">{title}</H>}
          {actions}
        </div>
      )}
      {children}
    </Tag>
  )
}
