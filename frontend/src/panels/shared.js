// Helpers shared by the board/dock panels (moved out of pages/Workspace.jsx).
export const TEXT_EXT = /\.(md|txt|py|js|jsx|ts|json|html|css|csv|toml|yaml|yml|sh|tex)$/i
export const IMG_EXT = /\.(png|jpg|jpeg|gif|svg|webp)$/i
export const MEDIA_EXT = /\.(html?|pdf|png|jpg|jpeg|gif|svg|webp)$/i

// a project file as the browser loads it: served inert by /raw (see the
// same-origin hardening in backend), so HTML never executes from it
export const rawUrl = (slug, p) =>
  `/api/projects/${slug}/raw/${p.split('/').map(encodeURIComponent).join('/')}`

export function upLast(list, fn) {
  const copy = [...list]
  copy[copy.length - 1] = fn(copy[copy.length - 1])
  return copy
}
