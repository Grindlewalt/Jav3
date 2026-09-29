from backend.agent.tools.toolctx import require_project


async def run(title: str, description: str = "") -> str:
    from backend import gitea
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    if not gitea.enabled():
        return ("error: Gitea isn't set up on this Jav3, so there is nowhere to "
                "push. Nothing was filed. Use git_commit_request to ask the "
                "operator for a commit instead.")
    try:
        row = await gitea.create_push_request(slug, title, description)
    except ValueError as e:
        return f"error: {e}"
    except gitea.GiteaOff:
        return ("error: Gitea isn't set up on this Jav3. Nothing was filed. Use "
                "git_commit_request to ask the operator for a commit instead.")
    except gitea.GiteaUnreachable as e:
        return (f"error: {gitea.scrub(str(e))}. No pull request was made and your "
                "files are safe in the project. Tell the operator Gitea is down; "
                "use git_commit_request meanwhile to record the work. Don't retry "
                "in a loop.")
    except (gitea.GiteaError, RuntimeError) as e:
        return (f"error: could not open the pull request: {gitea.scrub(str(e))}. "
                "Nothing was filed and your files are safe in the project. Tell "
                "the operator what this says; retrying with the same arguments "
                "will fail the same way.")
    files = row.get("files") or []
    listed = ("\n  " + "\n  ".join(files)) if files else ""
    others = row.get("others") or []
    note = ""
    if others:
        ids = ", ".join(f"#{o['id']}" for o in others)
        note = (f"\nEarlier push request(s) {ids} are still waiting. This one holds "
                "those changes too, so the operator can merge just this one and "
                "close the others.")
    return (f"push request #{row['id']} filed: pull request #{row['pr_number']} from branch "
            f"{row['branch']} into main ({row['pr_url']}).\n"
            f"It changes {len(files)} file(s):{listed}\n"
            "Status: pending. The operator reviews it in the Review Center or in "
            "Gitea and merges or closes it; nothing reaches main until then, and you "
            "won't see the outcome in this turn. Don't file it again. Carry on, and "
            "say in your reply that it is waiting for review." + note)
