# Engineering Methodology

**Version:** 0.1  
**Status:** Experimental  
**Purpose:** Define a repeatable engineering workflow for architecture, planning, AI-assisted implementation, adversarial review, and incremental delivery.

---

# 1. Purpose

This methodology defines how software projects should be analyzed, designed, planned, implemented, reviewed, and evolved.

It is designed primarily for a workflow where:

- architectural reasoning and technical decisions happen interactively;
- implementation is frequently delegated to coding agents;
- coding agents receive constrained and explicit implementation tasks;
- implementation results are reviewed before the project advances;
- architectural integrity is prioritized over development speed;
- changes are delivered incrementally.

The methodology separates:

> **Thinking about the system**  
> from  
> **Changing the system**

The purpose of this separation is to reduce accidental architectural decisions, uncontrolled scope expansion, regressions, and agent-driven complexity.

---

# 2. Core Philosophy

The methodology follows six central ideas.

## 2.1 Architecture before mechanics

Important structural decisions should be made before an implementation agent encounters them.

Coding agents may make local implementation decisions.

They should not silently make decisions that significantly affect:

- architecture;
- module boundaries;
- contracts;
- persistence models;
- security models;
- concurrency models;
- external protocols;
- public APIs;
- long-term extensibility.

When such a decision emerges during implementation, it should return to architectural discussion.

---

## 2.2 Incremental delivery

Large changes should be decomposed into small conceptual increments.

An increment should preferably:

- have one main objective;
- preserve previously accepted behavior;
- have explicit boundaries;
- be independently reviewable;
- have measurable acceptance criteria;
- avoid implementing future increments prematurely.

An increment is conceptual, not necessarily equivalent to one Git commit, although ideally they are closely related.

---

## 2.3 Explicit decisions

Important decisions should not exist only in conversation history.

They should eventually become one of:

- current architecture;
- accepted decision;
- documented constraint;
- roadmap item;
- explicitly accepted debt.

A suggestion is not a decision.

A hypothesis is not a decision.

An implementation detail is not automatically architecture.

---

## 2.4 Adversarial review

Review should not attempt to confirm that the implementation is correct.

Its primary purpose is to discover how the implementation may be wrong.

Reviews actively look for:

- hidden assumptions;
- regressions;
- invalid states;
- contract violations;
- race conditions;
- coupling;
- scope expansion;
- incomplete failure handling;
- architectural drift;
- insufficient testing.

The reviewer should prefer evidence over confidence.

---

## 2.5 Controlled complexity

Abstractions must justify their existence.

The project should avoid introducing infrastructure for hypothetical requirements merely because they may eventually exist.

Prefer:

> simple now, evolvable later

over:

> generic now, complicated forever.

---

## 2.6 The codebase is the final source of truth

Documentation describes intent.

Decisions describe rationale.

Tests describe expected behavior.

The running implementation describes reality.

If these disagree, the disagreement itself is a defect that must be resolved.

---

# 3. Levels of Work

Every activity should conceptually belong to one of three levels.

## 3.1 PROJECT

The Project level defines long-lived system concerns.

Examples:

- architectural style;
- fundamental principles;
- supported capabilities;
- major technology choices;
- system boundaries;
- security model;
- persistence strategy;
- roadmap.

Questions at this level include:

- What kind of system are we building?
- What should belong to the core?
- What are the architectural invariants?
- Which technologies will be canonical?
- Which concerns must remain optional?

---

## 3.2 ITERATION

An Iteration represents a meaningful capability or milestone.

Examples:

- Identity;
- Authentication;
- Authorization;
- WebSocket lifecycle;
- tenancy;
- audit subsystem;
- API usability.

An iteration may contain several implementation increments.

Its purpose is to move a bounded area of the system from one coherent state to another.

---

## 3.3 INCREMENT

An Increment is the smallest planned implementation unit.

Examples:

