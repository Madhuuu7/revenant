"""revenant -- a durable agent runtime.

An agent run is a fold over an append-only event log. Nothing the agent does
that the outside world can observe, and nothing it reads that could differ
between two executions, happens outside that log. Given the log, re-executing
the agent reaches the same decisions in the same order; given the log and a
crash, the run continues from where it stopped without repeating its effects.
"""

__version__ = "0.1.0"
