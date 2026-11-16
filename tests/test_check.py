# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/check.py, judged by EXIT CODE.

A conforming fixture repository is generated from this repository's own templates, so the
fixture cannot drift from them. The checker must pass it (exit 0), and must FAIL (exit 1, with
the named message) on each deliberately wrong variant: a check that cannot fail proves nothing.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_fixture(dest: Path, cmake_include: bool = False) -> None:
    """A repository that uses every checked mechanism, written to conform."""
    profile = (ROOT / "cargo/profile-release.toml").read_text()
    write(dest / "Cargo.toml", f"""[package]
name = "fixture"
version = "0.1.0"
edition = "2021"

[features]
default = ["mimalloc"]
mimalloc = ["dep:mimalloc", "dep:libmimalloc-sys"]

[dependencies]
mimalloc = {{ version = "=0.1.48", default-features = false, optional = true }}
libmimalloc-sys = {{ version = "=0.1.44", default-features = false, optional = true }}

[target.'cfg(target_os = "linux")'.dependencies]
libmimalloc-sys = {{ version = "=0.1.44", default-features = false, features = ["override"], optional = true }}

{profile}""")
    write(dest / "src/main.rs", "fn main() {}\n")
    shutil.copy(ROOT / "cargo/rust-toolchain.toml", dest / "rust-toolchain.toml")
    write(dest / ".bazelrc", (ROOT / "bazel/common.bazelrc").read_text()
          + "\n# library-specific\ncommon --@dep//:use_readline=false\n")
    write(dest / ".bazelversion", "9.1.1\n")
    write(dest / "MODULE.bazel", 'module(name = "fixture")\n')
    write(dest / "BUILD.bazel", 'cc_library(name = "x", copts = ["-std=c++20"])\n')
    if cmake_include:
        shutil.copy(ROOT / "cmake/vyges-flags.cmake", dest / "vyges-flags.cmake")
        write(dest / "CMakeLists.txt", "cmake_minimum_required(VERSION 3.20)\n"
              "include(${CMAKE_CURRENT_LIST_DIR}/vyges-flags.cmake)\n"
              "project(fixture LANGUAGES CXX)\n")
    else:
        write(dest / "CMakeLists.txt", """cmake_minimum_required(VERSION 3.20)
if(APPLE AND NOT CMAKE_OSX_DEPLOYMENT_TARGET)
  set(CMAKE_OSX_DEPLOYMENT_TARGET 13.3)
endif()
project(fixture LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-ffp-contract=off)
""")
    write(dest / "build.rs", """fn main() {
    let dst = cmake::Config::new(".").profile("Release").build();
    let mut b = cxx_build::bridges(["src/lib.rs"]);
    b.file("src/shim.cc").std("c++20");
    b.flag("-ffp-contract=off");
    b.compile("shim");
}
""")
    write(dest / ".github/workflows/ci.yml", """name: ci
on: [push]
jobs:
  test:
    strategy:
      matrix:
        include:
          - { os: "ubuntu-${{ vars.VYGES_UBUNTU_VERSION || '24.04' }}" }
          - { os: "ubuntu-${{ vars.VYGES_UBUNTU_VERSION || '24.04' }}-arm" }
          - { os: "macos-${{ vars.VYGES_MACOS_VERSION || '15' }}" }
    runs-on: ${{ matrix.os }}
    env:
      CARGO_PROFILE_RELEASE_LTO: "false"
      CARGO_PROFILE_RELEASE_CODEGEN_UNITS: "16"
    steps:
      - uses: actions/checkout@v7
      - uses: dtolnay/rust-toolchain@1.100.0
      - run: curl -fsSL -o bazelisk https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-amd64
      - run: cargo test --release
""")


def sub(path: Path, old: str, new: str) -> None:
    """Replace the LAST occurrence (template comments may quote the same text earlier)."""
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    head, _, tail = text.rpartition(old)
    path.write_text(head + new + tail)


def drop_osx(d: Path) -> None:
    sub(d / "CMakeLists.txt",
        "if(APPLE AND NOT CMAKE_OSX_DEPLOYMENT_TARGET)\n  set(CMAKE_OSX_DEPLOYMENT_TARGET 13.3)\nendif()\n", "")