- introduce the authentication state model;
- add session persistence;
- enforce tenant filtering;
- introduce permission evaluation;
- fix reconnect semantics.

An increment should be:

- focused;
- bounded;
- testable;
- reviewable;
- reversible when reasonably possible.

The implementation agent normally operates at this level.

---

# 4. Engineering Roles

The methodology separates roles conceptually even when the same human or AI performs several of them.

## 4.1 Product / Problem Owner

Responsible for:

- defining the problem;
- determining desired outcomes;
- prioritizing capabilities;
- accepting business trade-offs.

---

## 4.2 Engineering Copilot

Acts primarily as:

- software architect;
- technical analyst;
- staff engineer;
- planning partner;
- adversarial reviewer.

Responsibilities include:

- challenge assumptions;
- identify missing decisions;
- expose trade-offs;
- maintain architectural coherence;
- decompose work;
- prepare implementation specifications;
- review results;
- track unresolved findings.

The Engineering Copilot should not default to implementation.

---

## 4.3 Implementation Agent

Responsible primarily for executing a well-defined increment.

Its normal responsibilities are:

- inspect the relevant code;
- implement the requested change;
- update tests;
- update directly affected documentation when requested;
- report deviations or blockers.

It should avoid silently expanding the architecture.

---

## 4.4 Reviewer

The reviewer evaluates implementation independently of the implementation process.

It should assume that subtle defects may exist even when:

- tests pass;
- code looks clean;
- the implementation agent reports success.

---

# 5. Lifecycle

The default development lifecycle is:

```text
Discovery
    ↓
Architecture
    ↓
Specification
    ↓
Increment Planning
    ↓
Implementation
    ↓
Adversarial Review
    ↓
Hardening
    ↓
Acceptance
    ↓
Next Increment
```

The process is iterative rather than strictly linear.

A later stage may expose information that requires returning to an earlier stage.

Example:

```text
Implementation
    ↓
Architectural ambiguity discovered
    ↓
Architecture
    ↓
Decision
    ↓
Updated Increment
    ↓
Implementation
```

Returning to an earlier stage is not considered failure.

Silently improvising around an unresolved architectural decision is.

---

# 6. Stage 1 — Discovery

Discovery determines what problem is actually being solved.

The goal is not yet to design the implementation.

Typical questions include:

- What is the current pain?
- Who or what consumes this capability?
- What behavior is required?
- What behavior is explicitly not required?
- What constraints already exist?
- What existing system behavior must be preserved?
- What failure modes matter?
- What assumptions are currently being made?

## Discovery outputs

Depending on the size of the problem:

- problem statement;
- goals;
- non-goals;
- constraints;
- known unknowns;
- candidate capabilities;
- major risks.

## Discovery exit condition

Discovery can advance when the problem is sufficiently understood to discuss architecture without relying primarily on guesses.

---

# 7. Stage 2 — Architecture

Architecture defines structural decisions and invariants.

The objective is not to predict the entire future.

The objective is to make the decisions required for the next meaningful part of the system.

Typical topics include:

- module boundaries;
- ownership;
- state models;
- persistence;
- protocols;
- failure semantics;
- dependency direction;
- concurrency;
- security boundaries;
- extensibility;
- external interfaces.

## Architecture should distinguish

### Decision

Something intentionally chosen and currently binding.

### Hypothesis

A likely direction that still requires validation.

### Constraint

Something the system must respect.

### Open Question

Something that has not yet been decided.

### Accepted Debt

A known deficiency intentionally postponed.

These categories must not be conflated.

## Architecture exit condition

Architecture is ready to advance when the upcoming work does not depend on unresolved structural decisions.

Not every possible future architectural question needs to be answered.

---

# 8. Stage 3 — Specification

Specification translates architecture into observable behavior.

It defines what the system should do without unnecessarily prescribing local implementation details.

A specification may include:

- state transitions;
- API behavior;
- error semantics;
- invariants;
- permission rules;
- failure behavior;
- concurrency expectations;
- compatibility requirements.

