"""The shared knowledge base agents and people write together.

- ``vocabulary``: stored names (spaces, page types, statuses, id prefixes).
- ``records``: records and refusal codes shared by the service, store and callers.
- ``sensitive_content``: fixed rules for what is never stored or held for review.
- ``text``: page names, links, search terms, fingerprints and overlap checks.
- ``service``: the trusted rules between callers and the store
  (``storage.knowledge_repository``).

Design and rationale: ``ua-architecture`` ``docs/memory_and_context_architecture.md``,
section "Knowledge base".
"""
