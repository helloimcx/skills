---
name: code-duplication-scanner
description: Detect code duplication between git-modified files and existing codebase. Trigger when user says "/refactor", "/check-duplicates", or requests to check if new code duplicates existing code.
---

# Code Duplication Scanner (Git-Aware)

Check if newly modified or added code duplicates existing code in the repository.

## Supported Languages

Java, JavaScript/TypeScript, Python, Go, C/C++, C#, Ruby

## Trigger Commands

- `/refactor` - Check git modified files for duplicates
- `/check-duplicates` - Same as above

## Workflow

### Step 1: Get Git Modified Files

Get list of modified/added files from git:
```bash
git diff --name-only HEAD
git status --porcelain
```

Focus on:
- Modified files (M)
- Added files (A)
- Renamed files (R)

### Step 2: Extract New Code Blocks

For each modified file:
1. Extract changed code blocks (using git diff)
2. Normalize code (remove comments, normalize variables)

### Step 3: Compare with Existing Codebase

Compare new code blocks against:
- All other files in the codebase
- Focus on finding semantic similarities

### Step 4: Report Duplicates

For each duplicate found:
- Show new code location
- Show existing code location
- Provide specific refactoring suggestion

## Refactoring Patterns

See [references/refactoring_patterns.md](references/refactoring_patterns.md) for detailed patterns.

Common suggestions:
- **Extract Method**: New code duplicates method in another file
- **Use Existing Method**: Call existing method instead of duplicating
- **Extract Utility**: Create shared utility for cross-file duplication
- **Consolidate Logic**: Merge similar implementations

## Output Format

```
=== Duplicate Detection Report ===

Modified files checked: 3
New code blocks analyzed: 5
Duplicates found: 2

[DUPLICATE 1] Similarity: 92%
  New code: src/main/java/NewService.java:45-62
  Existing: src/main/java/OldService.java:78-95

  Suggestion: Extract common logic to BaseService class

  New code preview:
    public void processData() {
      validateInput();
      transformData();
      saveToDb();
    }

[DUPLICATE 2] ...
```

## Script Usage

```bash
# Check git modified files for duplicates
python scripts/check_git_duplicates.py

# Check with custom similarity threshold
python scripts/check_git_duplicates.py --similarity 85

# Check staged files only
python scripts/check_git_duplicates.py --staged

# Output JSON
python scripts/check_git_duplicates.py --format json
```

## Detection Logic

1. Get git status to find modified files
2. Extract changed hunks from diff
3. Normalize each hunk (remove comments, normalize identifiers)
4. Compare against all existing code blocks
5. Rank by similarity and frequency
