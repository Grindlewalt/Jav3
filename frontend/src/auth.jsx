import { createContext, useContext } from 'react'

// Who is signed in, and the one way out. App.jsx provides it; Settings' Session
// card consumes it. Log out used to be a row at the foot of the nav's overflow
// menu and again at the foot of the phone drawer — a door on every screen for
// a thing done once a month. It lives in Settings now, and nothing in the nav
// needs to know a session exists.
//
//   user    {id, username} from /api/auth/me, or null while logged out
//   logout  async; clears the cookie server-side and drops `user`, which sends
//           the router to /login
export const AuthContext = createContext({ user: null, logout: async () => {} })

export const useAuth = () => useContext(AuthContext)
