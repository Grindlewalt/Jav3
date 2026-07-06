import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Md from '../Md.jsx'

// Walk past agent-job trees. Cascading fidelity: the tree loads shallow (a run
// and its top rollup), and each node's rollup expands on click.
export default function Runs() {
  const [runs, setRuns] = useState([])
  const [selected, setSelected] = useState(null)
  const [nodes, setNodes] = useState([])
  const [open, setOpen] = useState({})

  useEffect(() => { api('/api/runs').then((r) => setRuns(r.runs)) }, [])

  async function openRun(cid) {
    setSelected(cid); setOpen({})
    const r = await api(`/api/runs/${cid}/tree?depth=full`)
    setNodes(r.nodes)
  }

  // order nodes as a DFS from the root so the tree reads top-down
  function ordered() {
    const byParent = {}
    for (const n of nodes) (byParent[n.parent_conversation_id] ??= []).push(n)
    const root = nodes.find((n) => n.parent_conversation_id == null)
    const out = []
    const walk = (node, depth) => {
      out.push({ ...node, depth })
      for (const c of byParent[node.id] || []) walk(c, depth + 1)
    }
    if (root) walk(root, 0)
    return out
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Runs</div>
        <ul className="file-list">
          {runs.map((r) => (
            <li key={r.id} className={selected === r.id ? 'active' : ''}
                onClick={() => openRun(r.id)}>
              <span className="grow ellipsis">{r.summary?.replace(/^\[head\]\s*/, '') || `#${r.id}`}</span>
              {r.project_slug && <span className="tag">{r.project_slug}</span>}
            </li>
          ))}
          {runs.length === 0 && <li className="dim">no runs yet — start one from a project's Research panel</li>}
        </ul>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">pick a run to walk its tree</div>
        ) : (
          <div className="run-tree" style={{ padding: '4px 2px' }}>
            {ordered().map((n) => (
              <div key={n.id} className="run-node" style={{ marginLeft: n.depth * 18 }}>
                <div className="run-row"
                     onClick={() => n.rollup && setOpen((o) => ({ ...o, [n.id]: !o[n.id] }))}>
                  <span className={`tag ${n.kind === 'head' ? 'done' : 'running'}`}>{n.kind}</span>
                  <span className="grow ellipsis">{n.summary?.replace(/^\[\w+\]\s*/, '')}</span>
                  {n.rollup && <span className="dim">{open[n.id] ? '▾' : '▸'}</span>}
                </div>
                {open[n.id] && n.rollup && <div className="run-rollup"><Md text={n.rollup} /></div>}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
