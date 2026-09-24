// The autonomy dial's four levels, named once. The Projects cards and the
// Workspace header each spelled them their own way ("full (commit)" on one,
// "full — + commit proposals" on the other), so the same setting read as two.
// `label` is what a select shows; `hint` is the longer gloss for a tooltip.
export const AUTONOMY = [
  { value: 'read_only', label: 'read-only', hint: 'observe only — no writes' },
  { value: 'stage', label: 'stage edits', hint: 'may write project files' },
  { value: 'gated', label: 'agents + research', hint: 'may also run agents and research' },
  { value: 'full', label: 'full (commit)', hint: 'may also propose commits' },
]

export const autonomyHint = (level) =>
  AUTONOMY.find((a) => a.value === level)?.hint || ''
