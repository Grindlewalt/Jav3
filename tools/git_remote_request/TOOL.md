---
name: git_remote_request
description: Request connecting a GitHub remote (https://github.com/owner/repo) to the current project. Nothing connects until the operator approves — approval verifies the repo, connects it, and pushes existing commits.
when_to_use: When the operator gives you a GitHub repo URL to hook up, or asks to get the project onto GitHub. File this INSTEAD of ever running git remote/push yourself — the VM has no credentials and its git state is discarded.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: The repo URL, exactly https://github.com/<owner>/<repo> (https, no credentials in the URL).
  required: [url]
---
Files a remote-connect request into the operator's Git panel. On approval the
host verifies the repo is reachable with the operator's token, connects it as
origin, and pushes all existing commits. After that, every approved commit
request auto-pushes. The token stays host-side: you can never read it, and
git inside the VM will never have it — do not try. If the operator has not
set a GITHUB_TOKEN secret, approval fails with a clear error and they add it
in the Secrets tab.
