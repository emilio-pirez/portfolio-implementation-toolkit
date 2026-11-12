# Interview notes

Short answers to the questions this project is designed to support. Updated
at the end of each phase.

## Phase 1 - Skeleton

**Why keep every limit in `config.yaml` instead of in code?**
Limits are business decisions, not implementation details. Keeping them in one
validated, version-controlled file means a risk or compliance reviewer can see
exactly what is enforced, every change leaves a Git history entry, and a typo
fails loudly instead of silently disabling a check.

**Why HARD vs. SOFT severity?**
Not every breach should stop trading. A position above the issuer limit or a
name on the restricted list must block the basket; a tracking error slightly
above target should alert the PM, who can approve it with a documented reason.