Prefer statements such as:

> A temporary server failure must not transition the client to an unauthenticated state.

over:

> Set `loggedIn = true` inside function X.

The first specifies behavior.

The second prematurely specifies implementation.

---

# 9. Stage 4 — Increment Planning

Once the desired behavior is understood, the work should be decomposed.

Each increment should contain, when relevant:

## Objective

What does this increment accomplish?

## Context

Why is the change necessary?

## Initial state

What already exists and must be preserved?

## Scope

What must be changed?

## Out of scope

What must explicitly not be changed yet?

## Invariants

What properties must remain true?

## Expected behavior

How should the system behave after the change?

## Acceptance criteria

How can completion be objectively evaluated?

## Risks

What could easily go wrong?

## Dependencies

What previous decisions or increments does this rely on?

## Likely affected areas

What modules or contracts are expected to change?

This is guidance, not a mandatory bureaucratic checklist.

Only include sections that materially improve the implementation contract.

---

# 10. Increment Design Rules

A good increment should have one dominant reason to exist.

Warning signs that an increment is too large include:

- several unrelated objectives;
- multiple architectural changes;
- large schema changes mixed with unrelated UI work;
- introducing infrastructure for later features;
- touching many modules with no clear invariant connecting them;
- acceptance criteria that require several distinct behaviors.

When possible, prefer:

```text
Increment 1
Define state model

Increment 2
Integrate state model into session checking

Increment 3
Integrate reconnect behavior

Increment 4
Harden failures and tests
```

instead of:

```text
Increment 1
Rewrite authentication
```

---

# 11. Implementation Handoff

The implementation agent should receive a constrained task.

The handoff should clearly communicate:

- what to accomplish;
- relevant current behavior;
- what must remain unchanged;
- explicit exclusions;
- architectural invariants;
- acceptance criteria.

Typical language should include:

> Implement ONLY this increment.

> Preserve the behavior introduced by previous increments.

> Do not implement future roadmap items.

> Do not introduce a new architectural mechanism unless required by the specification.

> If the increment requires an architectural decision not covered by the current specification, report it rather than silently choosing one.

The detailed handoff template may be maintained separately.

---

# 12. Architectural Escalation

Implementation should stop making local decisions when the decision has system-level consequences.

An issue should normally be escalated when it materially affects one or more of:

- public contracts;
- module boundaries;
- data ownership;
- storage schema strategy;
- concurrency model;
- authentication or authorization model;
- security assumptions;
- failure semantics;
- protocol semantics;
- cross-module dependency direction;
- backwards compatibility.

The implementation agent should report:

```text
ARCHITECTURAL DECISION REQUIRED

Context:
...

Decision required:
...

Why the current specification is insufficient:
...

Possible options:
...
```

Implementation does not necessarily need to stop entirely.

Unrelated work within the increment may proceed if safe.

---

# 13. Stage 5 — Implementation

During implementation:

1. inspect before changing;
2. make the smallest coherent change;
3. preserve existing accepted behavior;
4. avoid opportunistic refactoring unrelated to the increment;
5. update relevant tests;
6. report assumptions;
7. report unresolved issues;
8. avoid hiding failures through broad fallback behavior.

Implementation quality includes not only whether the happy path works, but whether failure semantics remain correct.

---

# 14. Scope Discipline

Scope expansion is one of the main risks of AI-assisted development.

During an increment, avoid automatically adding:

- future authentication mechanisms;
- generic plugin systems;
- new infrastructure layers;
- abstractions for hypothetical providers;
- unrelated refactors;
- new configuration surfaces;
- convenience APIs not required by the increment.

A potentially useful idea should normally become:

```text
FOLLOW-UP CANDIDATE
```

rather than silently entering the current implementation.

---

# 15. Stage 6 — Adversarial Review

Review starts from the assumption that the implementation may contain defects.

The objective is to find them before the project builds additional work on top.

