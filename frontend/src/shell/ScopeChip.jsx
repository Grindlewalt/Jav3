import { useCallback, useState } from 'react'
import Menu, { MenuItem, MenuSep } from '../components/Menu.jsx'

// Where this chat's work goes: a project, or nowhere (the chat's own file
// store). Two states, not the Chat page's three — "follow whatever project is
// loaded globally" is what made chats silently inherit the wrong project, and
// with the scope in the URL and on this chip it has no job left.
// `gone`: the chat is pinned to a project that was deleted since — the chip
// says so, dimmed, rather than showing a slug the menu doesn't list.
export default function ScopeChip({ projects, value, gone = false, onPick }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const name = gone ? '(deleted project)'
    : value ? (projects.find((p) => p.slug === value)?.name || value) : null
  const pick = (slug) => { setOpen(false); if (slug !== value) onPick(slug) }
  return (
    <Menu open={open} onClose={close} floating label="project" width={240}
          trigger={(
            <button type="button"
                    className={`sh-scope${value && !gone ? ' pinned' : ''}${gone ? ' gone' : ''}`}
                    aria-haspopup="menu" aria-expanded={open}
                    title={gone ? `was pinned to “${value}”, which has been deleted — pick a new scope`
                           : value ? `works in the ${name} project`
                                   : 'no project — files go to this chat’s own store'}
                    onClick={() => setOpen((o) => !o)}>
              <span className={`proj-dot${value && !gone ? ' pinned' : ' none'}`} aria-hidden="true" />
              <span className="ellipsis">{name || 'No project'}</span>
              <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
            </button>
          )}>
      <MenuItem checked={!value} sub="files go to this chat’s own store"
                onClick={() => pick(null)}>No project</MenuItem>
      {projects.length > 0 && <MenuSep />}
      {projects.map((p) => (
        <MenuItem key={p.slug} checked={value === p.slug} onClick={() => pick(p.slug)}>
          <span className="ellipsis">{p.name}</span></MenuItem>
      ))}
    </Menu>
  )
}
