import { useAuth } from '../../auth.jsx'
import { Button, Card } from '../../components/index.js'

// --- session ---------------------------------------------------------------------

// The door. Log out used to sit at the foot of the nav's overflow menu and
// again at the foot of the phone drawer — a way out on every screen for a
// thing done once a month. This card is the only one now.
export default function SessionPanel() {
  const { user, logout } = useAuth()
  return (
    <Card title="Session" headingLevel={2} id="session">
      <p className="dim small settings-note">
        Signed in as <strong>{user?.username}</strong>. Logging out ends this
        browser’s session; computers logged in with <code>jav3</code> keep
        their own tokens until revoked under Access.
      </p>
      <div className="settings-actions">
        <Button variant="ghost" onClick={logout}>Log out</Button>
      </div>
    </Card>
  )
}
