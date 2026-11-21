# Working rules

- Object-oriented only: every behavior lives in a class; functions are callable objects.
- Keep it lean. No bloat, filler, dead code, speculative options, or comments that restate code.
- No gates or validation that do not protect real data or block real progress.
- Modular: one job per class; separate pure logic from KiCad and file I/O.
- Keep the code base compatible, complete and working; run the relevant tests before committing.
- Git: work on a branch, commit at each meaningful point, open a PR, merge it into `master`, delete the branch.