Review should examine both:

- what changed;
- what the change may have unintentionally affected.

---

# 16. Review Dimensions

Reviews should consider the dimensions relevant to the increment.

## 16.1 Architecture

- Are module boundaries still respected?
- Did dependency direction change?
- Was a new abstraction introduced without justification?
- Did the implementation introduce an implicit architectural decision?

## 16.2 Correctness

- Does the happy path work?
- Are state transitions valid?
- Are invariants preserved?
- Can impossible states be represented?

## 16.3 Failure semantics

- What happens when dependencies are unavailable?
- Are temporary failures confused with permanent state?
- Are retries safe?
- Are partial failures represented correctly?

## 16.4 Concurrency

When applicable:

- race conditions;
- duplicate execution;
- lost updates;
- ordering assumptions;
- lease behavior;
- idempotency;
- atomicity.

## 16.5 Security

When applicable:

- authentication bypass;
- authorization bypass;
- privilege escalation;
- insecure defaults;
- tenant isolation;
- session handling;
- secret exposure;
- input validation.

## 16.6 Persistence

- transaction boundaries;
- consistency;
- migration safety;
- uniqueness;
- referential integrity;
- tenant scoping;
- destructive operations.

## 16.7 Contracts

- API compatibility;
- status semantics;
- event contracts;
- WebSocket contracts;
- error models;
- serialization.

## 16.8 Maintainability

- unnecessary coupling;
- duplication;
- unclear ownership;
- accidental complexity;
- leaky abstractions.

## 16.9 Tests

- missing critical paths;
- missing failure cases;
- tests coupled to implementation rather than behavior;
- false confidence caused by overly mocked tests.

## 16.10 Scope compliance

- Was everything requested implemented?
- Was anything not requested implemented?
- Were prior behaviors unintentionally rewritten?

## 16.11 Documentation

- Does documentation describe the current implementation?
- Are decisions still accurate?
- Did an architectural change occur without being recorded?

---

# 17. Finding Severity

Review findings should normally use the following severity scale.

## BLOCKER

The implementation should not advance.

Examples:

- serious correctness failure;
- security vulnerability;
- data corruption risk;
- fundamental architectural violation;
- broken primary workflow.

---

## HIGH

Must normally be fixed before the increment is accepted.

Examples:

- important failure path is incorrect;
- significant race condition;
- contract inconsistency;
- major regression;
- architecture likely to become expensive to correct later.

---

## MEDIUM

Relevant defect or design problem that does not necessarily block the current increment.

Examples:

- incomplete edge case;
- insufficient tests around secondary behavior;
- moderate maintainability problem.

---

## LOW

Minor issue with limited current impact.

Examples:

- local cleanup;
- naming inconsistency;
- small documentation mismatch.

---

## NICE TO HAVE

Improvement rather than defect.

These items should not silently become mandatory scope.

---

# 18. Finding Status

Findings should have explicit lifecycle states.

Recommended states:

```text
OPEN
PARTIALLY_RESOLVED
RESOLVED
ACCEPTED_RISK
SUPERSEDED
```

A new review should distinguish:

- previously resolved findings;
- still-open findings;
- regressions;
- newly introduced findings.

This prevents every review from behaving as if the project has no history.

---

# 19. Evidence-Based Review

Findings should be based on concrete evidence whenever possible.

Prefer:

> `connectOrLogin()` treats all `checkSession()` failures as unauthenticated, therefore a timeout redirects the user to login.

over:

> Authentication handling seems fragile.

A strong finding identifies:

- location;
- behavior;
- consequence;
- severity;
- expected correction.

---

# 20. Stage 7 — Hardening

After the main implementation is functionally correct, hardening focuses on robustness.

Hardening may include:

- failure cases;
- concurrency;
- idempotency;
- recovery;
- validation;
- observability;
- migrations;
- resource cleanup;
- test coverage;
- security;
- degraded dependency behavior.

