"""comms' registration with turns for session events (the `moxi.turns.listeners` group).

Standard library only, and imports nothing of comms: turns' endpoint reads this module
when it starts, and imports the function below only when it first asks it.

At a session's start (`ready`) comms answers layer one, the context block and the roster
snapshot; at each prompt it answers the roster's joins and leaves since its last look.
The text is comms'; turns carries it into the session as the event's added context.
"""

LISTENERS = (
    (("ready", "prompt"), "moxi_comms.listener:answer"),
)
