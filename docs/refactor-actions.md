# Refactor selected code

Enable selected-region refactoring in `initializationOptions`:

```json
{
  "refactor_actions": {
    "model": "model1",
    "parameters": {
      "max_context": 8192,
      "max_tokens": 2048,
      "temperature": 0
    }
  }
}
```

`model` names an existing instruction model in `models`. This feature is optional
and independent of `diagnostic_actions`, completion, and legacy configured actions.
It constructs its own extraction prompt, so no messages, FIM template or completion
post-processing are needed.

When the editor requests code actions for a non-whitespace selection containing
more than one character, LSP-AI offers **Refactor: Extract function**, with kind
`refactor.extract.function`. No diagnostics are required and listing actions does
not call the model. A one-character range is excluded because Helix represents its
normal-mode cursor that way. LSP supplies one range; Helix uses the primary selection
if there are multiple selections.

Executing the action sends the selection's exact text, UTF-16 range, language,
document URI and nearby code to the configured model. Context always includes the
whole selection. The character budget is approximately `max_context * 4`, bounded
to 1,024–262,144 characters. Selections exceeding the budget are rejected instead
of silently truncated. Distant declarations and other files are not included.

The prompt asks for a named function in the same file and a call replacing the
selected code, preserving types, arguments, returned values, mutations, scope,
evaluation order, async/await and control flow. The response uses exact, unique
search/replacement snippets; multiple non-overlapping edits let it add a helper
and replace the call site in one operation. Missing, ambiguous, overlapping,
unchanged, empty or out-of-context replacements are rejected. A response must
replace the selected code, rather than only add a helper elsewhere.

The action returns a versioned `WorkspaceEdit` containing only the current file.
Changing, closing/reopening or renaming the document after listing, including
changes during generation, invalidates the action with `ContentModified`. The
editor applies the edit with its normal undo support. These structural checks do
not prove semantic equivalence: review the result and let your compiler/tests
check the generated code.

## Follow selected instruction comments

Start the selection with `//` or `--` comments describing the desired change.
Consecutive leading comment lines are combined, in order, into one instruction.
Empty comment lines inside the block are allowed. The first selected line must
be a comment; leading blank lines and empty instruction blocks are excluded.

If code follows the comments, the action is **Refactor: Follow instruction**,
with kind `refactor.rewrite.instruction`. Select the comments and target code:

```typescript
// rewrite use arrow function
// keep the existing types
function add(a: number, b: number): number {
  return a + b;
}
```

```haskell
-- rewrite use list comprehension
-- keep the function name
doubleEvens xs = map (* 2) (filter even xs)
```

If the selection contains only instruction comments (plus optional trailing
whitespace), the action is **Implement: Follow instruction**, with kind
`source.generate.instruction`. It inserts generated code immediately below the
comments, using nearby code as context. For example, with `visitors` in scope:

```typescript
// implement sort by visitor.status
// sort ascending, modify visitors in place
```

Comment-only selections must include complete comment lines. A selection ending
at the end of a final comment without its newline is also supported. The server
adds a newline before generated code if needed, preserving LF/CRLF conventions.
The instruction comments remain unchanged in both modes. Only selected target
code or trailing selected whitespace is replaced; surrounding code is read-only.
Include signatures/imports in the selected target if they need to change.

Both actions use the existing `refactor_actions` model and parameters. No extra
configuration is needed. The model receives the combined instruction, mode,
exact target/range, full selection and nearby context. It returns one JSON
replacement, applied as a single versioned edit at that range. Identical code
elsewhere is unaffected. An empty replacement may delete code in rewrite mode;
implementation mode requires nonempty generated code. Malformed, unchanged,
unsupported and stale responses are rejected.

Select comments with or without code, press `Space a`, then choose the relevant
**Follow instruction** action. `u` undoes the edit. After rebuilding LSP-AI, use
`:lsp-restart` to load the new binary.

## Helix

```toml
[language-server.lsp-ai.config.refactor_actions]
model = "model1"

[language-server.lsp-ai.config.refactor_actions.parameters]
max_context = 8192
max_tokens = 2048
temperature = 0.0
```

Keep `code-action` enabled for LSP-AI alongside the normal language server. Select
an expression or statements, press `Space a`, and choose **Refactor: Extract function**.
Use `u` to undo the edit. After changing the configuration, run `:config-reload`
and `:lsp-restart`.

## Offline validation

```sh
cargo test -p lsp-ai --bin lsp-ai actions --no-default-features --features rayon --locked
cargo build -p lsp-ai --no-default-features --features rayon --locked
python3 crates/lsp-ai/tests/refactor_actions_protocol.py target/debug/lsp-ai
python3 crates/lsp-ai/tests/refactor_actions_protocol.py target/debug/lsp-ai --with-diagnostics
```

The protocol test uses a local mock model without an API key. It exercises exact
selection payloads, action-kind filters, multiple versioned edits, independent
refactor configuration, coexistence with Explain/Fix, incremental changes,
stale/in-flight rejection, close/reopen and rename.

For instruction-based rewriting, run the additional mock-model protocol test:

```sh
python3 crates/lsp-ai/tests/instruction_actions_protocol.py target/release/lsp-ai
```
