import ChatBox from '../ChatBox.jsx'
import { ReviewQueue } from '../pages/Review.jsx'
import { NetworkPanel } from '../pages/Network.jsx'
import PlanPanel from '../PlanPanel.jsx'
import EmptyState from '../components/EmptyState.jsx'
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
import DesktopPanel from '../panels/DesktopPanel.jsx'
import TaskBoardPanel from '../panels/TaskBoardPanel.jsx'
import TodoPanel from '../panels/TodoPanel.jsx'


// A window's body: the card component with the props the board always gave
// it (slug, project, refreshProject, state, setState, onToggleExpand).
// A chat window holds any conversation: state.chatProject (a slug, or null
// for no project; absent = the window's project) and state.conversation (an
// id, or 'new').
export default function WindowBody(props) {
  switch (props.type) {
    case 'chat': {
      const { chatProject, conversation } = props.state || {}
      const slug = chatProject === undefined ? props.slug : chatProject || undefined
      return <ChatBox key={slug || ''} projectSlug={slug} initialId={conversation}
                      onOpened={(c) => props.setState({ conversation: c })} />
    }

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
    case 'desktop': return <DesktopPanel {...props} />
    default: return <EmptyState pad>unknown window “{WINDOW_TYPES[props.type]?.title || props.type}”</EmptyState>
  }
}
