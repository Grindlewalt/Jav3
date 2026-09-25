import { useEffect, useRef, useState } from 'react'
import { api, chatStream } from '../api.js'
import { watchRun } from '../agentWatch.js'
import Md from '../Md.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { upLast } from './shared.js'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// Run any defined agent right here in the project. The run is pinned to THIS
// project (the slug rides the request), so several boards can run agents in
// different projects at the same time; edits apply live to the project files.
export default function AgentPanel({ slug, state, setState }) {
  const [agents, setAgents] = useState([])
  const [task, setTask] = useState('')
  const [log, setLog] = useState([])
  const [busy, setBusy] = useState(false)
  const [peakAsk, setPeakAsk] = useState(null)   // in-page; iOS eats confirm()
  const [runId, setRunId] = useState(null)       // the in-flight run, for Stop
  const bottomRef = useRef(null)
  const unwatch = useRef(null)
  const which = state.agent || ''

  useEffect(() => { api('/api/agents').then((r) => setAgents(r.agents)) }, [])
  // the run outlives this panel now, so releasing the watch on unmount is what
  // turns "operator walked away" into a notice
  useEffect(() => () => { unwatch.current?.() }, [])
  useEffect(() => {
    // contain the autoscroll to the log list (scrollIntoView scrolls the page)
    const box = bottomRef.current?.parentElement
    if (box) box.scrollTop = box.scrollHeight
  }, [log])

  async function run(confirmPeak = false) {
    if (!which || !task.trim() || busy) return
    setBusy(true)
    setLog((l) => [...l, { role: 'task', text: task }, { role: 'out', text: '' }])
    try {
      await chatStream(
        { task, confirm_peak: confirmPeak, project: slug }, (ev) => {
          if (ev.type === 'start') {
            setRunId(ev.conversation_id)
            unwatch.current?.()
            unwatch.current = watchRun(ev.conversation_id)
          }
          if (ev.type === 'tool')
            setLog((l) => upLast(l, (last) => ({ ...last, text: last.text + `\n⚙ ${ev.name}\n` })))
          if (ev.type === 'token')
            setLog((l) => upLast(l, (last) => ({ ...last, text: last.text + ev.text })))
          if (ev.type === 'final')
            setLog((l) => upLast(l, () => ({ role: 'out', text: ev.content })))
          if (ev.type === 'error')
            setLog((l) => upLast(l, () => ({ role: 'err', text: ev.message })))
        }, `/api/agents/${which}/run`)
      setTask('')
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) {
      setLog((l) => l.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        setPeakAsk(true)
      } else setLog((l) => [...l, { role: 'err', text: err.detail || String(err) }])
    }
    setRunId(null)
    setBusy(false)
  }

  // cancels the run server-side; its final "[Request interrupted]" event
  // arrives on the stream above and settles the log the normal way
  async function stopRun() {
    if (!runId) return
    try { await api(`/api/agents/runs/${runId}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }

  return (
    <div className="pane-col">
      <div className="row">
        <select className="grow" value={which}
                onChange={(e) => setState({ agent: e.target.value })}>
          <option value="">— pick an agent —</option>
          {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
        </select>
      </div>
      <div className="messages compact">
        {log.length === 0 && <EmptyState pad>
          {agents.length ? 'pick an agent and give it a task' : 'no agents yet — create one in the Agents tab'}</EmptyState>}
        {log.map((m, i) => (
          <div key={i} className={`msg ${m.role === 'task' ? 'user' : m.role === 'err' ? 'error' : 'assistant'}`}>
            {m.role === 'out'
              ? <div className="bubble"><Md text={m.text || (busy ? '…' : '')} /></div>
              : <pre>{m.text || (busy ? '…' : '')}</pre>}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      {peakAsk && (
        <div className="peak-ask compact" role="alertdialog"
             aria-label="peak pricing confirmation">
          <span className="grow">Peak pricing right now — running this agent costs 2×.</span>
          <button type="button" className="ghost"
                  onClick={() => setPeakAsk(null)}>Cancel</button>
          <button type="button"
                  onClick={() => { setPeakAsk(null); run(true) }}>Run anyway</button>
        </div>
      )}
      <form className="row" onSubmit={(e) => { e.preventDefault(); run() }}>
        <textarea className="grow" rows={2} value={task} placeholder="task for the agent…"
                  onChange={(e) => setTask(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); run() } }} />
        {busy && runId
          ? <button type="button" className="ghost danger" title="stop this run"
                    onClick={stopRun}>⏹</button>
          : <button type="submit" disabled={busy || !which}>{busy ? '…' : 'Run'}</button>}
      </form>
    </div>
  )
}
