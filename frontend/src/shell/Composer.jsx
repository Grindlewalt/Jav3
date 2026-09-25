import { useCallback, useEffect, useRef, useState } from 'react'
import Menu, { MenuItem, MenuSep } from '../components/Menu.jsx'
import Toggle from '../components/Toggle.jsx'
import ModelPicker from '../ModelPicker.jsx'
import { isPhone } from '../breakpoints.js'

// The shell's prompt: one box pinned under the transcript.
//
//   @agent ▾   who answers. On a fresh chat it picks the identity the new
//              conversation is created as (POST /api/chat {agent}); on an
//              existing thread identity is fixed at creation, so the menu
//              offers "new chat as …" instead.
//   model ▾    the runtime model switch (ModelPicker: enabled models grouped
//              by provider) — the same setting the Chat page's chip changes.
//   Temporary  fresh chats only: nothing is kept once you leave.
//
// Enter sends, Shift+Enter is a newline, ↑ in an empty box recalls the last
// prompt. There is no status line: the chat stream carries no usage events,
// so "context % · tokens" would be a number we made up.

function AgentPicker({ fresh, agents, agentSlug, agentName, onPick, onNewAs }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const choose = (slug) => { setOpen(false); (fresh ? onPick : onNewAs)(slug) }
  const label = agentSlug ? `@${agentSlug}` : '@jav3'
  return (
    <Menu open={open} onClose={close} up align="left" floating width={280}
          label={fresh ? 'answer as' : 'new chat as'}
          trigger={(
            <button type="button" className="sh-chip" aria-haspopup="menu" aria-expanded={open}
                    title={fresh ? 'who answers this new chat'
                                 : `this chat runs as ${agentSlug ? agentName(agentSlug) : 'Jav3'}`}
                    onClick={() => setOpen((o) => !o)}>
              <span className="ellipsis">{label}</span>
              <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
            </button>
          )}>
      {!fresh && <div className="menu-caption">New chat as</div>}
      <MenuItem checked={fresh ? !agentSlug : undefined} sub="the central assistant"
                onClick={() => choose('')}>Jav3</MenuItem>
      {agents.length > 0 && <MenuSep />}
      {agents.map((a) => (
        <MenuItem key={a.slug} checked={fresh ? agentSlug === a.slug : undefined}
                  sub={a.description || undefined} onClick={() => choose(a.slug)}>
          <span className="ellipsis">{a.name}</span></MenuItem>
      ))}
    </Menu>
  )
}

export default function Composer({
  value, onChange, onSend, busy, onStop, peakAsk, onPeakCancel, onPeakConfirm,
  fresh, agents, agentSlug, agentName, onPickAgent, onNewAs,
  temporary, onTemporary, history,
}) {
  const ta = useRef(null)

  // the box grows with the draft up to its CSS max-height
  useEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [value])

  // focus lands in the box whenever a chat opens, so typing just works — not
  // on a phone, where focusing throws the keyboard over the transcript
  useEffect(() => {
    if (!isPhone()) ta.current?.focus({ preventScroll: true })
  }, [fresh])

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!busy) onSend()
      return
    }
    if (e.key === 'ArrowUp' && !value) {
      const last = [...history].reverse().find((m) => m.role === 'user')
      if (last) { e.preventDefault(); onChange(last.content) }
      return
    }
    if (e.key === 'Escape' && busy) { e.preventDefault(); onStop() }
  }

  const who = agentSlug ? agentName(agentSlug) : 'Jav3'
  return (
    <form className="sh-composer" onSubmit={(e) => { e.preventDefault(); if (!busy) onSend() }}>
      {peakAsk && (
        <div className="peak-ask" role="alertdialog" aria-label="peak pricing confirmation">
          <span className="grow">Peak pricing right now — this reply costs 2×.</span>
          <button type="button" className="ghost" onClick={onPeakCancel}>Cancel</button>
          <button type="button" onClick={onPeakConfirm}>Send anyway</button>
        </div>
      )}
      <div className="sh-box">
        <div className="sh-input">
          <span className="sh-caret" aria-hidden="true">›</span>
          <textarea ref={ta} rows={1} value={value}
                    aria-label={`message ${who}`}
                    placeholder={temporary ? `Message ${who} (temporary)…` : `Message ${who}…`}
                    onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown} />
        </div>
        <div className="sh-bar">
          <AgentPicker fresh={fresh} agents={agents} agentSlug={agentSlug}
                       agentName={agentName} onPick={onPickAgent} onNewAs={onNewAs} />
          <ModelPicker chip="sh-chip" floating />
          {fresh && (
            <Toggle checked={temporary} onChange={onTemporary} label="temporary chat"
                    onText="Temporary" offText="Temporary" className="sh-temp"
                    title="nothing from this chat is kept once you leave it" />
          )}
          <span className="grow" />
          {busy
            ? <button type="button" className="send-btn stop" title="stop this turn (Esc)"
                      aria-label="stop" onClick={onStop}>◼</button>
            : <button type="submit" className="send-btn" title="send (Enter)"
                      aria-label="send" disabled={!value.trim()}>↑</button>}
        </div>
      </div>
    </form>
  )
}
