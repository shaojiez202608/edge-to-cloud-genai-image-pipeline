"""Edge-to-cloud generative image pipeline (prototype).

Shared code for the Lambda handlers, the local simulation mode, the device simulators and the CDK
app. Modules that describe cloud resources (policy documents, rule definitions, topic names) are the
single source of truth for both the deployed resources and the in-process stand-ins.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
