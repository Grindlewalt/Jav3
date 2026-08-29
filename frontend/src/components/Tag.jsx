// The small status pill. `.tag` is dim-on-panel-2 by default and the app
// already ships tone modifiers for it — .pending/.planning (amber),
// .running (accent), .done (green), .error (red), .untrusted (amber) — plus
// severity variants used by the Review and Network pages. `tone` picks one of
// those existing classes; it invents no new colour.
const TONES = ['pending', 'planning', 'running', 'done', 'error', 'untrusted']

export default function Tag({ tone, className = '', children, ...rest }) {
  const cls = ['tag', tone && TONES.includes(tone) ? tone : '', className]
    .filter(Boolean).join(' ')
  return <span className={cls} {...rest}>{children}</span>
}

// `.badge` is the other pill in the app: green-tinted, used for a positive
// state ("granted" on Tools and Skills). It is a separate rule with a separate
// shape (pill radius, roomier padding), so it stays a separate component
// rather than a Tag tone.
export function Badge({ className = '', children, ...rest }) {
  return (
    <span className={['badge', className].filter(Boolean).join(' ')} {...rest}>
      {children}
    </span>
  )
}
