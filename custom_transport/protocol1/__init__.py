"""The runtime side of protocol 1: host keys, trust records and signed live messages.

The message contract (``contracts/messages/v1``) defines frames, records and who may
send what; ``custom_messages.wire`` signs and verifies them. This package gives a
running host what it needs to use them:

- ``host_keys``: each host's application key and NATS key, and the installation
  authority's key, kept in the instance's ``trust/`` folder
  (``runtime_settings.instance_layout``);
- ``bootstrap``: the bundle every host starts from (the authority keys and the
  records a new host needs before it has a certificate);
- ``trust_state``: a trust store that keeps the newest revocations record and block
  list sequence across restarts, as the contract requires;
- ``identity``: who this host is when it signs (its certificate's ids, and the
  agent sessions it holds);
- ``live``: signed requests, replies and served addresses over NATS, with the
  receiver's checks and its replay record;
- ``state_records``: signed records in the key-value buckets.

Only ``live`` and ``state_records`` touch NATS, through the connection the
transport adapter owns (``custom_transport.nats_adapter``).
"""
