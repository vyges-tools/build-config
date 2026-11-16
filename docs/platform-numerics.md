# Platform numerics: what can make the same code compute different numbers

Data points that decide which build settings in this repository are pinned, and what to record
alongside a reference capture. Each is dated; re-check when a compiler, libm or runner changes.
No action is implied by an entry beyond what `versions.toml` and the checker already enforce.

## 1. Floating-point contraction (checked 2026-11-16)

- **rustc never contracts** `a*b + c` into a fused multiply-add, on any target or optimisation
  level. A fused operation in Rust happens only through an explicit `mul_add`.
- **clang contracts by default wherever the target has FMA.** Compiler Explorer, clang 22.1.0,
  `-O2`, on `a*b + c`:

  | Target | Code without `-ffp-contract=off` | With `-ffp-contract=off` |
  | --- | --- | --- |
  | x86-64, no `-march` | multiply, then add (two roundings) | same |
  | x86-64 `-march=haswell` | one fused multiply-add | multiply, then add |
  | aarch64 Linux | one fused multiply-add | multiply, then add |
  | arm64 macOS | one fused multiply-add | multiply, then add |

  aarch64 has FMA in its baseline, so contraction happens there with no `-mcpu` flag at all. A
  fused result rounds once instead of twice and can differ in the last bit.
- **Consequence:** every C and C++ source we compile carries `-ffp-contract=off` (C and C++,
  target and host), and no `-march`/`-mcpu` or fast-math flag (`bazel/common.bazelrc`,
  `cmake/vyges-flags.cmake`, the `forbidden_flags` list).

## 2. Platform libms differ by about one ulp (measured 2026-11-16)

One million deterministic inputs per function, results compared bit for bit. Every difference
found was 1 ulp, except `tanh` (2 ulp on macOS). `sqrt` is correctly rounded everywhere and never
differed.

| Function (input range) | glibc x86-64 vs glibc aarch64 | glibc x86-64 vs Apple libm (arm64) |
| --- | --- | --- |
| `ln` on (0, 1) | 0 | 1,368 |
| `ln` on large values | 0 | 43 |
| `ln_1p` | 0 | 65,562 |
| `exp` (negative arguments) | 0 | 1,921 |
| `exp` (wide range) | 0 | 1,900 |
| `exp_m1` | 0 | 67,764 |
| `exp2` | 959 | 1,991 |
| `pow` | 0 | 1,527 |
| `log2` | 0 | 35 |
| `log10` | 0 | 8,092 |
| `hypot` | 130,500 | 160,191 |
| `cbrt` | 0 | 76,414 |
| `tanh` | 0 | 28,076 (max 2 ulp) |
| `sqrt` | 0 | 0 |
| `ln` (f32) | 0 | 173 |
| `exp` (f32) | 0 | 6,295 |

glibc builds the same source for both Linux targets; x86-64 has its own `exp2` and `hypot`. Apple's
libm is a different implementation. Where both were checked against the exact value, both were
faithful (within about half an ulp of it) but neither was correctly rounded on every input.

## 3. glibc on x86-64 picks its libm code path by CPU (measured 2026-11-16)

glibc selects FMA variants of `log`, `exp`, `pow` and others at load time from the CPU's features
(ifunc / hwcaps). The same binary on the same glibc therefore gives different results on a CPU
with FMA and one without. Masking FMA with
`GLIBC_TUNABLES=glibc.cpu.hwcaps=-AVX2,-FMA,-AVX512F` on an FMA-capable x86-64 host changed, of one
million inputs each:

| Function | Results changed |
| --- | --- |
| `ln` on (0, 1) | 114 |
| `ln` on large values | 7 |
| `ln_1p` | 197 |
| `exp` (negative arguments) | 737 |
| `exp` (wide range) | 670 |
| `exp_m1` | 515 |
| `pow` | 705 |
| `log2` | 10 |
| `log10` | 2 |
| `tanh` | 48 (max 3 ulp) |
| `exp2`, `hypot`, `cbrt`, `sqrt`, f32 `ln`/`exp` | 0 |

**Consequence:** a reference program that calls libm computes with the HOST's libm and CPU, not
only its own build. A reference capture is reproducible only on a host with the same libm and the
same CPU feature class. Record the fingerprint (below) with every capture.

## 4. A pure-Rust engine across three platforms (measured 2026-11-16)

Our static timing engine (pure Rust; its only libm calls are `ln` of library thresholds, results
narrowed to f32 delays and slews) was run on its full correlation corpus on x86-64 Linux, aarch64
Linux and arm64 macOS, rustc 1.100.0, same profile: all 50 detailed outputs (every arc delay, slew,
arrival, required time and slack as exact f32 bits) were **byte-identical** on all three, as were
its test suites. Perturbing every `ln` result by up to 256 ulps changed no output: the f32
narrowing absorbs the 1-ulp libm differences above. A computation that keeps f64 results, or
feeds a libm result into a comparison or a sort key, would not have that margin.

## Recording a host's libm: `scripts/libm-fingerprint.py`

```bash
python3 scripts/libm-fingerprint.py            # text
python3 scripts/libm-fingerprint.py --json     # one JSON line, to store with a capture
```

It prints the C library and its version, the CPU model and whether it has FMA/AVX2, any
`GLIBC_TUNABLES` setting, and an md5 per function (`log` on two ranges, `exp`, `pow`, `log2`,
`hypot`, `exp2`, `tanh`) over the C library's results on a fixed, deterministic input set (100,000
per function by default; `--n` changes it, and the same `--n` gives the same inputs everywhere).

- Store its `--json` output next to every oracle or reference capture.
- Before comparing a new capture with an old one, compare the two fingerprints. Equal md5s mean
  the libm gave the same results on these inputs; a different md5 means libm-reaching values may
  differ by an ulp and the comparison must allow for it, or the capture must be redone on a
  matching host.
- The functions are called through `ctypes` from the C library itself, not through Python's
  `math` module, which reimplements some of them.
