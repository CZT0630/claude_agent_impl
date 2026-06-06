---
name: code-review
description: Review code for bugs, style issues, and improvements.
---

# Code Review Skill

When reviewing code, follow this checklist:

## 1. Correctness
- Does the code do what it claims?
- Are there off-by-one errors, null dereferences, or race conditions?
- Are edge cases handled (empty input, max values, errors)?

## 2. Security
- Is user input validated/sanitized?
- Are there path traversal, injection, or credential leak risks?
- Are permissions/capabilities correctly scoped?

## 3. Readability
- Are names descriptive and consistent?
- Is the function length reasonable (<50 lines preferred)?
- Are comments explaining "why", not "what"?

## 4. Performance
- Are there O(n²) loops that could be O(n)?
- Is I/O done efficiently (batched, buffered)?
- Are there unnecessary allocations or copies?

## Output Format

```
[severity] file:line — description
  Suggestion: how to fix
```

Severity: `critical` > `warning` > `info`
