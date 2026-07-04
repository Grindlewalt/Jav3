import { marked } from 'marked'
import DOMPurify from 'dompurify'

export default function Md({ text }) {
  const html = DOMPurify.sanitize(marked.parse(text || '', { breaks: true }))
  return <div className="md-body" dangerouslySetInnerHTML={{ __html: html }} />
}
