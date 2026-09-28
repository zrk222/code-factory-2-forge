# ForgeLine 0.10.8

This compatibility release publishes the QA parser corrections merged to
`main` after the `v0.10.7` package tag. It improves public-surface coverage,
uses parser-backed JavaScript and TypeScript syntax validation, and prevents
quoted security fixtures from being reported as executable vulnerabilities.
Unsupported TSX remains an explicit incomplete result unless a TypeScript
compiler is available.

Code Factory 0.47.0 requires ForgeLine 0.10.8 or newer and checks real TSX QA
behavior during `factory doctor --strict`; installations that still resolve to
the earlier 0.10.7 artifact are reported as incompatible.
