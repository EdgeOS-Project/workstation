# Repository guidance

- Write code, comments, documentation, commit messages, and user-facing source
  strings in English.
- Keep x86_64 and ARM64 lifecycle behavior unified in shared code. Add separate
  implementations only where host or guest architecture requirements make
  sharing impractical.
- Do not commit credentials, local machine paths, VM state, disk images, build
  products, or extracted distribution files.
- Keep the Workstation, kernel, and distribution boundaries explicit. Use the
  path helpers in `tools/vmm/paths.py` instead of assuming a monorepo layout.
- Run the documented Python compilation and regression checks before changes
  are committed.
