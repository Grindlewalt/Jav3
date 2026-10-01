// Slash commands the terminal client has and the web app does not, each with
// the one line it answers instead of "unknown command". Pure (no React, no
// fetch), so node can test it against the terminal's own command table
// (src/slash/__tests__/terminal.test.mjs).
//
// A command the web app DOES have, under another meaning ("/web" here says
// where you are), lives in commands.js. This is only for the ones with nothing
// to run: they answer, that is all.

export const TERMINAL_ONLY = {
  themes: 'themes are the terminal client’s — here it is light or dark, the sun / moon '
    + 'button in the nav',
  'stop-all': 'stopping every running turn is the terminal client’s /stop-all — here '
    + '/stop ends this chat’s turn',
  'stop-a': '/stop-a stops a whole project’s turns in the terminal client — here /stop '
    + 'ends this chat’s turn',
  screenshot: '/screenshot saves the terminal’s screen — in the browser use its own '
    + 'screenshot tool',
  exit: 'there is nothing to quit in the browser — close the tab (/stop ends a running '
    + 'turn first)',
  permissions: 'the permission mode (yolo / auto / ask) is the picker in the chat’s '
    + 'toolbar, at the top of the chat',
  persona: 'agent presets live on the Agents page — /agents takes you there',
  dnd: 'do not disturb is in Settings → Notifications; while it is on, the top bar '
    + 'says so',
}

// the terminal client's aliases for the above
const ALIASES = { theme: 'themes', 'stop-project': 'stop-a', quit: 'exit',
  perms: 'permissions', mode: 'permissions' }

// name (any case, an alias fine) -> the line, or null
export function terminalAnswer(name) {
  const n = String(name || '').toLowerCase()
  return TERMINAL_ONLY[ALIASES[n] || n] || null
}

// Every name and alias the terminal client's _commands() registers, read from
// its source. The test uses this to hold the web app to "every terminal command
// is a command, an alias or a line here".
export function terminalCommandNames(source) {
  const start = source.indexOf('def _commands(self)')
  const end = source.indexOf('return c\n', start)
  const body = source.slice(start, end)
  const names = new Set()
  for (const m of body.matchAll(/^ {16}"([a-z][a-z0-9-]*)": Command\(/gm)) names.add(m[1])
  for (const m of body.matchAll(/\("([a-z][a-z0-9-]*)", "([a-z][a-z0-9-]*)"\)/g)) names.add(m[1])
  return [...names]
}
