import { Link, useLocation } from 'react-router-dom'
import Page from '../components/Page.jsx'
import EmptyState from '../components/EmptyState.jsx'

// There was no catch-all. An unknown path rendered the nav, the player and the
// toasts around a completely empty <Routes>, which looks exactly like a page
// that failed to load — and the two are worth telling apart, because one is a
// typo and the other is a bug.
export default function NotFound() {
  const { pathname } = useLocation()
  return (
    <Page title="Not found">
      <EmptyState pad hint="Pick a destination from the menu, or go back to the chat.">
        Nothing lives at <code>{pathname}</code>.
      </EmptyState>
      <p className="center-link"><Link to="/">Back to Chat</Link></p>
    </Page>
  )
}
