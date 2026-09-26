import ChatBox from '../ChatBox.jsx'
import { ReviewQueue } from '../pages/Review.jsx'
import { NetworkPanel } from '../pages/Network.jsx'
import PlanPanel from '../PlanPanel.jsx'
import VmStrip from '../VmStrip.jsx'
import Toggle from '../components/Toggle.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import { WINDOW_TYPES } from './types.js'
import JournalPanel from '../panels/JournalPanel.jsx'
import EditorPanel from '../panels/EditorPanel.jsx'
import RendererPanel from '../panels/RendererPanel.jsx'
import OrganizerPanel from '../panels/OrganizerPanel.jsx'
import RunPanel from '../panels/RunPanel.jsx'
import ContextPanel from '../panels/ContextPanel.jsx'
import AgentPanel from '../panels/AgentPanel.jsx'
import ResearchPanel from '../panels/ResearchPanel.jsx'
import GitPanel from '../panels/GitPanel.jsx'
import GrantsPanel from '../panels/GrantsPanel.jsx'
import TerminalPanel from '../panels/TerminalPanel.jsx'
import TaskBoardPanel from '../panels/TaskBoardPanel.jsx'
import TodoPanel from '../panels/TodoPanel.jsx'


// A window's body: the card component with the props the board always gave
// it (slug, project, refreshProject, state, setState, onToggleExpand).
export default function WindowBody(props) {
  switch (props.type) {
    case 'chat': return <ChatBox projectSlug={props.slug} />
    case 'journal': return <JournalPanel {...props} />
    case 'editor': return <EditorPanel {...props} />
    case 'renderer': return <RendererPanel {...props} />
    case 'organizer': return <OrganizerPanel {...props} />
    case 'run': return <RunPanel {...props} />
    case 'todos': return <TodoPanel {...props} />
    case 'git': return <GitPanel {...props} />
    case 'board': return <TaskBoardPanel {...props} />
    case 'context': return <ContextPanel {...props} />
    case 'agent': return <AgentPanel {...props} />
    case 'research': return <ResearchPanel {...props} />
    case 'plan': return <PlanPanel {...props} />
    case 'review': return (
      <div className="pane-col">
        <div className="review-scrollwrap"><ReviewQueue slug={props.slug} /></div>
      </div>
    )
    case 'network': return <NetworkPanel slug={props.slug} />
    case 'secrets': return <GrantsPanel slug={props.slug} />
    case 'terminal': return <TerminalPanel slug={props.slug} />
    case 'vm': return <VmWindow {...props} />
    default: return <EmptyState pad>unknown window “{WINDOW_TYPES[props.type]?.title || props.type}”</EmptyState>
  }
}

// What the old project page's header held: the "in Jav3's context" switch and
// the VM strip (disk, persist, reset).
function VmWindow({ slug, project, refreshProject }) {
  return (
    <div className="pane-col work-vm">
      <Toggle checked={!!project?.loaded} label="loaded into Jav3's context"
              onText="in context" offText="not in context"
              title={project?.loaded
                ? 'Jav3 is working in this project — switch off to unload it'
                : 'load this project into Jav3\'s context'}
              onChange={async (on) => {
                try {
                  await api(on ? `/api/projects/${slug}/load` : '/api/projects/unload',
                            { method: 'POST' })
                } catch (err) { notifyError(err) }
                refreshProject()
              }} />
      <VmStrip slug={slug} />
    </div>
  )
}
