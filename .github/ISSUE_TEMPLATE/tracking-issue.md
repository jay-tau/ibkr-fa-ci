---
name: Tracking issue
about: A parent issue grouping atomic-unit sub-issues by area (e.g., Complete Schedule FA Table A3).
title: ''
labels: []
assignees: []
---

<!--
  Use this template ONLY for parent tracking issues that group related sub-issues.
  - Add an `area:*` label.
  - Each child sub-issue is created via the atomic-unit template and linked
    via GitHub's native Sub-issues feature.
-->

## Goal

<!-- One paragraph describing what this tracking issue ships when complete. -->

## Why this matters

<!-- Concrete user-facing scenario this unlocks for filing Schedule FA / etc. -->

## Sub-issues

<!--
  List child sub-issues here. GitHub renders them as a progress checklist
  once they are linked via the Sub-issues panel (Issue → "Create sub-issue"
  or `gh issue edit <child> --add-parent <this>`).

  Example layout:

  - [ ] #NN  feat(compute): peak value computation per lot
  - [ ] #NN  feat(parser): trades section parser
-->

## Out of scope

<!-- Anything explicitly NOT covered here, with a pointer to where it lives. -->

## Acceptance

When all sub-issues close, the area is considered complete and the
`area:*-complete` label may be applied to a release.
