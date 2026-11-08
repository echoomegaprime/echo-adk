"""ECHO SDK adapters for the existing native v2 API.

Installing this package does not register capabilities or expose a network listener.
The SDK host must authenticate and authorize callers before using an adapter.
"""

from .operations import OPERATIONS, Operation, OperationResult, SdkOperations, operation_catalog

__all__ = ["OPERATIONS", "Operation", "OperationResult", "SdkOperations", "operation_catalog"]
