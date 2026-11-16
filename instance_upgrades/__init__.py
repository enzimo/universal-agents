"""One-time steps for an instance upgraded in place.

The launcher runs each as a first-start step (``docker.launch_headless.build_first_start_tasks``),
before the gateway, the adapter host and the agents start, until the step's
completion record under ``state/upgrades`` exists (``InstanceLayout``). A step
may read and change both the gateway's stores and the team's, which is why
these live outside ``assistant_gateway``: the gateway runtime never imports
the team's stores (``tests/test_manager_control_runtime.py``). Each step goes
with the other upgrade steps of its release line when the next minor release
ships (README_ARCHITECTURE.md, "Upgrading an instance in place").
"""
