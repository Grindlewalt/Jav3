import Button from '../components/Button.jsx'

// One thing waiting on the operator, inline in the transcript, with the
// decisions its API actually offers:
//
//   network host   Allow · Deny     approve adds the host to the allowlist
//                                    that governs this project (its own, or
//                                    the shared one) — there is no allow-once
//                                    in the egress API, so none is offered
//   git request    Approve · Reject  commit (or connect-and-push) happens on
//                                    approve, server-side, never from a tool
//   plan question  Retry · Done · Skip   the item asked for the operator
//
// The host, the commit message and the plan note are agent-produced text:
// they render as text, never as markup.

function paths(g) {
  try { return JSON.parse(g.paths || 'null') } catch { return null }
}

export default function ApprovalRow({ a, acting, onDecide, onReview }) {
  const busy = acting === a.key
  if (a.kind === 'egress') {
    return (
      <div className="sh-approval" role="group" aria-label={`network request for ${a.host}`}>
        <div className="sh-approval-what">
          <span className="sh-approval-kind">Network</span>
          <span className="sh-approval-main"><code>{a.host}</code> wants in
            {a.hit_count > 1 && <span className="dim"> · {a.hit_count} tries</span>}</span>
          {a.triage_verdict === 'flag' && a.triage_reason && (
            <span className="sh-approval-note">Auto review flagged it: {a.triage_reason}</span>
          )}
        </div>
        <div className="sh-approval-acts">
          <Button disabled={busy} onClick={() => onDecide(a, 'approve')}
                  title="add this host to the allowlist that governs this project">Allow</Button>
          <Button variant="ghost" danger disabled={busy}
                  onClick={() => onDecide(a, 'reject')}>Deny</Button>
        </div>
      </div>
    )
  }
  if (a.kind === 'git') {
    const files = paths(a)
    return (
      <div className="sh-approval" role="group" aria-label="git request">
        <div className="sh-approval-what">
          <span className="sh-approval-kind">{a.git_kind === 'remote' ? 'Push' : 'Commit'}</span>
          <span className="sh-approval-main">{a.message}</span>
          <span className="sh-approval-note">
            {a.git_kind === 'remote' ? 'connect this remote and push'
              : files?.length ? `${files.length} file${files.length > 1 ? 's' : ''}`
                : 'all changes'}</span>
        </div>
        <div className="sh-approval-acts">
          {onReview && <Button variant="ghost" onClick={() => onReview(a)}>Review</Button>}
          <Button disabled={busy} onClick={() => onDecide(a, 'approve')}>Approve</Button>
          <Button variant="ghost" danger disabled={busy}
                  onClick={() => onDecide(a, 'reject')}>Reject</Button>
        </div>
      </div>
    )
  }
  const it = a.item
  return (
    <div className="sh-approval" role="group" aria-label={`plan item ${it.title}`}>
      <div className="sh-approval-what">
        <span className="sh-approval-kind">Plan</span>
        <span className="sh-approval-main">{it.title}</span>
        {it.last_error && <span className="sh-approval-note">{it.last_error}</span>}
      </div>
      <div className="sh-approval-acts">
        <Button disabled={busy} onClick={() => onDecide(a, 'todo')}
                title="put it back in the queue for the runner">Retry</Button>
        <Button variant="ghost" disabled={busy} onClick={() => onDecide(a, 'done')}>Done</Button>
        <Button variant="ghost" disabled={busy} onClick={() => onDecide(a, 'skipped')}>Skip</Button>
      </div>
    </div>
  )
}
