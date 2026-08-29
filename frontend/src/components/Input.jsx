import { useId } from 'react'

// A labelled text control. The bare `input, textarea, select` rule already
// carries the whole look (bg-soft, hairline, --radius-lg corners, accent focus
// ring); this only adds the label/hint scaffolding that Skills, Schedules and
// Network each re-declare as `label.mini` with identical values.
//
// Rendered as <label> wrapping the control, which is how every existing call
// site does it — no htmlFor/id pairing to get wrong, and the whole label is a
// hit target. `id` is still generated so `aria-describedby` can point at the
// hint.
export default function Input({
  label, hint, error, textarea = false, rows, className = '', ...rest
}) {
  const hintId = useId()
  const Control = textarea ? 'textarea' : 'input'
  const control = (
    <Control
      rows={textarea ? (rows ?? 4) : undefined}
      aria-describedby={hint || error ? hintId : undefined}
      aria-invalid={error ? true : undefined}
      className={className || undefined}
      {...rest}
    />
  )
  if (!label && !hint && !error) return control
  return (
    <label className="field">
      {label && <span>{label}</span>}
      {control}
      {error
        ? <span className="error" id={hintId}>{error}</span>
        : hint && <span className="field-hint" id={hintId}>{hint}</span>}
    </label>
  )
}

// The checkbox row — `<label className="mini row">` with a checkbox and a span,
// as in Skills' "granted to Jarvis and agents" and Agents' switches. A
// checkbox's label must sit beside it, not above, so it is its own component
// rather than a flag on Input.
export function Checkbox({ label, className = '', ...rest }) {
  return (
    <label className={['check-row', className].filter(Boolean).join(' ')}>
      <input type="checkbox" {...rest} />
      <span>{label}</span>
    </label>
  )
}
