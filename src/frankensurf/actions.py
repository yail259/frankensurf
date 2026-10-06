"""Operation authority shared by Core, identities, and provider plugins."""

ACTION_CLASSES = (
    "READ_PUBLIC",
    "READ_AUTHENTICATED",
    "WRITE_REVERSIBLE",
    "WRITE_EXTERNAL",
    "PURCHASE/FINANCIAL",
    "ACCOUNT_SECURITY",
)

READ_ACTION_CLASSES = frozenset({"READ_PUBLIC", "READ_AUTHENTICATED"})
WRITE_ACTION_CLASSES = frozenset(ACTION_CLASSES) - READ_ACTION_CLASSES


def validate_action_classes(value, *, allow_empty=False):
    if (type(value) is not tuple
            or not allow_empty and not value
            or any(item not in ACTION_CLASSES for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(
            "action_classes must be distinct FrankenSurf action classes")
    return value


def required_read_action(identity):
    return "READ_AUTHENTICATED" if identity else "READ_PUBLIC"


def require_action(policy, action_class):
    """Reject an operation before acquisition when its class is not granted."""
    from .runtime import WebFailure

    if action_class not in policy.action_classes:
        raise WebFailure(
            "POLICY_DENIED",
            "Operation action class is not granted by effective policy")