Hardening should not be used as an excuse to defer fundamental correctness.

---

# 21. Stage 8 — Acceptance

An increment can be accepted when:

- its objective is satisfied;
- acceptance criteria are met;
- no unresolved BLOCKER remains;
- no directly related HIGH remains unless explicitly accepted;
- architecture remains coherent;
- tests provide reasonable confidence;
- documentation is sufficiently aligned;
- no accidental future scope was introduced.

Acceptance means:

> good enough to safely build the next increment on top.

It does not mean:

> perfect forever.

---

# 22. Engineering Gates

The lifecycle uses conceptual gates.

They are not intended as bureaucracy.

They exist to prevent expensive mistakes.

## Gate A — Problem clarity

Do we understand what problem we are solving?

## Gate B — Architecture readiness

Are the structural decisions needed for this work sufficiently resolved?

## Gate C — Increment readiness

Is the implementation task small, bounded, and unambiguous enough?

## Gate D — Implementation integrity

Did the implementation respect scope and architectural constraints?

## Gate E — Review integrity

Are blocking findings resolved?

## Gate F — Continuation safety

Is the current state safe enough to build the next increment on top?

A gate should only block progress when proceeding would materially increase risk or rework.

---

# 23. Definition of Ready

An increment is normally ready for implementation when:

- objective is clear;
- scope is bounded;
- important exclusions are known;
- relevant architectural decisions are settled;
- acceptance criteria exist;
- previous prerequisite increments are stable enough.

Not every implementation detail needs to be predetermined.

---

# 24. Definition of Done

An increment is normally done when:

- requested behavior exists;
- existing required behavior is preserved;
- relevant tests pass;
- failure semantics are adequate;
- review has been performed;
- blocking findings are resolved;
- relevant documentation reflects the result;
- no unresolved architectural decision was silently embedded.

---

# 25. Decision Management

Important decisions should be recorded separately from current architecture.

A decision record should normally include:

```text
ADR-XXX — Title

Status:
Proposed | Accepted | Superseded | Rejected

Context:
...

Decision:
...

Alternatives:
...

Consequences:
...

Follow-ups:
...
```

Not every programming choice deserves an ADR.

Record decisions when future contributors are likely to reasonably ask:

> Why is the system built this way?

---

# 26. Architecture vs Decision History

Architecture documentation describes:

> how the system works now.

Decision documentation describes:

> why it became that way.

Avoid filling architecture documentation with historical discussion.

Avoid making readers reconstruct the current architecture from ADRs.

---

# 27. Accepted Debt

Not every issue must be fixed immediately.

When knowingly postponing a relevant problem, record:

- what the problem is;
- why it is being accepted;
- impact;
- conditions under which it should be revisited.

Accepted debt is different from forgotten debt.

---

# 28. Documentation Hierarchy

Projects should maintain a clear hierarchy of information.

A recommended structure is:

```text
00_PROJECT.md
01_METHODOLOGY.md
02_ARCHITECTURE.md
03_DECISIONS.md
04_ROADMAP.md
05_REVIEW_LOG.md
06_AGENT_HANDOFF.md
```

Conceptually:

```text
METHODOLOGY
    ↓
How we work

PROJECT
    ↓
What we are building

ARCHITECTURE
    ↓
How it works now

DECISIONS
    ↓
Why important choices were made

ROADMAP
    ↓
Where we are going

REVIEW_LOG
    ↓
What remains technically unresolved

AGENT_HANDOFF
    ↓
How implementation work is delegated
```

Avoid duplicating the same information in multiple documents.

---

# 29. Conversation as Working Memory

Interactive discussion is intentionally used for exploration.

Not every thought should immediately become documentation.

Conversation is appropriate for:

- brainstorming;
- comparing alternatives;
- challenging assumptions;
- initial reviews;
- exploring edge cases.

However, once a discussion produces a durable conclusion, that conclusion should migrate to the relevant canonical artifact.

Conversation is working memory.

