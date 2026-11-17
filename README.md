# build-config

The single source of truth for how Vyges tools are built: toolchain versions, compiler flags,
the Cargo release profile and the CI runner OS versions. Every native library and Rust tool
repository either consumes these files or is checked against them in CI.

## Contents

| File | What it holds |
| --- | --- |
| `versions.toml` | Rust toolchain, C++ standard, optimisation level, forbidden C/C++ flags, Bazel and bazelisk versions, macOS minimum, CI runner versions and the organisation variables that carry them, allowed CI profile overrides, allocator pins. |
| `bazel/common.bazelrc` | The `.bazelrc` lines every Bazel-built library shares, each with its reason. Library-specific lines stay in the library. |
| `cmake/vyges-flags.cmake` | The same settings for a CMake-built library; include it before `project()`. |
| `cargo/profile-release.toml` | The canonical `[profile.release]` table. |
| `cargo/rust-toolchain.toml` | The `rust-toolchain.toml` every Rust repository carries. |
| `scripts/check.py` | Checks a repository against all of the above (read-only). |
| `scripts/sync-org-vars.sh` | Shows (dry run) or applies the `gh variable set` commands that keep the organisation variables equal to `versions.toml`. |
| `scripts/libm-fingerprint.py` | Records the host's C library, CPU FMA/AVX2 and an md5 of its libm results on fixed inputs, to store with any reference capture. |
| `docs/platform-numerics.md` | Dated measurements behind the pinned flags: contraction by target, libm differences between platforms and CPUs. |
| `tests/` | Checker tests: a conforming fixture must pass and every deliberately wrong variant must fail. |

## Why the flags matter

Most build settings only trade build time for run time. A few decide the numbers a program
computes, and those are pinned here and checked everywhere:

- **No floating-point contraction in C/C++** (`-ffp-contract=off`, on C and C++ sources, target
  and host). A contracted `a*b + c` is rounded once instead of twice. Clang fuses it by default
  wherever the target has a fused multiply-add instruction: aarch64 (Linux and macOS) has one in
  its baseline, and x86-64 has one under an FMA `-march` such as `haswell`. Checked 2026-11-16:
  x86-64 with no `-march` emits a multiply and an add; aarch64 Linux, arm64 macOS and x86-64
  `-march=haswell` emit one fused instruction; `-ffp-contract=off` restores two roundings on all
  of them. rustc never contracts, so this flag is what keeps C/C++ results equal across targets
  and equal to Rust code computing the same expression.
- **No `-march`/`-mcpu`, no fast-math family** (`-ffast-math`, `-Ofast`, `-funsafe-math-optimizations`,
  `-fassociative-math`, `-freciprocal-math`, `-ffp-model=fast`) in any C/C++ we compile. One
  exception, `versions.toml` `allowed_flags`: `-march=x86-64`, the x86-64 baseline ISA (no FMA),
  which cc-rs passes by default on Linux x86_64. Any other `-march`, including `x86-64-v3`, is refused.
- **One C++ standard** for a library and everything built from source inside it: abseil's ABI
  depends on it.

The Cargo release profile (`lto = "fat"`, `codegen-units = 1`) cannot change a computed value —
rustc does not reassociate or contract floating-point operations at any level — and was adopted
with before-and-after output comparisons. Packages that ship release binaries (listed in
`versions.toml` `[cargo] shipped`) also strip symbols; every other package keeps them.

Measurements behind these rules, and what else can move a result (platform libms, glibc's
CPU-dependent code paths): [docs/platform-numerics.md](docs/platform-numerics.md).

## Checking a repository

```bash
python3 scripts/check.py path/to/repo [more/repos ...]     # text report, exit 1 on any drift
python3 scripts/check.py --json path/to/repo               # one JSON object per repository
python3 scripts/check.py --self                            # templates agree with versions.toml
python3 -m unittest discover -s tests -v                   # checker tests
```

Python 3.11 or newer (for `tomllib`); nothing else. Exit status: 0 conforms, 1 drift, 2 bad
arguments. Each finding names the file, the line where there is one, and the expected value.

What is checked, wherever the repository has it: `rust-toolchain.toml`; `[profile.release]`
(exactly the canonical keys, and present in any package that builds a binary); allocator pins;
no tracked `.cargo/config.toml` with `rustflags`; `.bazelrc` (imports followed) carrying every
common line, `.bazelversion`, no forbidden flag in Bazel files; `CMakeLists.txt` including
`vyges-flags.cmake` or setting the same values; C++ compiled from `build.rs`; and workflows
(no hard-coded runner OS version, fallbacks equal to `versions.toml`, pinned Rust toolchain and
bazelisk, allowed CI profile overrides only).

## Running the check in CI

The `build-config-check` composite action in `vyges-tools/actions` downloads a tagged release of
this repository and runs the checker on the calling repository:

```yaml
jobs:
  build-config:
    runs-on: ubuntu-${{ vars.VYGES_UBUNTU_VERSION || '24.04' }}
    steps:
      - uses: actions/checkout@v7
      - uses: vyges-tools/actions/build-config-check@v1.1.0
```

Both the action and the build-config release it uses are pinned, so a change here reaches a
repository only when that repository moves its pin.

## How a repository consumes this

- **Bazel**: a synced copy of `bazel/common.bazelrc` imported from the library's `.bazelrc`
  (`import %workspace%/build-config/common.bazelrc`), or the lines inline; CI runs the checker.
- **CMake**: a synced copy of `cmake/vyges-flags.cmake` included before `project()`, or the
  settings inline; CI runs the checker.
- **Cargo**: Cargo cannot import a profile and reads profiles only from the root of the build, so
  the table is carried verbatim and CI runs the checker.
- **CI runners**: workflows name the OS through organisation variables with the
  `versions.toml` value as fallback:

  ```yaml
  runs-on: ubuntu-${{ vars.VYGES_UBUNTU_VERSION || '24.04' }}
  runs-on: macos-${{ vars.VYGES_MACOS_VERSION || '15' }}
  ```

  Changing a runner version is: edit `versions.toml`, run `scripts/sync-org-vars.sh --apply`,
  then update the fallbacks (the checker lists every one that disagrees).

## Exemptions

`versions.toml` lists, under `[[exemption]]`, the repositories deliberately left out, each with
its reason and date. Remove an entry when its repository is brought in line.

## Changing a value

1. Edit `versions.toml` and the template that carries the value.
2. `python3 scripts/check.py --self` and the tests must pass.
3. Roll the change into consuming repositories one at a time, each with its outputs compared
   byte for byte before and after.

## License

Apache-2.0. See [LICENSE](LICENSE).
