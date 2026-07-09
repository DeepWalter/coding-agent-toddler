# Git Commit Style Guide

This guide defines the standards for writing clean, structured, and predictable Git commit messages.

## Structural Layout

A complete commit message consists of a header, an optional body, and an optional footer.

```text
<type>(<optional scope>)[!]: <subject>
// a blank line here.
<optional body: detailed explanation of what and why>

<optional footer: breaking changes or issue tracker links>
```

## The 7 Core Rules

* **Limit the subject line** to 60 characters or less.
* **Start subject line** with a lowercase letter.
* **Do not end the subject line** with a period.
* **Use the imperative mood** in the subject (e.g., "add feature", not "added feature").
* **Separate the subject from the body** with a single blank line.
* **Wrap the body text** at 80 characters per line.
* **Explain the *what* and *why*** in the body, rather than the *how*.

## Common Commit Types

* **feat**: Introduces a brand new feature to the codebase.
* **fix**: Patches a bug or fixes an existing error.
* **docs**: Documentation-only changes (like updating a README).
* **style**: Code changes that do not affect functionality (whitespace, formatting, semicolons).
* **refactor**: Rewriting code without changing external behavior or fixing bugs.
* **perf**: Code adjustments focused explicitly on improving performance.
* **test**: Adding missing test files or correcting existing test suites.
* **chore**: Routine maintenance, updating build tools, or managing dependencies.
* **init**: Initial commit.

## Breaking Changes Rules

Breaking changes signal API deprecations, backward-incompatible code, or major version bumps. They must be explicitly highlighted using one or both of the methods below:

### 1. The Exclamation Mark (`!`)

* Place an `!` immediately after the commit type or scope to grab attention in a short log.
* **Example:** `feat(auth)!: remove legacy OAuth1 support`

### 2. The `BREAKING CHANGE:` Footer

* Begin the footer section with the exact uppercase string `BREAKING CHANGE:`.
* Follow it with a space and a detailed description of what broke and how to migrate.
* **Example:**
  ```text
  chore(api): drop support for Node 14

  BREAKING CHANGE: The runtime now requires Node 16 or higher.
  Applications running on older versions will fail to start.
  ```

## Examples

### Simple Feature Commit

```text
feat: add user authentication middleware
```

### Complex Bug Fix with Body and Footer

```text
fix(api): resolve memory leak in streaming pipeline

The previous implementation retained references to connection chunks
instead of freeing them after flushing. This caused memory usage to
grow unbounded under sustained load.

Closes #402
```

### Breaking Change

```text
refactor(database)!: store customer UUIDs as binary format

We are switching the `customer_id` columns from string VARCHAR(36) to
BINARY(16) to improve indexing performance and reduce disk storage footprint.

BREAKING CHANGE: Database schemas must be migrated using the script in
/scripts/migrate-v2.sql. Existing API payloads that pass raw string
UUIDs to database queries will now throw a type error.
```
