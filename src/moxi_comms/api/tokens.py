"""comms' token store, for the box's own consumers (`moxi_comms.api.tokens`).

The store calls those consumers make: `TokenStore` (its one declared read,
`read_secret(owner, secret_id)`, and its one declared write, `write_secret`),
`TokenOwner`, `owner_for_role` and `TokenStoreError`. The roles are the ones comms knows
(`reader`, `daemon`, `operator`; `provisioner` only ever refused) and each role the
config file's `[secrets] roles` names; any other role is unknown and reaches no file.
"""

from __future__ import annotations

from ..tokens import TokenOwner, TokenStore, TokenStoreError, owner_for_role

__all__ = ["TokenStore", "TokenOwner", "owner_for_role", "TokenStoreError"]
