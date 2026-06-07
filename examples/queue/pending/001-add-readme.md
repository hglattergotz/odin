# Task: add a project README

Create a `README.md` at the project root with these sections:

- **What it is** — one paragraph
- **Install** — install/build instructions appropriate to the stack
- **Usage** — the simplest possible "run this and see something happen"
  example
- **Project status** — a single line indicating maturity (e.g.
  "experimental", "pre-1.0", "stable")

Pull the project name and stack from `package.json` / `pyproject.toml` /
`go.mod` / `Cargo.toml` as appropriate — do not invent.

If a README already exists, do not overwrite. Emit `<<<NEEDS_INPUT>>>`
asking whether to merge, replace, or skip.
