from backend.agent.tools.toolctx import require_project


async def run(title: str, description: str = "") -> str:
    from backend import gitea
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    if not gitea.enabled():
        return ("error: Gitea isn't set up on this Jav3, so there is nowhere to "
                "push. Use git_commit_request to ask the operator for a commit "
                "instead.")
    try:
        row = await gitea.create_push_request(slug, title, description)
    except ValueError as e:
        return f"error: {e}"
    except (gitea.GiteaError, gitea.GiteaOff, RuntimeError) as e:
        return f"error: could not open the pull request: {gitea.scrub(str(e))}"
    return (f"push request #{row['id']} filed: branch {row['branch']}, pull "
            f"request #{row['pr_number']} into main ({row['pr_url']}). Status "
            "pending — nothing reaches main until the operator approves (merges) it.")
