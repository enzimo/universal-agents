"""Model provider facts shared by the gateway, the broker and the agent runtime.

The broker must not import agent or gateway code
(``tests/test_runtime_architecture_boundaries.py``), yet all three need the
same answers: which address a model endpoint has, which environment variable
overrides its key, and how to check a key with one real request. Those
answers live here, in a package that imports no runtime host.
"""
