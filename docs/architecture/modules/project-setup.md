# Project Setup Module

## Goal

`project-setup` is an independently installable Agent Skill that converts a new or explicitly agreed bootstrap-only directory into a minimal, production-oriented project, establishes durable cross-Agent architecture governance for later work, verifies the cold-start result, reports evidence, and stops.

## Responsibilities

- Resolve and classify the exact target before mutation.
- Preserve existing content and prevent silent force/clean/overwrite behavior.
- Resolve initialization-specific choices such as project profile, runtime, package manager, public entrypoint, distribution/deployment shape, and real external boundaries.
- Use an official/ecosystem generator when appropriate or create the smallest manual scaffold.
- Establish a runnable public path plus architecture, Docs as Code, configuration, error, observability, security, test, build, and reproducibility baselines appropriate to the selected project.
- Generate a short root `AGENTS.md` architecture-impact router and a preserving `@AGENTS.md` import for Claude Code, while keeping detailed maintenance behavior in a repository-local on-demand policy.
- Keep current architecture facts and semantic change history independent of a replaceable diagram provider; prefer Archify when available and use an explicit fallback otherwise.
- Establish one managed README block that directly shows the latest validated system architecture diagram without overwriting unrelated README content.
- Verify applicable quality gates, public success/failure paths, and a candidate-only clean-environment bootstrap.
- Report evidence and end the initialization task.

## Boundaries and Non-responsibilities

- The module does not continue into product feature development beyond the minimal behavior required to prove the scaffold.
- It does not provide a general planning, approval, branching, feature-delivery, or independent-review lifecycle.
- It does not discover, load, invoke, require, or provide a fallback for another workflow skill.
- It does not perform later feature/refactor architecture maintenance itself; generated repository guidance owns that behavior without re-invoking this skill.
- It does not make Archify or another renderer the source of architecture truth, silently install a provider, or require one provider to complete unrelated initialization when a truthful fallback is available.
- It does not silently create remote repositories, hosted CI bindings, cloud resources, external accounts, production data, paid services, or global tool installations.
- It does not ship a framework-specific universal application template.

## Public Interface

| Interface | Role |
|---|---|
| `project-setup/SKILL.md` | Discovery metadata, target preflight, initialization router, safety boundary, verification, and stop condition |
| `project-setup/agents/openai.yaml` | Optional Codex UI presentation and default `$project-setup` invocation |
| `references/production-baseline.md` | Common production baseline loaded before designing the target |
| `references/project-profiles.md` | Only the section matching the selected project type |
| `references/architecture-governance.md` | Persistent-rule merge behavior, cross-Agent routing, provider manifest, history, and README initialization contract |
| `references/verification.md` | Applicable test, quality, public-path, clean-environment, and evidence rules |
| `assets/architecture-maintenance.md` | Provider-neutral on-demand policy adapted into the initialized repository |

Inputs are the user request, exact target state, applicable parent-repository instructions, available diagram-provider capability, and current primary documentation needed for unstable tool/version decisions. Outputs are scoped project files, durable architecture-governance artifacts, and a concise evidence report. No package-specific service or renderer is required.

## Dependency and Data Flow

```mermaid
flowchart LR
    A["Initialization request"] --> B["SKILL.md target preflight"]
    B --> C["Production baseline"]
    B --> D["Selected project profile"]
    B --> K["Architecture-governance reference"]
    C --> E["Minimal scaffold and public path"]
    D --> E
    K --> P["AGENTS router + Claude import + on-demand policy"]
    P --> R["Provider-neutral facts/history + replaceable renderer"]
    E --> F["Verification contract"]
    R --> F
    F --> G["Initialized project with current README diagram"]
    G --> H["Evidence report and stop"]
```

Dependencies stay inside the package: the entrypoint routes to its references and generated-policy asset, while generated project code never depends on skill tests or repository-root documentation. Diagram providers are capability-selected adapters behind a generated manifest; Archify-specific sources and receipts stay isolated from provider-neutral facts, history, Agent routing, and stable README outputs. External providers, generators, and package managers remain subject to authorization and current official guidance.

## Validation

- Package contracts verify discovery metadata, required files, local links, placeholder removal, progressive disclosure, cross-Agent routing, provider replaceability, README/history synchronization, and absence of workflow-skill coupling.
- Repository contracts copy the distributable skill into an isolated directory, run its package tests there, verify root inventory text, and require this module document/index.
- The skill-creator validator checks package structure and frontmatter.
- Independent forward tests exercise target protection, project generation, lock semantics, public paths, aggregate QA, clean-environment reproducibility, authorization boundaries, and the explicit stop condition.
