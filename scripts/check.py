#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check that a repository builds with the shared settings in this build-config repository.

Usage:
  check.py [--config DIR] [--json] REPO [REPO ...]
  check.py [--config DIR] --self

  REPO          a checkout to check (read-only: nothing is written to it).
  --self        check this repository's own templates against versions.toml, and its workflows.
  --config DIR  the build-config checkout holding versions.toml (default: this script's repo).
  --json        print one JSON object per repository instead of text.

Checks, each applied only where the repository has the thing checked:
  toolchain   rust-toolchain.toml exists beside Cargo.toml and equals cargo/rust-toolchain.toml.
  profile     [profile.release] equals cargo/profile-release.toml exactly (no missing, extra or
              different keys; a package listed in versions.toml [cargo] shipped may also carry
              [cargo.shipped_profile_extra]); required when the package builds a binary. Other
              profiles only as versions.toml [cargo.allowed_profiles] writes them.
  mimalloc    mimalloc / libmimalloc-sys pinned to versions.toml, optional, default features off;
              the Linux target table adds libmimalloc-sys with `override`.
  cargo-cfg   no tracked .cargo/config.toml carrying rustflags.
  bazel       .bazelrc (with its imports) carries every option of bazel/common.bazelrc;
              .bazelversion equals versions.toml; C++ standard agrees; no forbidden flag in
              .bazelrc, BUILD.bazel, MODULE.bazel or .bzl files.
  cmake       top-level CMakeLists.txt includes cmake/vyges-flags.cmake (an identical copy)
              before project(), or sets the same values inline (CMAKE_OSX_DEPLOYMENT_TARGET only
              when versions.toml [macos] cmake_target_required; if set it must agree); no
              forbidden flag.
  cc-shim     build.rs compiling C++ (cxx_build / cc::Build) sets the C++ standard and
              -ffp-contract=off; a cmake::Config build asks for the Release profile.
  workflows   no hard-coded runner OS version (ubuntu-24.04, macos-latest, ...) outside the
              organisation-variable expression, whose fallback equals versions.toml; Rust
              toolchain steps and bazelisk downloads name the versions.toml versions; a CI
              profile override uses exactly versions.toml [ci.test_profile_override].
  dist        cargo-dist github-custom-runners labels (literals: dist builds its matrix at run
              time) name the versions.toml runner versions.

Exit status: 0 every check passed, 1 a check failed, 2 bad arguments or unreadable config.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent
MIMALLOC_CRATES = ("mimalloc", "libmimalloc-sys")
DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


class Config:
    """The canonical values, read from a build-config checkout."""

    def __init__(self, root: Path):
        self.root = root
        self.versions = tomllib.loads((root / "versions.toml").read_text())
        self.toolchain = tomllib.loads((root / "cargo/rust-toolchain.toml").read_text())
        self.profile_doc = tomllib.loads((root / "cargo/profile-release.toml").read_text())
        self.bazelrc_text = (root / "bazel/common.bazelrc").read_text()
        self.cmake_text = (root / "cmake/vyges-flags.cmake").read_text()
        v = self.versions
        self.std = v["cxx"]["standard"]                       # "c++20"
        self.std_num = re.sub(r"\D", "", self.std)            # "20"
        self.forbidden = list(v["cxx"]["forbidden_flags"])
        self.rust = v["rust"]["toolchain"]
        self.ci = v["ci"]
        self.mim = v["mimalloc"]
        self.bazel_version = v["bazel"]["version"]
        self.bazelisk = v["bazel"]["bazelisk"]
        self.macos_min = v["macos"]["minimum"]
        self.profile = self.profile_doc.get("profile", {}).get("release", {})
        self.shipped = set(v.get("cargo", {}).get("shipped", []))
        self.shipped_extra = v.get("cargo", {}).get("shipped_profile_extra", {})
        self.allowed_profiles = v.get("cargo", {}).get("allowed_profiles", {})
        self.cmake_osx_required = v["macos"].get("cmake_target_required", True)
        self.bazel_pairs = bazelrc_pairs(self.bazelrc_text)


class Report:
    def __init__(self, repo: Path):
        self.repo = repo
        self.fails: list[tuple[str, str]] = []
        self.passed: list[str] = []
        self.skipped: list[str] = []

    def fail(self, check: str, msg: str) -> None:
        self.fails.append((check, msg))

    def done(self, check: str, applied: bool) -> None:
        if not applied:
            self.skipped.append(check)
        elif not any(c == check for c, _ in self.fails):
            self.passed.append(check)

    def ok(self) -> bool:
        return not self.fails


# --------------------------------------------------------------------------------------------
# helpers

def tracked_files(repo: Path) -> list[Path] | None:
    """Tracked files of a git checkout, or None when it is not one."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True,
                             check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [repo / p for p in out.decode().split("\0") if p]


def repo_files(repo: Path) -> list[Path]:
    files = tracked_files(repo)
    if files is not None:
        return [f for f in files if f.is_file()]
    skip = {".git", "target", "vendor", "node_modules"}
    out = []
    for p in repo.rglob("*"):
        rel = p.relative_to(repo).parts
        if any(part in skip or part.startswith("bazel-") for part in rel):
            continue
        if p.is_file():
            out.append(p)
    return out


def strip_hash_comments(text: str) -> str:
    """Drop `#` comments (whole-line, and trailing ones that start outside quotes)."""
    lines = []
    for line in text.splitlines():
        quote = None
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
        lines.append(line[:cut])
    return "\n".join(lines)


def strip_slash_comments(text: str) -> str:
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def forbidden_hits(text: str, forbidden: list[str]) -> list[str]:
    return [f for f in forbidden if f in text]


def bazelrc_pairs(text: str) -> set[tuple[str, str]]:
    """(command[:config], option) pairs of a .bazelrc; `-c opt` style values stay joined."""
    pairs = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.split()[0] in ("import", "try-import"):
            continue
        toks = line.split()
        cmd, rest = toks[0], toks[1:]
        i = 0
        while i < len(rest):
            opt = rest[i]
            if (opt.startswith("-") and "=" not in opt and i + 1 < len(rest)
                    and not rest[i + 1].startswith("-")):
                opt = f"{opt} {rest[i + 1]}"
                i += 1
            pairs.add((cmd, opt))
            i += 1
    return pairs


def read_bazelrc(path: Path, repo: Path, seen: set[Path] | None = None) -> str:
    """A .bazelrc with its import / try-import lines expanded."""
    seen = seen or set()
    if path in seen or not path.is_file():
        return ""
    seen.add(path)
    out = []
    for line in path.read_text().splitlines():
        toks = line.split()
        if toks and toks[0] in ("import", "try-import") and len(toks) > 1:
            target = Path(toks[1].replace("%workspace%", str(repo)))
            if not target.is_absolute():
                target = path.parent / target
            out.append(read_bazelrc(target.resolve(), repo, seen))
        else:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------------------------
# checks

def check_toolchain(cfg: Config, repo: Path, r: Report) -> None:
    if not (repo / "Cargo.toml").is_file():
        return r.done("toolchain", False)
    tc = repo / "rust-toolchain.toml"
    if not tc.is_file():
        r.fail("toolchain", f"rust-toolchain.toml missing; expected channel {cfg.rust}")
    else:
        try:
            got = tomllib.loads(tc.read_text()).get("toolchain", {})
        except tomllib.TOMLDecodeError as e:
            r.fail("toolchain", f"cannot parse rust-toolchain.toml: {e}")
            got = {}
        want = cfg.toolchain["toolchain"]
        for k in sorted(set(got) | set(want)):
            if k == "components" and set(want.get(k, [])) <= set(got.get(k, [])):
                continue                      # extra components are tools, not build settings
            if got.get(k) != want.get(k):
                r.fail("toolchain", f"rust-toolchain.toml [toolchain] {k} = {got.get(k)!r}, "
                                    f"expected {want.get(k)!r}")
    r.done("toolchain", True)


def has_binary(repo: Path, cargo: dict) -> bool:
    if cargo.get("bin"):
        return True
    if (repo / "src/main.rs").is_file():
        return True
    bindir = repo / "src/bin"
    return bindir.is_dir() and any(bindir.rglob("*.rs"))


def check_profile(cfg: Config, repo: Path, cargo: dict, r: Report) -> None:
    profiles = cargo.get("profile", {})
    binary = has_binary(repo, cargo)
    if not profiles and not binary:
        return r.done("profile", False)
    for pname in sorted(profiles):
        if pname == "release":
            continue
        allowed = cfg.allowed_profiles.get(pname)
        if allowed is None:
            r.fail("profile", f"[profile.{pname}] is not a standardised profile "
                              f"(cargo/profile-release.toml defines release only)")
        elif profiles[pname] != allowed:
            r.fail("profile", f"[profile.{pname}] = {profiles[pname]!r}, expected exactly {allowed!r} "
                              f"(versions.toml [cargo.allowed_profiles])")
    got = profiles.get("release")
    name = cargo.get("package", {}).get("name", "")
    shipped = name in cfg.shipped
    if got is None:
        if binary:
            r.fail("profile", "builds a binary but has no [profile.release]; copy "
                              "cargo/profile-release.toml")
    else:
        want = cfg.profile
        for k in sorted(set(got) | set(want)):
            if k not in want and shipped and got[k] == cfg.shipped_extra.get(k, object()):
                continue                      # allowed for a shipped package only
            if k not in want and k in cfg.shipped_extra:
                r.fail("profile", f"[profile.release] {k} = {got[k]!r} is allowed only in a shipped "
                                  f"package (versions.toml [cargo] shipped); {name or 'this package'} "
                                  f"is not one, or the value differs")
            elif k not in want:
                r.fail("profile", f"[profile.release] has extra key {k} = {got[k]!r}")
            elif k not in got:
                r.fail("profile", f"[profile.release] lacks {k} = {want[k]!r}")
            elif got[k] != want[k]:
                r.fail("profile", f"[profile.release] {k} = {got[k]!r}, expected {want[k]!r}")
    r.done("profile", True)


def dep_specs(cargo: dict):
    """(table label, crate, spec) for every dependency table, target tables included."""
    tables = [(t, cargo.get(t, {})) for t in DEP_TABLES]
    for tgt, body in cargo.get("target", {}).items():
        tables += [(f"target.{tgt}.{t}", body.get(t, {})) for t in DEP_TABLES]
    for label, table in tables:
        for crate, spec in table.items():
            name = spec.get("package", crate) if isinstance(spec, dict) else crate
            yield label, name, spec


def check_mimalloc(cfg: Config, cargo: dict, r: Report) -> None:
    specs = [(l, n, s) for l, n, s in dep_specs(cargo) if n in MIMALLOC_CRATES]
    if not specs:
        return r.done("mimalloc", False)
    for label, name, spec in specs:
        if not isinstance(spec, dict):
            spec = {"version": spec}
        want = "=" + cfg.mim[name]
        if spec.get("version") != want:
            r.fail("mimalloc", f"[{label}] {name} version {spec.get('version')!r}, expected {want!r}")
        if spec.get("optional") is not True:
            r.fail("mimalloc", f"[{label}] {name} must be optional (the allocator is a binary "
                               f"feature, never a library default)")
        if spec.get("default-features", True) is not False:
            r.fail("mimalloc", f"[{label}] {name} must set default-features = false")
    if any(n == "libmimalloc-sys" for _, n, _ in specs):
        linux_override = any(n == "libmimalloc-sys" and "linux" in l and isinstance(s, dict)
                             and "override" in s.get("features", []) for l, n, s in specs)
        if not linux_override:
            r.fail("mimalloc", "no Linux target entry for libmimalloc-sys with features = "
                               "[\"override\"] (C/C++ malloc must go through the allocator)")
    r.done("mimalloc", True)


def check_cargo_config(repo: Path, r: Report) -> None:
    tracked = tracked_files(repo)
    found = False
    for name in (".cargo/config.toml", ".cargo/config"):
        p = repo / name
        if not p.is_file():
            continue
        if tracked is not None and p not in tracked:
            continue                     # untracked local overrides are allowed
        found = True
        text = strip_hash_comments(p.read_text())
        if "rustflags" in text or "target-cpu" in text:
            r.fail("cargo-cfg", f"tracked {name} sets rustflags / target-cpu; build flags "
                                f"belong in the shared profile, not per repository")
    r.done("cargo-cfg", found)


def check_bazel(cfg: Config, repo: Path, files: list[Path], r: Report) -> None:
    rc = repo / ".bazelrc"
    if not rc.is_file() and not (repo / "MODULE.bazel").is_file():
        return r.done("bazel", False)
    text = read_bazelrc(rc, repo) if rc.is_file() else ""
    have = bazelrc_pairs(text)
    for cmd, opt in sorted(cfg.bazel_pairs - have):
        r.fail("bazel", f".bazelrc lacks `{cmd} {opt}` (bazel/common.bazelrc)")
    for cmd, opt in sorted(have):
        for m in re.finditer(r"-std=(?:c|gnu)\+\+\w+", opt):
            if m.group(0) != f"-std={cfg.std}":
                r.fail("bazel", f".bazelrc `{cmd} {opt}`: expected -std={cfg.std}")
    bv = repo / ".bazelversion"
    got = bv.read_text().strip() if bv.is_file() else None
    if got != cfg.bazel_version:
        r.fail("bazel", f".bazelversion is {got!r}, expected {cfg.bazel_version!r}")
    for f in files:
        if f.name in ("BUILD.bazel", "BUILD", "MODULE.bazel", ".bazelrc") or f.suffix == ".bzl":
            body = strip_hash_comments(f.read_text(errors="replace"))
            rel = f.relative_to(repo)
            for hit in forbidden_hits(body, cfg.forbidden):
                r.fail("bazel", f"{rel}: forbidden flag {hit}")
            for m in re.finditer(r"-std=(?:c|gnu)\+\+\w+", body):
                if m.group(0) != f"-std={cfg.std}":
                    r.fail("bazel", f"{rel}: {m.group(0)}, expected -std={cfg.std}")
    r.done("bazel", True)


CMAKE_VAR = r"\$\{(?:CMAKE_CURRENT_LIST_DIR|CMAKE_SOURCE_DIR|CMAKE_CURRENT_SOURCE_DIR|PROJECT_SOURCE_DIR)\}"


def check_cmake(cfg: Config, repo: Path, r: Report) -> None:
    cm = repo / "CMakeLists.txt"
    if not cm.is_file():
        return r.done("cmake", False)
    body = strip_hash_comments(cm.read_text())
    proj = re.search(r"^\s*project\s*\(", body, re.M | re.I)
    proj_at = proj.start() if proj else len(body)
    inc = re.search(r"include\s*\(\s*\"?([^)\s\"]*vyges-flags\.cmake)", body, re.I)
    if inc:
        path = Path(re.sub(CMAKE_VAR, str(repo), inc.group(1)))
        if not path.is_absolute():
            path = repo / path
        if not path.is_file():
            r.fail("cmake", f"includes {inc.group(1)}, which does not exist")
        elif path.read_text() != cfg.cmake_text:
            r.fail("cmake", f"{path.relative_to(repo) if path.is_relative_to(repo) else path} "
                            f"differs from cmake/vyges-flags.cmake")
        if inc.start() > proj_at:
            r.fail("cmake", "vyges-flags.cmake is included after project(); include it before")
    else:
        need = [
            (rf"set\s*\(\s*CMAKE_CXX_STANDARD\s+{cfg.std_num}\s*\)", f"set(CMAKE_CXX_STANDARD {cfg.std_num})"),
            (r"set\s*\(\s*CMAKE_CXX_STANDARD_REQUIRED\s+ON\s*\)", "set(CMAKE_CXX_STANDARD_REQUIRED ON)"),
            (r"set\s*\(\s*CMAKE_POSITION_INDEPENDENT_CODE\s+ON\s*\)", "set(CMAKE_POSITION_INDEPENDENT_CODE ON)"),
            (r"set\s*\(\s*CMAKE_BUILD_TYPE\s+Release\s*\)", "default CMAKE_BUILD_TYPE Release"),
            (rf"add_compile_options\s*\([^)]*-ffp-contract={cfg.versions['cxx']['fp_contract']}",
             f"add_compile_options(-ffp-contract={cfg.versions['cxx']['fp_contract']})"),
        ]
        for rx, what in need:
            if not re.search(rx, body, re.I):
                r.fail("cmake", f"CMakeLists.txt lacks {what} (cmake/vyges-flags.cmake)")
        osx = re.search(r"set\s*\(\s*CMAKE_OSX_DEPLOYMENT_TARGET\s+\"?([\d.]+)", body, re.I)
        if not osx:
            if cfg.cmake_osx_required:
                r.fail("cmake", f"CMakeLists.txt lacks CMAKE_OSX_DEPLOYMENT_TARGET {cfg.macos_min} "
                                f"(Bazel libraries build for macOS >= {cfg.macos_min})")
        elif osx.group(1) != cfg.macos_min:
            r.fail("cmake", f"CMAKE_OSX_DEPLOYMENT_TARGET {osx.group(1)}, expected {cfg.macos_min}")
        elif osx.start() > proj_at:
            r.fail("cmake", "CMAKE_OSX_DEPLOYMENT_TARGET is set after project(); it has no effect there")
    for hit in forbidden_hits(body, cfg.forbidden):
        r.fail("cmake", f"CMakeLists.txt: forbidden flag {hit}")
    m = re.search(r"CMAKE_CXX_STANDARD\s+(\d+)", body)
    if m and m.group(1) != cfg.std_num:
        r.fail("cmake", f"CMAKE_CXX_STANDARD {m.group(1)}, expected {cfg.std_num}")
    r.done("cmake", True)


def check_cc_shim(cfg: Config, repo: Path, r: Report) -> None:
    br = repo / "build.rs"
    if not br.is_file():
        return r.done("cc-shim", False)
    body = strip_slash_comments(br.read_text())
    applied = False
    if re.search(r"cxx_build::|cc::Build", body):
        applied = True
        if f'.std("{cfg.std}")' not in body:
            r.fail("cc-shim", f'build.rs compiles C++ without .std("{cfg.std}")')
        fpc = f"-ffp-contract={cfg.versions['cxx']['fp_contract']}"
        if not re.search(rf'\.flag(?:_if_supported)?\(\s*"{re.escape(fpc)}"\s*\)', body):
            r.fail("cc-shim", f'build.rs compiles C++ without .flag("{fpc}")')
    if "cmake::Config" in body:
        applied = True
        if not re.search(r'\.profile\(\s*"Release"\s*\)', body):
            r.fail("cc-shim", 'build.rs drives CMake without .profile("Release") (the cmake crate '
                              'otherwise picks RelWithDebInfo, -O2, when debug info is on)')
    if applied:
        for hit in forbidden_hits(body, cfg.forbidden):
            r.fail("cc-shim", f"build.rs: forbidden flag {hit}")
    r.done("cc-shim", applied)


RUNNER_RX = re.compile(r"(?<![\w.-])(ubuntu|macos|windows)-(latest|\d[\w.]*)")


def check_workflows(cfg: Config, repo: Path, r: Report) -> None:
    wdir = repo / ".github/workflows"
    flows = sorted(p for p in wdir.glob("*") if p.suffix in (".yml", ".yaml")) if wdir.is_dir() else []
    if not flows:
        return r.done("workflows", False)
    var_rx = {
        "ubuntu": re.compile(r"vars\.%s\s*\|\|\s*'([^']*)'" % re.escape(cfg.ci["ubuntu_var"])),
        "macos": re.compile(r"vars\.%s\s*\|\|\s*'([^']*)'" % re.escape(cfg.ci["macos_var"])),
    }
    expr_rx = re.compile(r"\$\{\{.*?\}\}")
    override = cfg.ci.get("test_profile_override", {})
    for f in flows:
        rel = f.relative_to(repo)
        raw = f.read_text()
        # A cargo-dist generated workflow is regenerated from dist's config, whose runner labels
        # the `dist` check pins; there a literal is allowed but must name the pinned version.
        generated = raw.startswith("# This file was autogenerated by dist")
        text = strip_hash_comments(raw)
        lines = text.splitlines()
        for n, line in enumerate(lines, 1):
            for os_name, rx in var_rx.items():
                for m in rx.finditer(line):
                    if m.group(1) != cfg.ci[os_name]:
                        r.fail("workflows", f"{rel}:{n}: {os_name} fallback '{m.group(1)}', "
                                            f"expected '{cfg.ci[os_name]}'")
            bare = expr_rx.sub("${{}}", line)
            for m in RUNNER_RX.finditer(bare):
                if generated and m.group(1) in ("ubuntu", "macos") and m.group(2) == cfg.ci[m.group(1)]:
                    continue
                r.fail("workflows", f"{rel}:{n}: hard-coded `{m.group(0)}`; use "
                                    f"{m.group(1)}-${{{{ vars.{cfg.ci.get(m.group(1) + '_var', '?')} || "
                                    f"'{cfg.ci.get(m.group(1), '?')}' }}}}")
            for m in re.finditer(r"rust-toolchain@([\w.\-]+)", line):
                # @master takes the version from a `toolchain:` input; allowed only when the next
                # lines of the step pass one computed in the workflow (from rust-toolchain.toml,
                # which the toolchain check pins).
                if m.group(1) == "master" and any(
                        re.match(r"\s*toolchain:\s*\$\{\{", nxt) for nxt in lines[n:n + 4]):
                    continue
                if m.group(1) != cfg.rust:
                    r.fail("workflows", f"{rel}:{n}: rust-toolchain@{m.group(1)}, expected @{cfg.rust}")
            m = re.search(r"^\s*toolchain:\s*['\"]?([\w.\-]+)", line)
            if m and m.group(1) != cfg.rust:
                r.fail("workflows", f"{rel}:{n}: toolchain {m.group(1)}, expected {cfg.rust}")
            for m in re.finditer(r"bazelisk/releases/download/v([\d.]+)", line):
                if m.group(1) != cfg.bazelisk:
                    r.fail("workflows", f"{rel}:{n}: bazelisk v{m.group(1)}, expected v{cfg.bazelisk}")
            m = re.search(r"(CARGO_PROFILE_RELEASE_[A-Z_]+)\s*[:=]\s*['\"]?([\w.\-]+)", line)
            if m:
                key, val = m.group(1), m.group(2)
                if key not in override:
                    r.fail("workflows", f"{rel}:{n}: {key} is not an allowed CI profile override")
                elif override[key] != val:
                    r.fail("workflows", f"{rel}:{n}: {key}={val}, expected {override[key]}")
    r.done("workflows", True)


def check_dist_runners(cfg: Config, repo: Path, cargo: dict, r: Report) -> None:
    """cargo-dist builds its job matrix at run time from its own config, where a workflow
    expression cannot be evaluated, so its runner labels are literals: they must name the
    versions.toml versions."""
    tables = []
    dw = repo / "dist-workspace.toml"
    if dw.is_file():
        try:
            tables.append(("dist-workspace.toml", tomllib.loads(dw.read_text()).get("dist", {})))
        except tomllib.TOMLDecodeError as e:
            r.fail("dist", f"cannot parse dist-workspace.toml: {e}")
    meta = cargo.get("workspace", {}).get("metadata", {}).get("dist")
    if isinstance(meta, dict):
        tables.append(("Cargo.toml [workspace.metadata.dist]", meta))
    if not tables:
        return r.done("dist", False)
    for where, dist in tables:
        for key, val in dist.get("github-custom-runners", {}).items():
            label = val.get("runner") if isinstance(val, dict) else val
            m = re.fullmatch(r"(ubuntu|macos)-([\w.]+?)(-arm)?", str(label))
            if not m:
                r.fail("dist", f"{where} github-custom-runners.{key} = {label!r} is not an "
                               f"ubuntu-<version> or macos-<version> label")
            elif m.group(2) != cfg.ci[m.group(1)]:
                r.fail("dist", f"{where} github-custom-runners.{key} = {label!r}, expected "
                               f"{m.group(1)}-{cfg.ci[m.group(1)]}{m.group(3) or ''}")
    r.done("dist", True)


def check_repo(cfg: Config, repo: Path) -> Report:
    r = Report(repo)
    files = repo_files(repo)
    check_toolchain(cfg, repo, r)
    cargo_path = repo / "Cargo.toml"
    cargo = {}
    if cargo_path.is_file():
        try:
            cargo = tomllib.loads(cargo_path.read_text())
        except tomllib.TOMLDecodeError as e:
            r.fail("profile", f"cannot parse Cargo.toml: {e}")
    if cargo:
        check_profile(cfg, repo, cargo, r)
        check_mimalloc(cfg, cargo, r)
    else:
        r.done("profile", False)
        r.done("mimalloc", False)
    check_cargo_config(repo, r)
    check_bazel(cfg, repo, files, r)
    check_cmake(cfg, repo, r)
    check_cc_shim(cfg, repo, r)
    check_workflows(cfg, repo, r)
    check_dist_runners(cfg, repo, cargo, r)
    return r


def check_self(cfg: Config) -> Report:
    """The templates agree with versions.toml, and this repository's own workflows conform."""
    r = Report(cfg.root)
    tc = cfg.toolchain.get("toolchain", {})
    if tc.get("channel") != cfg.rust:
        r.fail("self", f"cargo/rust-toolchain.toml channel {tc.get('channel')!r} != versions.toml {cfg.rust!r}")
    if tc.get("components") != cfg.versions["rust"].get("components"):
        r.fail("self", "cargo/rust-toolchain.toml components != versions.toml [rust] components")
    if set(cfg.profile_doc) != {"profile"} or set(cfg.profile_doc["profile"]) != {"release"}:
        r.fail("self", "cargo/profile-release.toml must hold [profile.release] only")
    pairs = cfg.bazel_pairs
    want = {
        ("build", f"--cxxopt=-std={cfg.std}"), ("build", f"--host_cxxopt=-std={cfg.std}"),
        ("build", f"--copt=-{cfg.versions['cxx']['opt_level']}"),
        ("build", f"--copt=-ffp-contract={cfg.versions['cxx']['fp_contract']}"),
        ("build", f"--host_copt=-ffp-contract={cfg.versions['cxx']['fp_contract']}"),
        ("build:macos", f"--macos_minimum_os={cfg.macos_min}"),
        ("build:macos", f"--host_macos_minimum_os={cfg.macos_min}"),
    }
    for cmd, opt in sorted(want - pairs):
        r.fail("self", f"bazel/common.bazelrc lacks `{cmd} {opt}` implied by versions.toml")
    for cmd, opt in sorted(pairs):
        for m in re.finditer(r"-std=(?:c|gnu)\+\+\w+|_minimum_os=([\d.]+)", opt):
            if m.group(0).startswith("-std") and m.group(0) != f"-std={cfg.std}":
                r.fail("self", f"bazel/common.bazelrc `{cmd} {opt}` disagrees with versions.toml")
            if m.group(1) and m.group(1) != cfg.macos_min:
                r.fail("self", f"bazel/common.bazelrc `{cmd} {opt}` disagrees with versions.toml")
    for hit in forbidden_hits(strip_hash_comments(cfg.bazelrc_text), cfg.forbidden):
        r.fail("self", f"bazel/common.bazelrc carries forbidden flag {hit}")
    cm = strip_hash_comments(cfg.cmake_text)
    for rx, what in [
        (rf"set\(CMAKE_CXX_STANDARD {cfg.std_num}\)", f"CMAKE_CXX_STANDARD {cfg.std_num}"),
        (rf"set\(CMAKE_OSX_DEPLOYMENT_TARGET {re.escape(cfg.macos_min)}\)",
         f"CMAKE_OSX_DEPLOYMENT_TARGET {cfg.macos_min}"),
        (rf"add_compile_options\(-ffp-contract={cfg.versions['cxx']['fp_contract']}\)", "-ffp-contract"),
    ]:
        if not re.search(rx, cm):
            r.fail("self", f"cmake/vyges-flags.cmake lacks {what} implied by versions.toml")
    for hit in forbidden_hits(cm, cfg.forbidden):
        r.fail("self", f"cmake/vyges-flags.cmake carries forbidden flag {hit}")
    r.done("self", True)
    wf = check_repo(cfg, cfg.root)
    r.fails += wf.fails
    r.passed += [c for c in wf.passed if c != "self"]
    return r


def emit(r: Report, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"repo": str(r.repo), "ok": r.ok(), "passed": r.passed,
                          "skipped": r.skipped,
                          "fails": [{"check": c, "message": m} for c, m in r.fails]}))
        return
    print(f"== {r.repo}: {'CONFORMS' if r.ok() else 'DRIFTS'}")
    for c, m in r.fails:
        print(f"  FAIL {c}: {m}")
    if r.passed:
        print(f"  ok: {', '.join(r.passed)}")
    if r.skipped:
        print(f"  n/a: {', '.join(r.skipped)}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repos", nargs="*", type=Path)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--self", dest="self_check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.self_check and not a.repos:
        ap.print_usage(sys.stderr)
        print("check.py: give a repository path, or --self", file=sys.stderr)
        return 2
    try:
        cfg = Config(a.config.resolve())
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        print(f"check.py: cannot read config in {a.config}: {e}", file=sys.stderr)
        return 2
    reports = []
    if a.self_check:
        reports.append(check_self(cfg))
    for repo in a.repos:
        if not repo.is_dir():
            print(f"check.py: {repo}: not a directory", file=sys.stderr)
            return 2
        reports.append(check_repo(cfg, repo.resolve()))
    for r in reports:
        emit(r, a.json)
    return 0 if all(r.ok() for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
