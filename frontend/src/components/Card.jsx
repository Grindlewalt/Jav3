// A panel surface: --panel background, hairline border, --radius corners.
//
// The style is `.tool-card`'s, which is what every "card" in the app already
// looks like when it looks like anything. Until now `.panel` had no base rule
// at all, so Settings' and Pair's <section className="panel"> blocks rendered
// as bare divs — Settings' old danger zone even set a border-color on them,
// which did nothing without a border. `.card` and `.panel` are now the same
// rule, which fixes those in place.
//
// `flush` drops the padding for a card whose own children own their insets.
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
