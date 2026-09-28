---
name: git_push_request
description: Put the project's current changes up for the operator's review as a pull request on the host's Gitea. The host commits your changes onto a fresh agent/* branch, pushes it, and opens a PR into main. Nothing reaches main until the operator approves (merges) it.
when_to_use: When a piece of work is ready for the operator to review and merge. This is the ONLY way to push — never run git push, git remote or git credential commands yourself (the box has no credentials and its git state is discarded).
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    title:
      type: string
      description: One-line PR title (imperative, e.g. "Add data loader").
    description:
      type: string
      description: What changed and why, for the reviewer. Markdown is fine.
  required: [title]
---
Snapshots the project's live files (your write_file/edit_file changes apply
immediately) into one commit on top of main, on a branch the host names
`agent/<id>` — you do not choose the branch and you can never target main.
The host pushes it as the `jav3-agent` bot and opens a pull request; the
operator reviews the diff in the Review Center or in Gitea and approves
(merges) or rejects (closes) it. Main is protected: only the operator can
push or merge to it.

Never run `git push`, `git remote`, or put credentials anywhere: the box has
none, it cannot reach Gitea, and its git state is thrown away. If Gitea is
not set up on this Jav3, the tool says so — use git_commit_request instead.