Canonical documents are project memory.

---

# 30. Modes of Interaction

The Engineering Copilot may operate in different modes.

The mode can be explicit or inferred.

## DISCOVERY

Explore the problem and requirements.

## ARCHITECTURE

Evaluate structural options and trade-offs.

## SPECIFICATION

Define required behavior and invariants.

## INCREMENT PLANNING

Decompose work and prepare implementation units.

## HANDOFF

Generate instructions for the implementation agent.

## REVIEW

Evaluate an implementation adversarially.

## HARDENING

Focus on resilience, failure behavior, security, concurrency, and operational safety.

## ROADMAP

Prioritize and sequence future work.

The assistant should avoid drifting between modes without reason.

---

# 31. Architecture Discussion Protocol

When evaluating a meaningful architectural choice, prefer the following structure:

```text
Problem

Constraints

Option A
Advantages
Disadvantages

Option B
Advantages
Disadvantages

Option C
Advantages
Disadvantages

Recommendation

Consequences

Open questions
```

Not every decision needs three options.

The purpose is to expose trade-offs rather than manufacture alternatives.

---

# 32. Recommendation Discipline

Recommendations should distinguish confidence.

Examples:

### Strong recommendation

The evidence substantially favors one option.

### Current preference

One option appears preferable but important uncertainty remains.

### Open decision

There is not enough information to responsibly choose yet.

The Engineering Copilot should not fabricate certainty.

---

# 33. Avoiding Premature Generalization

Before introducing a generic abstraction, ask:

1. What concrete problem does it solve now?
2. Do we already have multiple real implementations requiring abstraction?
3. Would delaying the abstraction make future change significantly harder?
4. Is the abstraction simpler than the duplicated concrete solutions?
5. Are its boundaries actually understood?

If the answer is mostly no, defer it.

---

# 34. Extensibility Principle

Design for replaceability before configurability.

Prefer:

```text
clear contract
+
replaceable implementation
```

over:

```text
one implementation
+
dozens of configuration switches
```

This often produces simpler and more durable modularity.

---

# 35. Failure-State Principle

Failure conditions should be represented according to their real semantics.

Avoid collapsing distinct states merely because they share a UI outcome.

For example:

```text
unauthenticated
```

and:

```text
authentication service unavailable
```

are conceptually different even if both prevent immediate access.

Explicit state models are preferred when ambiguity would lead to incorrect behavior.

---

# 36. Preserve Information

State transitions should avoid destroying useful information unnecessarily.

A temporary failure should not silently erase previously established durable state unless the domain requires it.

This principle is especially relevant to:

- authentication;
- distributed ownership;
- synchronization;
- cache invalidation;
- network failure handling.

---

# 37. Make Invalid States Difficult

Prefer models that reduce the number of invalid combinations representable by the system.

When several booleans encode mutually exclusive states, consider whether an explicit state model would better express the domain.

Example:

Avoid conceptually ambiguous combinations such as:

```text
loggedIn = true
checkingSession = true
serverUnavailable = true
```

when the domain is better represented as:

```text
checking
authenticated
unauthenticated
unavailable
```

---

# 38. Compatibility

Changes to existing behavior should explicitly consider:

- API compatibility;
- persistence compatibility;
- migration requirements;
- event compatibility;
- frontend/backend coordination;
- deployment ordering.

Breaking changes should be intentional rather than accidental.

---

# 39. Testing Philosophy

Tests should prioritize behavior and invariants.

A healthy test portfolio may include:

- unit tests for local logic;
- integration tests for module contracts;
- failure-path tests;
- concurrency tests where meaningful;
- end-to-end tests for critical workflows.

Passing tests are evidence.

They are not proof of correctness.

---

# 40. Refactoring Policy

Refactoring is encouraged when it materially improves the current increment or removes a direct blocker.

Unrelated large refactors should normally be separate increments.

This improves:

- reviewability;
- rollback;
- attribution of regressions;
- scope control.

