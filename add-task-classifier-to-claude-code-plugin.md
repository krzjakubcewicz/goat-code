# Add a Task Classifier to the Claude Code Multi-Agent Plugin

## Goal

Add a task classifier to the existing Claude Code plugin.

The current architecture already contains:

- planner agent
- developer agent
- verifier agent
- Claude Code hooks
- deterministic policy/harness
- tool authorization
- audit logging
- verification gates

The classifier must determine the complexity and risk of an incoming coding task **before deciding which agent workflow should be executed**.

The goal is to optimize for efficiency:

- Simple tasks should not invoke planner or verifier unnecessarily.
- Normal tasks should use the developer with deterministic verification.
- Complex tasks should use planner → developer → deterministic verification → verifier.
- High-risk tasks should use planner → developer → deterministic verification → independent verifier → human approval when required.

The classifier is **not a security boundary**.

The LLM may classify a task, but deterministic infrastructure must enforce security policy and the selected workflow.

---

## Required Classification

Introduce:

```text
SIMPLE
NORMAL
COMPLEX
HIGH_RISK
```

Suggested routing:

```text
SIMPLE
  → developer
  → deterministic verification
  → done

NORMAL
  → developer
  → deterministic verification
  → done

COMPLEX
  → planner
  → developer
  → deterministic verification
  → verifier
  → done

HIGH_RISK
  → planner
  → developer
  → deterministic verification
  → independent verifier
  → human approval if required
  → done
```

The routing must be configurable rather than hardcoded throughout the codebase.

---

## Classification Criteria

The classifier should evaluate two independent dimensions:

### Complexity

Consider factors such as:

- number of files likely to change
- number of modules/components involved
- architectural changes
- cross-service changes
- database/schema changes
- API changes
- concurrency/distributed-system changes
- unclear requirements
- need for substantial repository exploration

### Risk

Consider factors such as:

- authentication/authorization
- cryptography
- secrets/credentials
- security-sensitive code
- CI/CD configuration
- production deployment
- infrastructure
- dependency changes
- data deletion
- database migrations
- permission changes
- code affecting customer data

Risk must be evaluated independently from complexity.

For example:

```text
"Change authentication logic in one file"
```

may be SIMPLE from a code-size perspective but HIGH_RISK from a security perspective.

---

## Domain Model

Adapt these models to the existing project's conventions:

```java
public enum TaskComplexity {
    SIMPLE,
    NORMAL,
    COMPLEX
}
```

```java
public enum TaskRisk {
    LOW,
    MEDIUM,
    HIGH,
    CRITICAL
}
```

```java
public record TaskClassification(
    TaskComplexity complexity,
    TaskRisk risk,
    TaskClass taskClass,
    String reasoning,
    List<String> riskFactors,
    List<String> complexityFactors
) {}
```

Do not blindly introduce these exact classes if equivalent domain objects already exist.

Reuse existing abstractions where possible.

---

## Classifier Architecture

Create a clear abstraction:

```java
public interface TaskClassifier {

    TaskClassification classify(TaskContext task);
}
```

The implementation may use an LLM, but it must reuse the existing LLM abstraction if one already exists.

Do not introduce a second LLM client.

The classifier should request structured output.

Example:

```json
{
  "complexity": "COMPLEX",
  "risk": "HIGH",
  "riskFactors": [
    "authentication",
    "authorization"
  ],
  "complexityFactors": [
    "multiple modules",
    "API changes"
  ],
  "reasoning": "The task changes authentication behavior across multiple modules."
}
```

Validate the response against a strict schema.

Invalid model output must result in a safe fallback.

---

## Deterministic Security Override

The classifier must never be able to reduce a deterministic security requirement.

Example:

```text
LLM:
risk = LOW

Deterministic policy:
task modifies authentication code

Final:
risk = HIGH
```

The deterministic policy is authoritative.

The architecture should therefore be:

```text
LLM classification
        +
deterministic risk rules
        ↓
final classification
        ↓
workflow routing
```

Introduce a deterministic risk evaluation abstraction if one does not already exist:

```java
public interface RiskPolicy {

    RiskAssessment evaluate(TaskContext task);
}
```

The deterministic layer must be able to override the LLM classification.

---

## Workflow Router

Introduce a single clear routing abstraction:

```java
public interface WorkflowRouter {

    Workflow select(TaskClassification classification);
}
```

Possible workflows:

```java
public enum Workflow {
    DIRECT_DEVELOPMENT,
    PLANNED_DEVELOPMENT,
    HIGH_RISK_DEVELOPMENT
}
```

Do not scatter routing logic across agents.

The workflow selection must happen in one clear place.

---

## State Machine Integration

Integrate classification into the existing execution state machine.

Possible flow:

