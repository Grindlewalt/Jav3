"""music_download: pull a track from YouTube into the library."""
from backend import tarmac


async def run(url: str = "", job: str = "") -> str:
    if job:
        try:
            s = await tarmac.download_status(job)
        except tarmac.TarmacError as e:
            return f"error: {e}"
        state = s.get("state") or s.get("status") or "unknown"
        line = f"job {job}: {state}"
        if s.get("error"):
            line += f" — {s['error']}"
        if s.get("title"):
            line += f" ({s['title']})"
        return line
    if not url:
        return "error: give a YouTube url, or a job to check on"
    try:
        r = await tarmac.download(url)
    except tarmac.TarmacError as e:
        return f"error: {e}"
    j = r.get("job") or r.get("id")
    return (f"started ripping — job {j}. Call this again with job=\"{j}\" to see "
            f"whether it finished; it will not be searchable until it does.")
