import { useId } from 'react'

// A native <select>, because that is what every call site in the app uses and
// a custom listbox would lose the phone's picker wheel.
//
// `options` accepts either strings (Skills' param types: 'string', 'number',
// …) or {value, label} objects (Artifacts' and Network's project lists, which
// map slug -> name). `placeholder` is the disabled-empty-option idiom the app
// already uses for an action menu that must not show a current value:
//   <option value="" disabled>merge into…</option>
export default function Select({
  label, hint, options = [], placeholder, children, className = '', ...rest
}) {
  const hintId = useId()
  const control = (
    <select
      aria-describedby={hint ? hintId : undefined}
      className={className || undefined}
      {...rest}
    >
      {placeholder && <option value="" disabled>{placeholder}</option>}
      {options.map((o) => {
        const value = typeof o === 'string' ? o : o.value
        const text = typeof o === 'string' ? o : (o.label ?? o.value)
        return <option key={value} value={value}>{text}</option>
      })}
      {children}
    </select>
  )
  if (!label && !hint) return control
  return (
    <label className="field">
      {label && <span>{label}</span>}
      {control}
      {hint && <span className="field-hint" id={hintId}>{hint}</span>}
    </label>
  )
}
