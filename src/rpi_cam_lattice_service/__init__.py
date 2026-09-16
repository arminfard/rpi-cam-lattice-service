"""rpi-cam-lattice-service — a Raspberry Pi camera Lattice integration built on the
Lattice SDK for gRPC (Connect) in Python.

A long-running daemon that publishes the camera as a stationary asset entity
once per second, registers its SRT video ingress with Lattice's VideoManager,
and acts as a Lattice agent executing ``Start`` / ``Stop`` tasks.

Subpackages:

* ``lattice``  — TLS transport, auth metadata, one thin client per Lattice API.
* ``camera``   — the camera's state, the entity built from it, the task-driven
                 stream control, and the video ingress lifecycle.
* ``tasking``  — the task definitions the camera advertises and the agent
                 handler that executes them.

Root modules: ``config`` (.env), ``logging_setup`` (JSON logs), ``service``
(threads, signals, shutdown), ``main`` (CLI wiring).
"""

__version__ = "0.1.0"
