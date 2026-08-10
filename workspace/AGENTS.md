# Working with this 1C configuration

Guidance for AI agents working on the 1C:Enterprise 8.3 configuration in this
workspace.

> Rewritten to match the servers that actually exist. The previous version
> described `sonar_*`, `naparnik_*`, `templates_*`, `code_search` and
> `rest_proxy` — none of which are part of this stack anymore. An agent
> reading that file would plan work around tools that never answer, then
> improvise. Stale documentation is worse than none.
>
> The authoritative, detailed rules live in `.cursor/rules/mcp-tools.mdc`
> and `.cursor/rules/bsl-code.mdc`. This file is the short version, for
> agents that do not read Cursor rules.

## Available MCP servers

Five servers, 36 tools. Full list: `python scripts/list_tools.py`.

### Configuration structure and call graph (`metadata`, port 8001)

- `metadata_search(query)` — find objects by name or purpose
- `metadata_list_objects(kind)` — list catalogs, documents, registers
- `metadata_object_attributes(full_name)` — attributes and tabular sections
- `metadata_referrers(full_name)` / `metadata_find_link_path(a, b)` — links
- `metadata_subsystem_tree()` — subsystem hierarchy
- `code_callers(name)` / `code_callees(name)` — who calls what
- `code_method_signature(name)` — parameters of a procedure or function
- `code_call_path(a, b)` — call chain between two methods

Answers about **this** configuration. Never guess object names — look them up.

### Static analysis (`bsl`, port 8002)

- `bsl_check_code(code)` — analyze a snippet
- `bsl_check_file(file_path)` / `bsl_check_directory(dir_path)`

Every diagnostic carries `std_ref` (`bslls:UsingModalWindows`), and the
response has a `std_lookup` section with the unique codes. Feed those codes
to `v8std_explain_diagnostics` in a single call to learn which standard was
violated and why. Do not paraphrase a diagnostic from memory.

An `error` field means the analysis did not run. That is not the same as
"no issues found" — never report it as clean.

### Platform reference (`help`, port 8003)

- `platform_help_search(query)` — semantic search over platform help
- `platform_help_lookup(name)` — exact lookup by method or object name
- `platform_help_details` / `platform_help_kinds` / `platform_help_stats`

Answers **how the platform works**.

### Query builder (`query`, port 8009)

- `query_build` / `query_validate` / `query_optimize` / `query_fields` /
  `query_join_hint`

### Development standards (`v8std`, port 8765)

- `v8std_get_page(id)` — full text of a standard, e.g. `std783`.
  **Always pass `body_limit: 30000`** — the default of 12000 truncates
  larger standards mid-way.
- `v8std_search(query)` — find a standard by phrase or number
- `v8std_explain_diagnostics(codes)` — analyzer codes to standards
- `v8std_get_related(id)` — move between a standard and its diagnostics
- `v8std_explain_snippet(snippet)` — applicable standards for a fragment.
  Local server only; never send configuration code to a public endpoint.

Answers **how code is supposed to be written**. Blocks titled
"В стандарте не указано" are the v8std author's commentary, not a vendor
requirement — keep that distinction when reviewing someone's code.

## Three sources, three different questions

| Server | Answers |
|---|---|
| `metadata` | what exists in **this** configuration |
| `help` | how the platform **works** |
| `v8std` | how code **should** be written |

They are not interchangeable. "The platform reference says nothing about it"
does not mean there is no requirement, and the reverse is equally false.

## Typical workflow

1. **Understand the task** — clarify which objects and modules are involved
2. **Find them** — `metadata_search`, `metadata_object_attributes`
3. **Check the blast radius** — `code_callers` before changing a signature
4. **Check the API** — `platform_help_search` for how a method behaves
5. **Check the requirements** — `v8std_search` or `v8std_get_page` for what
   the standards demand, before writing the code rather than after
6. **Write the code**
7. **Validate** — `bsl_check_code`, then `v8std_explain_diagnostics` on the
   codes from `std_lookup`
8. **Fix and re-check**

## When a tool is unavailable

Say so. Do not fall back on memory for standards, platform behaviour, or
object names in this configuration: a plausible answer that cannot be
verified is worse than an honest "the server is down".

`Выполнить()` and `Вычислить()` are invisible to the call graph — after
them, any conclusion `code_callers` draws about who calls what is
incomplete.
