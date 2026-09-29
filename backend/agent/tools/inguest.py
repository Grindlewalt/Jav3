"""Which tools run IN the guest, one list for both sides.

The host decides which handlers to ship (backend/vm/guest_pkg.py) and the guest
decides how to route a call (guest/backend/agent/tools/registry.py). Those were
two literals that had to match by hand: a tool added to one and not the other
either shipped a handler nothing routed to, or routed a call to a host that has
no handler for it (run_code exists only in the guest).

Pure: copied verbatim into the guest package (guest_pkg._COPY_MODULES).
"""

# clean tools that run IN the guest (against the pushed workspace); their handler
# is loaded locally. Everything else brokers to the host. run_code lives here and
# ONLY here — code execution exists nowhere on the host.
IN_GUEST_TOOLS = ("read_file", "list_files", "search_codebase", "crawl_codebase",
                  "write_file", "edit_file", "dashboard", "todo_update",
                  "run_code", "screenshot")

# in-guest tools that exist only with boxes on (the contract: flag off, the
# guest package is byte-for-byte today's). `screenshot` needs the desktop
# image variant and reports "needs the desktop image" on any other.
BOX_ONLY_TOOLS = frozenset({"screenshot"})

# in-guest tools the conversation's permission mode may hold for the operator
# (backend/permissions.py IN_GUEST_GATED): the host decides, over the broker,
# before they run
GATED_IN_GUEST = frozenset({"write_file", "edit_file", "run_code"})
