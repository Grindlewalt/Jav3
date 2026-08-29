// The app's four button shapes, which already exist as bare CSS on `button`:
// the accent primary, `.ghost` (outlined, quieter), `.link` (text only — note
// it carries `margin-left: auto`, so it pushes itself right), and the square
// `.icon-btn`. This wraps them so a variant is a prop rather than a class
// string, and so `danger` stops being spelled two different ways.
//
// Nothing here restyles anything: every variant maps onto the class the pages
// already use, so a migrated call site renders byte-identically.
const VARIANTS = {
  primary: '',
  ghost: 'ghost',
  link: 'link',
  icon: 'icon-btn',
}

export default function Button({
  variant = 'primary', danger = false, className = '', type = 'button', ...rest
}) {
  const cls = [VARIANTS[variant] ?? '', danger ? 'danger' : '', className]
    .filter(Boolean).join(' ')
  return <button type={type} className={cls || undefined} {...rest} />
}

// The save button, copy-pasted verbatim in Skills, Context, Artifacts, Agents
// and twice in Workspace's editor panels:
//   <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
// The label IS the state — there is no separate "saved ✓" affordance anywhere
// in the app — so it stays inside the component rather than becoming a prop.
// `disabled` is the extra condition Workspace's file editor adds (`|| !path`).
export function SaveButton({ dirty, onSave, disabled = false, ...rest }) {
  return (
    <Button onClick={onSave} disabled={!dirty || disabled} {...rest}>
      {dirty ? 'Save' : 'Saved'}
    </Button>
  )
}