```text
CREATED
   ↓
CLASSIFYING
   ↓
CLASSIFIED
   ↓
PLANNING             (if required)
   ↓
IMPLEMENTING
   ↓
VERIFYING
   ↓
REVIEWING            (if required)
   ↓
WAITING_FOR_APPROVAL (if required)
   ↓
COMPLETED
```

The LLM must never directly manipulate execution state.

Only deterministic application logic may transition execution state.

---

## Efficiency Requirements

The classifier should be optimized for latency and token cost.

Do not automatically use the most expensive model.

If the existing LLM abstraction supports model selection, introduce a configurable classifier model.

Example:

```yaml
agent:
  classifier:
    enabled: true
    model: <configured-fast-model>
    max-tokens: <configured-value>
    timeout: <configured-value>
```

The classifier should use minimal context initially.

Do not send the entire repository to the classifier.

Prefer:

- task description
- repository metadata
- branch metadata
- available task metadata
- optionally a small amount of relevant context

Repository exploration should happen only when necessary.

---

## Fail-Safe Behavior

If classification fails because of:

- LLM timeout
- malformed JSON
- invalid enum
- provider error
- rate limit
- unavailable model
- schema validation failure

do not fail open.

Use a conservative deterministic fallback.

For example:

```text
classification failure
        ↓
NORMAL / conservative workflow
        ↓
deterministic verification
```

If deterministic rules identify a security-sensitive task, always use the HIGH_RISK path regardless of classifier availability.

---

## Auditability

Record classification in the existing audit system.

Record at least:

- execution ID
- classifier version
- model
- prompt version
- classification
- complexity
- risk
- risk factors
- complexity factors
- workflow selected
- deterministic overrides
- timestamp
- fallback reason, if applicable

Example:

```json
{
  "event": "TASK_CLASSIFIED",
  "executionId": "...",
  "complexity": "COMPLEX",
  "risk": "HIGH",
  "workflow": "HIGH_RISK_DEVELOPMENT",
  "deterministicOverrides": [
    "authentication code detected"
  ]
}
```

Do not log secrets or sensitive repository content.

---

## Testing

Add comprehensive tests.

### Unit Tests

Cover:

- SIMPLE classification
- NORMAL classification
- COMPLEX classification
- HIGH_RISK classification
- malformed LLM response
- unknown enum
- timeout
- provider failure
- deterministic risk override
- workflow routing
- conservative fallback

### Security Tests

Verify:

```text
LLM says LOW
+
task touches authentication
=
HIGH_RISK
```

and:

```text
LLM says SIMPLE
+
task modifies CI/CD
=
HIGH_RISK or policy-controlled workflow
```

The classifier must never bypass deterministic policy.

### Integration Tests

Verify complete flows:

```text
SIMPLE
→ developer
→ verification
```

```text
COMPLEX
→ planner
→ developer
→ verification
→ verifier
```

```text
HIGH_RISK
→ planner
→ developer
→ verification
→ verifier
→ approval
```

---

## Configuration

Make classification and routing configurable.

Avoid hardcoding business policy into the classifier prompt.

The prompt should describe classification criteria.

Deterministic policy should enforce security requirements.

If appropriate for the existing configuration strategy, support:

```yaml
agent:
  classifier:
    enabled: false
```

When classification is disabled, existing behavior must remain unchanged.

---

## Backward Compatibility

This is an addition to an existing working solution.

Before implementing:

1. Inspect the current architecture.
2. Identify existing interfaces and abstractions.
3. Reuse the existing LLM client.
4. Reuse the existing tool registry.
5. Reuse the existing policy engine.
6. Reuse the existing state machine.
7. Reuse the existing audit system.
8. Reuse existing repositories.
9. Do not duplicate existing functionality.
10. Preserve current behavior when classification is disabled.

---

## Deliverables

Implement the complete feature, not just interfaces.

Provide:

1. classifier implementation
2. structured output schema
3. classifier prompt
4. deterministic risk evaluation
5. workflow router
6. state machine integration
7. configuration
8. audit events
9. unit tests
10. integration tests
11. documentation explaining the architecture

---

## Engineering Principles

Follow these principles throughout the implementation:

1. The LLM is probabilistic.
2. The classifier is advisory.
3. Deterministic policy is authoritative.
4. The classifier must never be a security boundary.
5. The harness controls execution.
6. The LLM cannot authorize its own actions.
7. Verification must remain independent.
8. Prefer deterministic rules whenever possible.
9. Fail safely.
10. Optimize for minimal latency and token usage.
11. Reuse existing abstractions.
12. Keep the implementation production-grade and testable.

---

## Final Validation

Before finishing:

1. Inspect all existing relevant code.
2. Implement the feature rather than merely describing it.
3. Run the existing test suite.
4. Run all new tests.
5. Fix regressions.
6. Verify that disabling the classifier preserves existing behavior.
7. Verify that deterministic policy can override an unsafe LLM classification.
8. Verify that the classifier cannot bypass authorization or verification.
9. Provide a concise summary of the implementation and tests executed.
