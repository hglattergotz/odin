# Task: add an MIT LICENSE file

Add a standard MIT `LICENSE` file at the project root.

- Year: current year
- Copyright holder: read from `package.json`/`pyproject.toml` author field
  if present; otherwise emit `<<<NEEDS_INPUT>>>` asking for the name.

If a LICENSE file already exists, do not modify. Emit `<<<NEEDS_INPUT>>>`
asking what to do.
