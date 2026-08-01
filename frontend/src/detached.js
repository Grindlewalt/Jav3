// A window that lives outside the browser.
//
// Two mechanisms behind one call. Chromium's Document Picture-in-Picture
// (`documentPictureInPicture.requestWindow`) gives a real chromeless OS window
// that floats above everything and drags anywhere on the desktop — no tab bar,
// no address bar, nothing to say it is a web page. Everywhere else we fall back
// to a plain popup: still a separate draggable window, but it sits in the
// normal window stack and wears whatever chrome the browser insists on.
//
// What deliberately does NOT move is the <audio> element. Adopting a playing
// media element into another document restarts it in most browsers, so the
// sound stays in the page that started it and only the CONTROLS are portalled
// out. Closing the float therefore never interrupts a track, and reopening it
// picks the same playback back up.
//
// The child document starts empty, so it gets: this page's stylesheets copied
// in (the design tokens live there), the theme attribute mirrored and kept in
// sync, and a background painted before first paint so there is no white flash.

const PIP = typeof window !== 'undefined' && 'documentPictureInPicture' in window

export const CAN_PIP = PIP

// On a phone a popup is a new tab, which is worse than the in-page card in
// every way, and there is no desktop to float onto — so the offer needs a
// pointing device either way, and the fallback needs room as well.
const media = (q) => typeof window !== 'undefined' && !!window.matchMedia?.(q).matches
export const CAN_DETACH = media('(pointer: fine)')
  && (PIP || media('(min-width: 700px)'))

// Same-origin sheets can be read rule by rule (this is how Vite's dev <style>
// blocks and the built stylesheet both arrive). Anything cross-origin throws on
// .cssRules and can only be re-linked by href.
function copyStyles(doc) {
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      const css = Array.from(sheet.cssRules).map((r) => r.cssText).join('\n')
      const style = doc.createElement('style')
      style.textContent = css
      doc.head.appendChild(style)
    } catch {
      if (!sheet.href) continue
      const link = doc.createElement('link')
      link.rel = 'stylesheet'
      link.href = sheet.href
      doc.head.appendChild(link)
    }
  }
}

// The float must follow the theme switch live, or it strands the operator with
// a dark mini-player over a light app.
function mirrorTheme(doc) {
  const apply = () => {
    doc.documentElement.dataset.theme = document.documentElement.dataset.theme || 'dark'
  }
  apply()
  const obs = new MutationObserver(apply)
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => obs.disconnect()
}

/**
 * Open a detached window and prepare its document as a portal target.
 * Must be called from a user gesture: both mechanisms require one.
 * Returns { win, dispose } — call dispose() when you stop rendering into it.
 */
export async function openDetached({ width = 384, height = 188, title = 'Jarvis' } = {}) {
  let win
  if (PIP) {
    win = await window.documentPictureInPicture.requestWindow({ width, height })
  } else {
    const left = Math.max(0, (window.screen?.availWidth || 1280) - width - 48)
    win = window.open('', 'jarvis-float',
      `popup=yes,width=${width},height=${height},left=${left},top=120`)
    if (!win) throw new Error('the browser blocked the pop-out window')
  }

  const doc = win.document
  // a named popup can be handed back with the last render still in it
  doc.head.replaceChildren()
  doc.body.replaceChildren()
  doc.title = title

  // painted inline first: the copied stylesheets land a frame later, and an
  // about:blank flash of white is exactly what a "native window" must not do
  const bg = getComputedStyle(document.documentElement)
    .getPropertyValue('--bg-soft').trim() || '#0a0a0b'
  doc.documentElement.style.background = bg
  doc.body.style.background = bg
  doc.body.className = 'jarvis-float'

  copyStyles(doc)
  const stopTheme = mirrorTheme(doc)

  // an orphaned float has a dead opener driving it — nothing would ever update
  // or close it again, so it goes when the page does
  const closeWithOpener = () => { try { win.close() } catch { /* already gone */ } }
  window.addEventListener('pagehide', closeWithOpener)

  return {
    win,
    dispose() {
      stopTheme()
      window.removeEventListener('pagehide', closeWithOpener)
    },
  }
}

/**
 * Call `fn` once when the detached window goes away. `pagehide` covers the
 * ordinary close; the poll covers the browsers that skip it for popups.
 * Returns a disposer that also stops `fn` from firing at all.
 */
export function watchClose(win, fn) {
  let done = false
  const stop = () => {
    done = true
    clearInterval(timer)
    win.removeEventListener('pagehide', fire)
  }
  const fire = () => {
    if (done) return
    stop()
    fn()
  }
  const timer = setInterval(() => { if (win.closed) fire() }, 1000)
  win.addEventListener('pagehide', fire)
  return stop
}
