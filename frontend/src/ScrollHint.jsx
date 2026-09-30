import { useEffect, useRef, useState } from 'react'

// Wraps a horizontally scrolling strip (the Security tab bar on a phone: six
// tabs in 356px, so Logs and Secrets sat off-screen with nothing to say so) and
// paints a fade and an arrow on whichever edge has more to scroll to. The
// active tab is scrolled into view on mount, so the strip never opens on a
// route it is showing off-screen. Purely presentational: it does not change
// what the strip does.

export default function ScrollHint({ children }) {
  const wrap = useRef(null)
  const [more, setMore] = useState({ left: false, right: false })

  useEffect(() => {
    const strip = wrap.current?.firstElementChild
    if (!strip) return undefined
    const measure = () => setMore({
      left: strip.scrollLeft > 4,
      right: strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 4,
    })
    strip.querySelector('.active, .on')?.scrollIntoView({ inline: 'center', block: 'nearest' })
    measure()
    strip.addEventListener('scroll', measure, { passive: true })
    window.addEventListener('resize', measure)
    return () => {
      strip.removeEventListener('scroll', measure)
      window.removeEventListener('resize', measure)
    }
  }, [])

  const cls = ['scroll-hint', more.left ? 'more-left' : '', more.right ? 'more-right' : '']
    .filter(Boolean).join(' ')
  return <div ref={wrap} className={cls}>{children}</div>
}
