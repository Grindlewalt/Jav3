import { useState } from 'react'

// Copies what it is GIVEN, not what is on screen. The set-up command once
// rendered a placeholder until a token was revealed, and copying the rendered
// text meant pasting the literal "<reveal the token above>" into a terminal.
export function Copy({ text, label = 'copy' }) {
  const [done, setDone] = useState(false)
  return (
    <button type="button" className="copy-btn" onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
      } catch {
        const ta = document.createElement('textarea')   // no secure context
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        ta.remove()
      }
      setDone(true)
      setTimeout(() => setDone(false), 1600)
    }}>{done ? 'copied' : label}</button>
  )
}
