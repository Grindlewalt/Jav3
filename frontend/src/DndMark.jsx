import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { dndLeft, dndUntil } from './dnd.js'

// The top bar's mark while do-not-disturb is on: what the operator sees instead
// of toasts, so a quiet browser never reads as a broken one. It is a button to
// Settings → Notifications, where it is switched off. The time left ticks on
// its own clock; the server ends it (and says so with one summary).
export default function DndMark({ dnd }) {
  const navigate = useNavigate()
  const [, tick] = useState(0)
  useEffect(() => {
    if (!dnd?.until) return undefined
    const id = setInterval(() => tick((n) => n + 1), 30000)
    return () => clearInterval(id)
  }, [dnd?.until])
  if (!dnd) return null
  const left = dndLeft(dnd)
  const tip = `Do not disturb is on ${dndUntil(dnd)}. Nothing pings; alerts still count in `
    + `Security.${dnd.break_critical ? ' Critical alerts still break through.' : ''} `
    + 'Click to change it.'
  return (
    <button type="button" className="dnd-mark" title={tip}
            onClick={() => navigate('/settings/alerts#notifications')}>
      <span className="dnd-dot" aria-hidden="true" />
      <span>DND</span>
      {left && <span className="dnd-left">· {left}</span>}
    </button>
  )
}
