"""Why the installation authority will not start (``AuthorityStartRefused`` and its kinds).

Its own module so that the parts of the start in modules of their own (``queues``) refuse
the same way as ``service``: the process logs ``event=installation_authority_start_refused``
with the refusal's kind and message and exits 1, and the launcher starts no other host.
"""

from __future__ import annotations


class AuthorityStartRefused(RuntimeError):
    """The authority cannot start without risking trust or queued work; the message says what the operator can do."""
