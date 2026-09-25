import { useCallback, useState } from 'react'
import Menu, { MenuItem, MenuSep } from './components/Menu.jsx'
import { modelCaption, modelGroups, modelOption, setModel, useModel } from './modelInfo.js'
import { notifyError } from './notify.js'

// The model chip both composers carry (the Chat page's and the shell's): the
// active model's label, opening the glass Menu upward with the enabled models
// grouped under their provider, price as the dim caption, the current one
// checked. Picking one PUTs /api/model (via modelInfo), which every other
// surface follows. With a single model there is nothing to pick, so the chip
// is a plain label with no chevron and no menu.
//
// `chip` is the trigger's class (each composer styles its own chip), `wrap`
// the wrapper's; `floating` portals the menu for a composer inside something
// that clips.
export default function ModelPicker({ chip, wrap = '', floating = false, width = 280 }) {
  const m = useModel()
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  if (!m) return null
  const current = modelOption(m, m.active)
  const label = current.label || 'no model'
  if (m.models.length < 2) {
    return (
      <span className={wrap || undefined}>
        <span className={`${chip} static`} title="model for new turns">{label}</span>
      </span>
    )
  }
  async function pick(id) {
    setOpen(false)
    if (id === current.id) return
    try { await setModel(id) } catch (err) { notifyError(err) }
  }
  const groups = modelGroups(m)
  const headed = groups.length > 1 || (groups[0] && groups[0].provider)
  return (
    <Menu open={open} onClose={close} up floating={floating} width={width}
          label="model" wrapClassName={wrap}
          trigger={(
            <button type="button" className={chip} aria-haspopup="menu" aria-expanded={open}
                    title="model for new turns" onClick={() => setOpen((o) => !o)}>
              <span className="ellipsis">{label}</span>
              <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
            </button>
          )}>
      {groups.map((g, i) => (
        <div key={g.provider || i} role="group" aria-label={headed ? g.label : undefined}
             className="model-group">
          {i > 0 && <MenuSep />}
          {headed && <div className="menu-caption">{g.label}</div>}
          {g.models.map((x) => (
            <MenuItem key={x.id} checked={x.id === current.id}
                      sub={modelCaption(x) || undefined} onClick={() => pick(x.id)}>
              <span className="ellipsis">{x.label}</span>
            </MenuItem>
          ))}
        </div>
      ))}
    </Menu>
  )
}
