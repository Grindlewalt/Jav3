import BackupPanel from '../../BackupPanel.jsx'
import Advanced from './Advanced.jsx'
import MusicPanel from './MusicPanel.jsx'
import SessionPanel from './SessionPanel.jsx'

// The server itself: its backup, the music server it talks to, and the door.
export default function SystemTab() {
  return (
    <>
      <BackupPanel />
      <Advanced ids={['music']}>
        <MusicPanel />
      </Advanced>
      <SessionPanel />
    </>
  )
}
