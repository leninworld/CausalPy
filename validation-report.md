# CausalPy #344 — documentation validation

Tested on November 12, 2026 in local ARM64 Docker, using the same pinned 201-package environment for the baseline and candidate.

- Baseline: `d1581744fa0bb167321024c4620c8780aa2aa5e3`.
- Tested documentation patch SHA256: `88cf1976b8baae5e2381f2b038076c86d4722efd9ab8ad3f3a6b51d5efee854f`.
- Scope: eight Markdown source cells in the IPW and quasi-DAG notebooks. Notebook code, outputs and metadata are unchanged.
- Both baseline and candidate: environment consistency and editable-path checks, imports, notebook schema/conventions, both affected upstream mock-notebook runs, the entire gallery/Sphinx HTML build, and all-files prek passed.
- Both full documentation builds reported the same 34 existing Sphinx warnings. Retained diagnostic comparison found no new candidate warnings; a non-warning MyST configuration banner differed only in set ordering.
- The rendered candidate contains the three-bullet estimand note, clipping explanation outside the note, identification assumptions and working cross-references.
- Total paired execution including cleanup: 350.16 seconds. Peak container memory: 1.74 GiB; no OOM events. Both source snapshots were restored and checked against all 339 expected file hashes.

The notebook execution used the repository's mock-sampling route. This verifies execution and documentation integration, not native posterior accuracy or a new statistical-estimator benchmark.
