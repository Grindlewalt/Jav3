"""Guest-side `budget` shim. Enforcement lives host-side in the gateway, keyed
by op_id (the host registers the Budget before the turn and meters every
model_call). The guest only needs the exception type so loop.py can catch a
budget stop the gateway signals as {"type":"error","error":"BudgetExceeded"}."""


class BudgetExceeded(Exception):
    pass
