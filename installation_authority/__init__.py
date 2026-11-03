"""The installation authority: the process that vouches for the installation's hosts.

The message contract's trust starts here (README "Who sent it", "Installation
traffic"). The authority signs every host's membership certificate with the
installation's authority key, signs the revocations record and renews it every six
hours, keeps the bootstrap bundle current, and owns the JetStream objects other roles
only use (``transport.yaml`` ``topology_owner``). It runs no model.

In the embedded profile (one machine, the default), the launcher lists the hosts it
starts and their public keys (``expected_hosts``); the authority approves a join from
exactly those and refuses every other, so a local installation can never take in a
host nobody approved. Joins from other machines come with administrator approval in a
later phase.
"""
