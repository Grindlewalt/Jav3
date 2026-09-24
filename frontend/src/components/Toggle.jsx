// The on/off switch: a pill track with a sliding thumb, the shape a phone's
// settings use for a binary that takes effect the moment it is flipped (no
// Save button behind it). A checkbox reads as "part of a form I will submit";
// this reads as "live now", which is what every call site here means.
//
// It is a <button role="switch">, so Space and Enter toggle it natively and it
// takes one focus stop — no keydown handler to get wrong. `onChange` receives
// the NEW boolean, like a checkbox's e.target.checked would.
//
// `label` is the accessible name and stays fixed across states (a switch's
// name must not flip with its value, or a screen reader hears two different
// controls). `onText`/`offText` are the visible state caption beside the
// track — "Auto" / "Manual" for the reviewer — and are aria-hidden because
// aria-checked already says which side it is on.
export default function Toggle({
  checked = false, onChange, label, onText, offText, disabled = false,
  className = '', ...rest
}) {
  const text = checked ? onText : offText
  const cls = ['toggle', checked ? 'on' : '', className].filter(Boolean).join(' ')
  return (
    <button type="button" role="switch" aria-checked={checked}
            aria-label={label} disabled={disabled} className={cls}
            onClick={() => onChange?.(!checked)} {...rest}>
      <span className="toggle-track" aria-hidden="true">
        <span className="toggle-thumb" />
      </span>
      {text && <span className="toggle-text" aria-hidden="true">{text}</span>}
    </button>
  )
}
