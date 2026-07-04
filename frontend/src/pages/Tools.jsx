import { useEffect, useState } from 'react'
import { api } from '../api.js'

// The grant list Jarvis will eventually get — placeholder catalogue until the
// tool layer is designed. Registered entries below come live from the registry.
const PLANNED = [
  { name: 'run_in_vm', phase: 'pass 2 · M3',
    desc: 'Execute code inside the nukeable QEMU sandbox — push files in, run, pull results back to the host.' },
  { name: 'monitored_run', phase: 'pass 2 · M4',
    desc: 'The final gate: run with host-side exec + network telemetry; trips nuke the VM instead of pushing.' },
  { name: 'git_push', phase: 'pass 2 · M5',
    desc: 'Host-side push after operator review of code + logs + diffs. The VM never pushes.' },
  { name: 'crawl_codebase', phase: 'pass 2 · M6',
    desc: 'Crawl uploaded code and write up what it does into project memory.' },
  { name: 'journal_update', phase: 'pass 2 · M6',
    desc: 'Update project.md on finish and refresh the all-projects rollup.' },
  { name: 'project_files', phase: 'tool layer',
    desc: 'Read / write / move files in the active project — the same API the workspace uses.' },
  { name: 'memory_write', phase: 'tool layer',
    desc: 'Edit soul / user / env / notes so Jarvis can remember things it learns.' },
  { name: 'web_search', phase: 'later',
    desc: 'Look things up on the web.' },
]

export default function Tools() {
  const [tools, setTools] = useState([])
  useEffect(() => { api('/api/tools').then((r) => setTools(r.tools)) }, [])

  return (
    <div className="page">
      <h2>Tools</h2>
      <p className="dim">what Jarvis is (and will be) allowed to do. Everything goes
        through the registry + one calling convention; granting = flipping
        <code> enabled</code> in the def once a handler exists.</p>

      <h3 className="section-h">Registered</h3>
      <div className="tool-grid">
        {tools.map((t) => (
          <div key={t.name} className="tool-card">
            <div className="row">
              <code className="grow">{t.name}</code>
              {t.enabled
                ? <span className="badge">granted</span>
                : <span className="tag">not granted</span>}
            </div>
            <p>{t.desc || t.description}</p>
            {t.when_to_use && <p className="dim small">use when: {t.when_to_use}</p>}
          </div>
        ))}
        {tools.length === 0 && <p className="dim">registry is empty</p>}
      </div>

      <h3 className="section-h">Planned grants</h3>
      <div className="tool-grid">
        {PLANNED.map((t) => (
          <div key={t.name} className="tool-card planned">
            <div className="row">
              <code className="grow">{t.name}</code>
              <span className="tag">{t.phase}</span>
            </div>
            <p>{t.desc}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
