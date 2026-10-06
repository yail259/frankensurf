"""Public failures expose an opt-in Core repair operation; authority never expands."""


def recovery_handoff(receipt, *, repair_input=None):
    failure = receipt.get("failure") or {}
    code = failure.get("code", "UNKNOWN")
    named = bool(receipt.get("identity"))
    authority_failure = code.startswith("IDENTITY_") or code in {
        "AUTH_REQUIRED", "AUTH_EXPIRED", "POLICY_DENIED", "RESULT_EXPORT_DENIED"}
    if named or authority_failure:
        step = "owner_authority"
        instruction = "Inspect the enrolled identity/executor status and owner policy; resume only through authorized execution."
    elif code == "SCHEMA_CHANGED":
        step = "adapter_repair"
        instruction = "Compare local failure evidence with the exact current page, repair adapter invariants, then run fixtures and a live canary."
    elif code in {"BLOCKED", "CAPTCHA", "VISUAL_REQUIRED", "EMPTY_PAGE"}:
        step = "agent_browser_diagnosis"
        instruction = "Inspect the exact public page in an authorized browser, diagnose access and native navigation, then propose a reusable provider or adapter repair."
    elif code in {"TIMEOUT", "BUDGET_EXHAUSTED"}:
        step = "readiness_and_budget_diagnosis"
        instruction = "Inspect the failed acquisition stage and retained evidence; test explicit readiness or budget policy against the original assertions."
    elif code in {"PROVIDER_UNAVAILABLE", "PROVIDER_DOWN", "PLUGIN_DISABLED"}:
        step = "provider_health"
        instruction = "Inspect installed plugin health and configuration; test an authorized available provider before retrying."
    elif code == "RATE_LIMITED":
        step = "rate_limit_diagnosis"
        instruction = "Inspect response and provider rate policy before scheduling another attempt."
    else:
        step = "evidence_diagnosis"
        instruction = "Inspect the exact request and diagnostic trace; establish current source truth before changing routing or listing state."
    attempts = []
    artifacts = []
    for attempt in receipt.get("attempts", []):
        attempts.append({key: attempt[key] for key in
                         ("provider", "provider_version", "status", "failure", "failure_stage", "latency_ms")
                         if key in attempt})
        if not named and not authority_failure:
            for artifact in attempt.get("evidence", []):
                reference = {key: artifact[key] for key in ("path", "sha256", "bytes") if key in artifact}
                if reference and reference not in artifacts:
                    artifacts.append(reference)
    eligible = not named and not authority_failure and repair_input is not None
    return {"status": "available" if eligible else "operator_required",
            "executed": False,
            "operation": "repair" if eligible else None,
            "requires_explicit_invocation": eligible,
            "next_step": step,
            "instruction": instruction, "trace_id": receipt.get("trace_id"),
            "attempts": attempts, "local_artifacts": artifacts,
            **({"repair_input": repair_input} if eligible else {}),
            "failure_stage": receipt.get("failure_stage"),
            "authority": "existing_owner_policy" if named or authority_failure else "public_read",
            "validation_required": "Core replays original assertions against retained fixtures and browser truth; independent live canary, performance comparison and explicit hash-bound promotion remain required."}
