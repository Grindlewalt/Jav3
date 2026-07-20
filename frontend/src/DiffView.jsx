// Line-level diff for staged-vs-canonical review: plain LCS on lines, adjacent
// del+add pairs folded into "changed" rows. Bounded — big files fall back to
// the plain side-by-side text. Content is UNTRUSTED (it is whatever the agent
// wrote) and is only ever rendered as text nodes, never markup. Shared by the
// Workspace staging panel and the Review Center.
const DIFF_MAX_LINES = 400
function lineDiff(a, b) {
  const A = a.split('\n'), B = b.split('\n')
  if (A.length > DIFF_MAX_LINES || B.length > DIFF_MAX_LINES) return null
  const n = A.length, m = B.length
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1])
  const rows = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (A[i] === B[j]) { rows.push({ l: A[i], r: B[j], t: 'same' }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ l: A[i], r: null, t: 'del' }); i++ }
    else { rows.push({ l: null, r: B[j], t: 'add' }); j++ }
  }
  while (i < n) rows.push({ l: A[i++], r: null, t: 'del' })
  while (j < m) rows.push({ l: null, r: B[j++], t: 'add' })
  const out = []
  for (let k = 0; k < rows.length; k++) {
    if (rows[k].t === 'del' && rows[k + 1]?.t === 'add') {
      out.push({ l: rows[k].l, r: rows[k + 1].r, t: 'mod' }); k++
    } else out.push(rows[k])
  }
  return out
}

export default function DiffView({ oldText, newText }) {
  const rows = (oldText != null && newText != null)
    ? lineDiff(oldText, newText) : null
  if (!rows) {
    return (
      <div className="diff-view">
        <div className="diff-col">
          <div className="dim small">current</div>
          <pre>{oldText ?? '(new file)'}</pre>
        </div>
        <div className="diff-col">
          <div className="dim small">staged</div>
          <pre>{newText ?? '(deleted)'}</pre>
        </div>
      </div>
    )
  }
  const changed = rows.filter((r) => r.t !== 'same').length
  return (
    <div className="diff-view">
      <div className="diff-col">
        <div className="dim small">current · {changed} line{changed !== 1 && 's'} differ</div>
        <pre>{rows.map((r, i) => (
          <div key={i} className={`diff-line ${r.t === 'add' ? 'pad' : r.t}`}>
            {r.l ?? ' '}</div>))}</pre>
      </div>
      <div className="diff-col">
        <div className="dim small">staged</div>
        <pre>{rows.map((r, i) => (
          <div key={i} className={`diff-line ${r.t === 'del' ? 'pad' : r.t}`}>
            {r.r ?? ' '}</div>))}</pre>
      </div>
    </div>
  )
}
