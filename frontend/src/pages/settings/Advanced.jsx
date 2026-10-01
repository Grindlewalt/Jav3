import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { hashId } from '../../settingsTabs.js'

// The cards a tab keeps out of the way: set once, rarely looked at. It starts
// shut, and a link to one of the cards inside (/settings#grounding) opens it,
// because a card in a shut <details> cannot be scrolled to.
export default function Advanced({ ids, children }) {
  const { hash, key } = useLocation()
  const [open, setOpen] = useState(false)
  useEffect(() => { if (ids.includes(hashId(hash))) setOpen(true) }, [hash, key]) // eslint-disable-line
  return (
    <details className="settings-adv" open={open}
             onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary><span className="chev" aria-hidden="true">›</span> Advanced</summary>
      <div className="settings-adv-body">{children}</div>
    </details>
  )
}
