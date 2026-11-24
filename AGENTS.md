# Working rules

AutoPCB follows these rules and, where they apply, the GRANDE agent instructions. This file wins where the two differ.

## Code

- Object-oriented only: every behavior lives in a class; functions are callable objects.
- Keep it lean. No bloat, filler, dead code, speculative options, or comments that restate code.
- No gates or validation that do not protect real data or block real progress.
- Modular: one job per class; separate pure logic from KiCad and file I/O.
- Format Python in Black's 88-column style with `ruff format`.
- Name each signal, measurement and operation directly, and give physical values their units (mm, nm, degrees, ohms).

## Documentation

- Every source file starts with a header docstring naming the file, its responsibility and `Copyright: GRANDE Robots and Aaron John Sabu`.
- Every class, function and method has a factual docstring. Add `Inputs:` and `Outputs:` when the signature does not say enough about values, units, state changes or failures.
- The root README uses these sections: Overview, Responsibilities, Requirements, Installation, Usage, Validation, File Structure, License.
- Every maintained folder has a README with a File Structure table of its immediate entries: File, Relevance, Dependencies, Used by.
- Update the guide a change affects, and keep a source document and its rendered PDF together.

## Checks

- Keep the code base compatible, complete and working; run the relevant tests before committing.
- Tests and CI stay, and grow with new behavior. The GRANDE rule against adding tests does not apply here.
- Report software checks, KiCad verification and hardware evidence separately. Do not call an unmeasured value known.

## Git

- Work on a branch, commit at each meaningful point, open a PR, merge it into `master`, delete the branch.
- Commits name Aaron John Sabu as author and add a co-author trailer for the assistant that made the change.

## GRANDE sections that do not apply

Robot operation and safety, ROS package structure, the provider boundary and the operator interface.
