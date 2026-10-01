import BrowserPanel from '../../BrowserPanel.jsx'
import DeskPanel from '../../DeskPanel.jsx'
import GroundingPanel from '../../GroundingPanel.jsx'
import PermissionRulesPanel from '../../PermissionRulesPanel.jsx'
import Advanced from './Advanced.jsx'
import DevicesPanel from './DevicesPanel.jsx'

// Who and what may reach this server and the computers it works on. Grounding
// (how a model finds a point on a screen) is set once, so it folds away.
export default function AccessTab() {
  return (
    <>
      <DevicesPanel />
      <DeskPanel />
      <BrowserPanel />
      <PermissionRulesPanel />
      <Advanced ids={['grounding']}>
        <GroundingPanel />
      </Advanced>
    </>
  )
}
