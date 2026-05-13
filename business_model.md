# Business Model Notes: LLM-Compiled Cache Pattern

## The Core Observation

Online LLM providers (OpenAI, Anthropic) profit from **per-token billing**.
Their ideal world = you call them every time for every page.
The LLM-compiled cache pattern deliberately subverts that.

## What We Built: An AOT Compiler

- **First visit** = LLM "compiles" a fast, deterministic parser → saved to disk
- **Every visit after** = run the compiled artifact, zero LLM cost
- The LLM is only the *compiler*, not the *runtime*

This is why **local LLMs make this pattern shine even harder** — the "compilation"
cost is also near-zero, so the whole stack costs nothing at scale.

## The Business Tension

| Approach              | LLM API calls      | Provider revenue |
|-----------------------|--------------------|------------------|
| Ask LLM every time    | 1 per page visit   | 💰 happy         |
| LLM-compiled cache    | 1 per domain (ever)| 😬 not ideal     |

It is similar to why cloud vendors preferred *functions-as-a-service* over
*containers* — more granular billing. The pattern here is essentially
**"compile the cloud out of the hot path."**

## Why This Is the Right Trade-off for overhired

- Users get sub-second re-extractions after the first visit
- LLM dependency shrinks over time as the parser/filler library grows
- The more sites covered, the less LLM is needed at all
- With a local LLM (Ollama), the marginal cost per new domain is also zero

## Analogy

| Software world       | overhired equivalent            |
|----------------------|---------------------------------|
| AOT compilation      | LLM generates parser once       |
| Compiled binary      | Cached `.py` / `.js` file       |
| Runtime execution    | Direct script execution (O(1))  |
| Recompile on failure | Self-healing: delete + regenerate |

## Broader Implication

The same pattern can be applied anywhere the logic is:
1. Hard to write by hand (edge cases, site-specific DOM, etc.)
2. Structurally stable once written
3. Triggered repeatedly on similar inputs

**Examples already in overhired:**
- `extractor.py` — job title/company parser per domain
- `ats_filler.py` — ATS form filler per domain

**Potential extensions:**
- Resume section parser (per resume format/layout)
- Salary normaliser (per region/currency convention)
- Interview question classifier (per company style)