# (name, mutation(repo), substring the failure message must contain)
MUTATIONS = [
    ("toolchain channel", lambda d: sub(d / "rust-toolchain.toml", "1.100.0", "1.99.0"),
     "[toolchain] channel = '1.99.0'"),
    ("toolchain missing", lambda d: (d / "rust-toolchain.toml").unlink(), "rust-toolchain.toml missing"),
    ("lto thin", lambda d: sub(d / "Cargo.toml", 'lto = "fat"', 'lto = "thin"'), "lto = 'thin'"),
    ("extra strip", lambda d: sub(d / "Cargo.toml", "codegen-units = 1", "codegen-units = 1\nstrip = true"),
     "allowed only in a shipped package"),
    ("strip false in shipped", lambda d: (sub(d / "Cargo.toml", 'name = "fixture"', 'name = "vyges-suite"'),
                                          sub(d / "Cargo.toml", "codegen-units = 1", "codegen-units = 1\nstrip = false")),
     "allowed only in a shipped package"),
    ("extra key", lambda d: sub(d / "Cargo.toml", "codegen-units = 1", "codegen-units = 1\nopt-level = 2"),
     "extra key opt-level"),
    ("profile missing", lambda d: sub(d / "Cargo.toml", "[profile.release]", "[profile.dummy]"),
     "has no [profile.release]"),
    ("other profile", lambda d: d.joinpath("Cargo.toml").write_text(
        d.joinpath("Cargo.toml").read_text() + "\n[profile.bench]\ndebug = true\n"),
     "[profile.bench] is not a standardised profile"),
    ("mimalloc pin", lambda d: sub(d / "Cargo.toml", '"=0.1.48"', '"=0.1.47"'), "mimalloc version '=0.1.47'"),
    ("mimalloc not optional", lambda d: sub(d / "Cargo.toml", '"=0.1.48", default-features = false, optional = true',
                                            '"=0.1.48", default-features = false'), "must be optional"),
    ("no linux override", lambda d: sub(d / "Cargo.toml", 'features = ["override"], ', ""), "override"),
    ("bazelrc no fp-contract", lambda d: sub(d / ".bazelrc",
        "build --copt=-ffp-contract=off --host_copt=-ffp-contract=off\n", ""),
     "lacks `build --copt=-ffp-contract=off`"),
    ("bazelrc std", lambda d: sub(d / ".bazelrc", "--cxxopt=-std=c++20", "--cxxopt=-std=c++17"),
     "expected -std=c++20"),
    ("bazelrc macos min", lambda d: sub(d / ".bazelrc", "build:macos --macos_minimum_os=13.3",
                                        "build:macos --macos_minimum_os=12.0"),
     "lacks `build:macos --macos_minimum_os=13.3`"),
    ("BUILD march", lambda d: sub(d / "BUILD.bazel", '"-std=c++20"', '"-std=c++20", "-march=native"'),
     "forbidden flag -march="),
    ("BUILD fast-math", lambda d: sub(d / "BUILD.bazel", '"-std=c++20"', '"-std=c++20", "-ffast-math"'),
     "forbidden flag -ffast-math"),
    ("bazelversion", lambda d: (d / ".bazelversion").write_text("8.0.0\n"), ".bazelversion is '8.0.0'"),
    ("cmake no fp-contract", lambda d: sub(d / "CMakeLists.txt", "add_compile_options(-ffp-contract=off)\n", ""),
     "lacks add_compile_options(-ffp-contract=off)"),
    ("cmake std 17", lambda d: sub(d / "CMakeLists.txt", "CMAKE_CXX_STANDARD 20", "CMAKE_CXX_STANDARD 17"),
     "CMAKE_CXX_STANDARD 17, expected 20"),
    ("cmake osx after project", lambda d: (
        sub(d / "CMakeLists.txt", "if(APPLE AND NOT CMAKE_OSX_DEPLOYMENT_TARGET)\n  set(CMAKE_OSX_DEPLOYMENT_TARGET 13.3)\nendif()\n", ""),
        d.joinpath("CMakeLists.txt").write_text(d.joinpath("CMakeLists.txt").read_text()
                                                + "set(CMAKE_OSX_DEPLOYMENT_TARGET 13.3)\n")),
     "set after project()"),
    ("shim no fp-contract", lambda d: sub(d / "build.rs", '    b.flag("-ffp-contract=off");\n', ""),
     'without .flag("-ffp-contract=off")'),
    ("shim std", lambda d: sub(d / "build.rs", '.std("c++20")', '.std("c++17")'), 'without .std("c++20")'),
    ("cmake crate profile", lambda d: sub(d / "build.rs", '.profile("Release")', ""),
     'without .profile("Release")'),
    ("runner latest", lambda d: sub(d / ".github/workflows/ci.yml", "runs-on: ${{ matrix.os }}",
                                    "runs-on: ubuntu-latest"), "hard-coded `ubuntu-latest`"),
    ("runner pinned literal", lambda d: sub(d / ".github/workflows/ci.yml",
        "\"macos-${{ vars.VYGES_MACOS_VERSION || '15' }}\"", '"macos-15"'), "hard-coded `macos-15`"),
    ("runner fallback", lambda d: sub(d / ".github/workflows/ci.yml", "|| '24.04'", "|| '22.04'"),
     "ubuntu fallback '22.04'"),
    ("rust stable", lambda d: sub(d / ".github/workflows/ci.yml", "rust-toolchain@1.100.0", "rust-toolchain@stable"),
     "rust-toolchain@stable, expected @1.100.0"),
    ("bazelisk", lambda d: sub(d / ".github/workflows/ci.yml", "download/v1.25.0", "download/v1.24.0"),
     "bazelisk v1.24.0"),
    ("ci override value", lambda d: sub(d / ".github/workflows/ci.yml", 'CARGO_PROFILE_RELEASE_LTO: "false"',
                                        'CARGO_PROFILE_RELEASE_LTO: "thin"'), "CARGO_PROFILE_RELEASE_LTO=thin"),
    ("ci override key", lambda d: sub(d / ".github/workflows/ci.yml", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
                                      "CARGO_PROFILE_RELEASE_OPT_LEVEL"), "not an allowed CI profile override"),
    ("Cargo.toml unparseable", lambda d: sub(d / "Cargo.toml", "[package]", "[package"),
     "cannot parse"),
    ("dist profile adds lto", lambda d: d.joinpath("Cargo.toml").write_text(
        d.joinpath("Cargo.toml").read_text() + '\n[profile.dist]\ninherits = "release"\nlto = "thin"\n'),
     "[profile.dist] = "),
    ("toolchain missing clippy", lambda d: sub(d / "rust-toolchain.toml", '["clippy"]', '["rustfmt"]'),
     "[toolchain] components"),
    ("dist runner old ubuntu", lambda d: write(d / "dist-workspace.toml",
        '[dist.github-custom-runners]\nx86_64-unknown-linux-gnu = "ubuntu-22.04"\n'),
     "expected ubuntu-24.04"),
    ("dist runner latest", lambda d: write(d / "dist-workspace.toml",
        '[dist.github-custom-runners]\nglobal = "ubuntu-latest"\n'),
     "expected ubuntu-24.04"),
    ("cargo config rustflags", lambda d: write(d / ".cargo/config.toml",
        '[build]\nrustflags = ["-C", "target-cpu=native"]\n'), "rustflags"),
]


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build-config-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture(self, name: str, **kw) -> Path:
        d = self.tmp / re.sub(r"\W+", "-", name)
        make_fixture(d, **kw)
        return d

    def assert_exit(self, proc, code, needle=None):
        self.assertEqual(proc.returncode, code, f"exit {proc.returncode}\n{proc.stdout}{proc.stderr}")
        if needle is not None:
            self.assertIn(needle, proc.stdout, proc.stdout)

    def test_conforming_fixture_passes(self):
        self.assert_exit(run(str(self.fixture("good"))), 0, "CONFORMS")

    def test_conforming_fixture_with_cmake_include_passes(self):
        self.assert_exit(run(str(self.fixture("good-include", cmake_include=True))), 0, "CONFORMS")

    def test_every_rule_fails_on_a_wrong_input(self):
        for name, mutate, needle in MUTATIONS:
            with self.subTest(name):
                d = self.fixture(name)
                mutate(d)
                self.assert_exit(run(str(d)), 1, needle)

    def test_cmake_include_of_a_drifted_copy_fails(self):
        d = self.fixture("bad-include", cmake_include=True)
        sub(d / "vyges-flags.cmake", "add_compile_options(-ffp-contract=off)", "")
        self.assert_exit(run(str(d)), 1, "differs from cmake/vyges-flags.cmake")

    def test_commented_runner_is_not_a_finding(self):
        d = self.fixture("commented")
        sub(d / ".github/workflows/ci.yml", "    steps:", "    # runs-on: ubuntu-latest (old)\n    steps:")
        self.assert_exit(run(str(d)), 0)

    def test_strip_allowed_in_a_shipped_package(self):
        d = self.fixture("shipped")
        sub(d / "Cargo.toml", 'name = "fixture"', 'name = "vyges-suite"')
        sub(d / "Cargo.toml", "codegen-units = 1", "codegen-units = 1\nstrip = true")
        self.assert_exit(run(str(d)), 0, "CONFORMS")

    def test_cargo_dist_layout_passes(self):
        d = self.fixture("dist")
        sub(d / "rust-toolchain.toml", '["clippy"]', '["rustfmt", "clippy"]')
        d.joinpath("Cargo.toml").write_text(d.joinpath("Cargo.toml").read_text()
                                            + '\n[profile.dist]\ninherits = "release"\n')
        write(d / "dist-workspace.toml", '[dist.github-custom-runners]\n'
              'global = "ubuntu-24.04"\nx86_64-unknown-linux-gnu = "ubuntu-24.04"\n'
              'aarch64-unknown-linux-gnu = "ubuntu-24.04-arm"\naarch64-apple-darwin = "macos-15"\n')
        self.assert_exit(run(str(d)), 0, "CONFORMS")

    def test_toolchain_master_with_computed_input_passes(self):
        d = self.fixture("master")
        sub(d / ".github/workflows/ci.yml", "      - uses: dtolnay/rust-toolchain@1.100.0\n",
            "      - uses: dtolnay/rust-toolchain@master\n        with:\n"
            "          toolchain: ${{ steps.tc.outputs.toolchain }}\n")
        self.assert_exit(run(str(d)), 0, "CONFORMS")

    def test_toolchain_master_without_input_fails(self):
        d = self.fixture("master-bare")
        sub(d / ".github/workflows/ci.yml", "rust-toolchain@1.100.0", "rust-toolchain@master")
        self.assert_exit(run(str(d)), 1, "rust-toolchain@master, expected @1.100.0")

    def test_dist_generated_workflow_literals(self):
        d = self.fixture("dist-gen")
        gen = "# This file was autogenerated by dist: https://example.invalid\nname: Release\njobs:\n  plan:\n"
        write(d / ".github/workflows/release.yml", gen + '    runs-on: "ubuntu-24.04"\n')
        self.assert_exit(run(str(d)), 0, "CONFORMS")
        write(d / ".github/workflows/release.yml", gen + '    runs-on: "ubuntu-22.04"\n')
        self.assert_exit(run(str(d)), 1, "hard-coded `ubuntu-22.04`")

    def test_cmake_osx_target_optional_until_required(self):
        d = self.fixture("osx-missing")
        drop_osx(d)
        self.assert_exit(run(str(d)), 0, "CONFORMS")
        cfg = self.tmp / "config-osx"
        shutil.copytree(ROOT, cfg, ignore=shutil.ignore_patterns(".git"))
        sub(cfg / "versions.toml", "cmake_target_required = false", "cmake_target_required = true")
        self.assert_exit(run("--config", str(cfg), str(d)), 1, "lacks CMAKE_OSX_DEPLOYMENT_TARGET 13.3")

    def test_self_check_passes(self):
        self.assert_exit(run("--self"), 0, "CONFORMS")

    def test_self_check_fails_when_a_template_disagrees(self):
        cfg = self.tmp / "config"
        shutil.copytree(ROOT, cfg, ignore=shutil.ignore_patterns(".git"))
        sub(cfg / "versions.toml", 'toolchain = "1.100.0"', 'toolchain = "1.101.0"')
        self.assert_exit(run("--config", str(cfg), "--self"), 1, "cargo/rust-toolchain.toml channel")
        cfg2 = self.tmp / "config2"
        shutil.copytree(ROOT, cfg2, ignore=shutil.ignore_patterns(".git"))
        sub(cfg2 / "versions.toml", 'minimum = "13.3"', 'minimum = "14.0"')
        self.assert_exit(run("--config", str(cfg2), "--self"), 1, "bazel/common.bazelrc lacks")

    def test_bad_arguments_exit_2(self):
        self.assert_exit(run(), 2)
        self.assert_exit(run(str(self.tmp / "missing")), 2)


if __name__ == "__main__":
    unittest.main()
