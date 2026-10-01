/* Settings: the things that are configured once and consulted everywhere.
 *
 * A layout route like Agents and Security: this shell owns the <h1> and the tab
 * strip, and each tab is a real URL (/settings, /settings/alerts,
 * /settings/access, /settings/system) rendered through the Outlet. The eleven
 * cards that used to run down one long page are split by what they are for:
 *
 *   Models   API providers
 *   Alerts   Notifications
 *   Access   Devices, Computer use, Browser use, Permission rules, and
 *            Grounding under "Advanced"
 *   System   Backup, Music server under "Advanced", Session (Log out)
 *
 * (Gitea left for the Git page.) Every card is the Card primitive and every
 * control Input/Select/Button, so one control is one size across the page. */
import { useEffect } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { Tabs } from '../components/index.js'
import Page from '../components/Page.jsx'
import ScrollHint from '../ScrollHint.jsx'
import ProvidersPanel from '../ProvidersPanel.jsx'
import { SETTINGS_TABS, hashId, settingsRedirect } from '../settingsTabs.js'
import AccessTab from './settings/AccessTab.jsx'
import NotificationsPanel from './settings/NotificationsPanel.jsx'
import SystemTab from './settings/SystemTab.jsx'

export function ModelsTab() { return <ProvidersPanel /> }
export function AlertsTab() { return <NotificationsPanel /> }
export { AccessTab, SystemTab }

// A link that names a card (/settings#notifications, #desk, #providers) lands
// on the tab that holds it; the card itself is scrolled to once it is there.
// The cards load their own data and a card in "Advanced" opens a moment after
// the tab does, so this looks for the element for a couple of seconds rather
// than once.
function useScrollToHash() {
  const { pathname, hash, key } = useLocation()
  useEffect(() => {
    const id = hashId(hash)
    if (!id) return undefined
    let tries = 0
    const look = () => {
      const el = document.getElementById(id)
      if (el && el.getClientRects().length) {
        // a card already near the top of the tab stays put, so the tab strip
        // above it is not scrolled away
        const top = el.getBoundingClientRect().top
        if (top < 64 || top > window.innerHeight * 0.7) el.scrollIntoView({ block: 'start' })
        return true
      }
      return ++tries >= 20
    }
    if (look()) return undefined
    const t = setInterval(() => { if (look()) clearInterval(t) }, 100)
    return () => clearInterval(t)
  }, [pathname, hash, key])
}

export default function Settings() {
  const { pathname, hash } = useLocation()
  useScrollToHash()
  // an old link's tab is not this one: go to the right one, replacing the
  // address so Back does not land on the redirect again
  const to = settingsRedirect(pathname, hash)
  if (to) return <Navigate to={to} replace />
  return (
    <Page title="Settings" className="settings-page"
          actions={(
            <ScrollHint><Tabs label="Settings sections" items={SETTINGS_TABS} /></ScrollHint>
          )}>
      <Outlet />
    </Page>
  )
}
