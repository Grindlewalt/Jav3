import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api.js'
import { listProfiles } from './boxes/api/profiles.js'
import { postureItems } from './securityCopy.js'

// The top of the Queue tab: what is switched on right now, in one line, each
// item a link to where it is changed (WEB-12). A newcomer opening Security
// otherwise sees a list of alerts with no idea what the defaults are (network
// policy, whether a model is deciding things, where boxes run).
//
// Read once on mount: these change rarely and every one of them is edited on a
// page that reloads its own state. A fetch that fails just drops its item.

export default function Posture() {
  const [state, setState] = useState(null)
  useEffect(() => {
    let live = true
    Promise.all([
      listProfiles().catch(() => null),
      api('/api/egress/auto').catch(() => null),
      api('/api/reviewer').catch(() => null),
    ]).then(([profiles, auto, reviewer]) => {
      if (live) setState({ profiles, auto, reviewer })
    })
    return () => { live = false }
  }, [])
  if (!state) return null
  const items = postureItems(state)
  if (!items.length) return null
  return (
    <div className="posture" aria-label="what is switched on">
      {items.map((it) => (
        <Link key={it.label} to={it.to} className={`posture-item${it.tone ? ` ${it.tone}` : ''}`}
              title={it.title}>
          <span className="dim">{it.label}</span> <b>{it.value}</b>
        </Link>
      ))}
    </div>
  )
}