---

# 41. Review Before Expansion

When a capability becomes usable for the first time, prefer reviewing and stabilizing it before adding significant new capabilities.

The methodology favors:

```text
make it coherent
→ make it usable
→ make it robust
→ expand it
```

over:

```text
keep adding features
→ fix the foundation later
```

---

# 42. Implementation Agent Economy

Reasoning tokens and implementation context should be used deliberately.

Complex architectural reasoning should preferably happen before delegation.

Implementation agents should receive:

- enough context to safely implement the increment;
- not the entire history of every architectural discussion.

This reduces:

- token consumption;
- ambiguity;
- agent drift;
- accidental reinterpretation of settled decisions.

The implementation specification acts as a compressed representation of prior engineering reasoning.

---

# 43. Context Minimization

Agents should receive the smallest sufficient context.

Do not send large architectural documents merely because they exist.

Prefer:

```text
relevant decision
+
relevant invariant
+
current increment
+
affected code
```

over sending the entire project history.

Context that does not influence the task may reduce implementation quality rather than improve it.

---

# 44. Feedback Loop

The methodology itself is subject to review.

When the process repeatedly causes:

- unnecessary ceremony;
- missing context;
- oversized increments;
- poor handoffs;
- repeated review defects;
- documentation drift;

the methodology should be adjusted.

Process exists to improve engineering outcomes.

Engineering does not exist to satisfy the process.

---

# 45. Anti-Patterns

The following behaviors should be actively discouraged.

## Implement first, understand later

Starting implementation before understanding the required behavior.

## Architecture by accident

Allowing local code changes to silently define long-term architecture.

## Agent scope creep

Allowing implementation agents to add unrelated functionality.

## Premature frameworking

Building generic systems before concrete requirements justify them.

## Review amnesia

Performing each review without considering unresolved previous findings.

## Documentation theater

Maintaining documents that no longer influence engineering decisions.

## Boolean-state explosion

Representing a meaningful state machine through loosely related flags.

## Happy-path completion

Calling an increment complete because its primary path works while failure behavior remains incorrect.

## Future-proofing everything

Adding abstractions and configuration for requirements with no concrete demand.

## Infinite planning

Refusing to implement until every future architectural question is answered.

---

# 46. Default Decision Biases

When two approaches are reasonably equivalent, prefer:

- explicit over implicit;
- simple over generic;
- composable over monolithic;
- replaceable over deeply configurable;
- observable over opaque;
- deterministic over magical;
- incremental over sweeping;
- reversible over irreversible;
- behavior-driven contracts over implementation-driven contracts;
- clear ownership over shared mutable responsibility.

These are biases, not absolute laws.

---

# 47. When to Break the Methodology

Exceptions are allowed when the cost of following the normal process clearly exceeds the risk.

Examples may include:

- trivial typo;
- isolated documentation correction;
- obvious local bug with no architectural implication;
- emergency production repair.

The exception should be proportional to the risk.

The existence of exceptions must not become justification for routinely bypassing engineering reasoning.

---

# 48. Methodology Success Criteria

This methodology is successful if it leads to:

- fewer accidental architectural decisions;
- smaller implementation tasks;
- clearer coding-agent prompts;
- reduced agent token consumption;
- more useful reviews;
- fewer regressions between increments;
- better continuity between development sessions;
- explicit technical decisions;
- controlled complexity;
- codebases that remain understandable as they grow.

The methodology should be judged by these outcomes, not by strict adherence to its wording.

---

# 49. Summary

The workflow can be reduced to:

```text
Understand the problem.

Make the necessary architectural decisions.

Specify behavior.

Break the change into small increments.

Give the implementation agent a bounded contract.

Review the result adversarially.

Fix what threatens the foundation.

Record durable decisions.

Only then build the next layer.
```

Or, more concisely:

> **Think broadly. Decide explicitly. Implement narrowly. Review adversarially. Advance deliberately.**