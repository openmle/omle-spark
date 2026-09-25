#!/usr/bin/env python3
"""Copy the built JARs into the omle-spark Python package before the wheel build.

The wheel ships four JARs so that `pip install omle-spark` is sufficient on a
real cluster:

  omle-spark_2.12-<v>.jar   Scala transformer for PySpark 3.x
  omle-spark_2.13-<v>.jar   Scala transformer for PySpark 4.x
  omle-runtime-<v>.jar      Java bindings + native libomleruntime per platform
  jna-<v>.jar               JNA, which omle-runtime loads the native lib through

JNA has to be shipped explicitly. It is a declared Maven dependency of
omle-runtime, but `spark.jars` puts bare JARs on the classpath and resolves no
transitive dependencies, so without it the first call fails with
`NoClassDefFoundError: com/sun/jna/Library`.

Both Scala builds are required. They are binary-incompatible and PyPI ships
Spark 3.x built against 2.12 and 4.x against 2.13, so a wheel carrying only one
silently fails on the other with `'JavaPackage' object is not callable`.

The omle-runtime jar comes from Maven Central, resolved by sbt alongside the
rest of the dependencies. It carries the native libraries under JNA's resource
prefixes (linux-x86-64, darwin-aarch64, ...), so JNA extracts the right one
from the classpath on each executor and no -Djna.library.path is needed. A jar
shipping classes only is what --require-natives refuses to build a wheel
around: it would install cleanly and then fail with UnsatisfiedLinkError at
first use.

Run from anywhere:

    python scripts/stage_jars.py [--require-natives]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPARK_TARGET = REPO / "target"
DEST = REPO / "python" / "omle_spark" / "jars"

# omle-runtime and JNA are resolved from Maven Central by sbt rather than built
# here, so they are collected out of the Coursier cache that `sbt package`
# already populated, via the paths sbt reports for them.
#
# This file is regenerated on every run. It used to be written by the caller and
# merely read here, which made a stale one invisible: bumping omleRuntimeVersion
# in build.sbt left the previous release's path on disk, and staging copied that
# jar into the wheel without a word. The result installs and imports cleanly,
# then misbehaves at the ABI boundary — which is exactly how a renumbered
# column-type constant once reached a published wheel.
RUNTIME_JARS_LIST = REPO / "target" / "dependency-jars.txt"

# Resolved for sbt's default scalaVersion unless --sbt-scala names another. One
# resolution is enough either way: both artifacts taken from it are plain Java,
# so omle-runtime and JNA are identical across 2.12 and 2.13. The omle-spark
# jars are the per-version ones, and they come from target/scala-*/ instead.
SBT_EXPORT_TASK = "export Compile / dependencyClasspath"

# The directory names JNA looks under inside a JAR. omle-runtime's own release
# workflow stages its libraries at exactly these paths.
JNA_PREFIXES = ("linux-x86-64", "linux-aarch64", "darwin-aarch64", "win32-x86-64")

# sbt and Maven both leave sources/javadoc artifacts next to the real JAR.
_AUX = ("-sources.jar", "-javadoc.jar")


def _newest(paths: list[Path]) -> Path:
    return max(paths, key=lambda p: p.stat().st_mtime)


def _find_spark_jars() -> dict[str, Path]:
    """One omle-spark JAR per Scala version, newest wins."""
    found: dict[str, Path] = {}
    if not SPARK_TARGET.is_dir():
        return found
    for jar in SPARK_TARGET.glob("scala-*/omle-spark_*.jar"):
        if jar.name.endswith(_AUX):
            continue
        scala = jar.name.split("_", 1)[1].split("-", 1)[0]
        found.setdefault(scala, []).append(jar)  # type: ignore[arg-type]
    return {k: _newest(v) for k, v in found.items()}  # type: ignore[arg-type]


def _refresh_jars_list(scala: str | None = None) -> int:
    """Ask sbt for the dependency classpath and rewrite RUNTIME_JARS_LIST.

    The list is taken from sbt rather than by globbing the Coursier cache: the
    cache layout is platform-specific, and globbing it is how the old JNA lookup
    came to work on macOS and fail on Linux.
    """
    sbt = shutil.which("sbt")
    if sbt is None:
        print("::error::sbt is not on PATH, so the dependency list cannot be "
              "refreshed. Install sbt, or pass --no-refresh to reuse the "
              f"existing {RUNTIME_JARS_LIST.name} (only safe when it was "
              "generated for the current build.sbt).", file=sys.stderr)
        return 1

    cmd = [sbt, "-batch", "-Dsbt.log.noformat=true"]
    if scala:
        cmd.append(f"++{scala}")
    cmd.append(SBT_EXPORT_TASK)

    print("resolving dependencies: " + " ".join(cmd[3:]))
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        print(f"::error::sbt exited {proc.returncode} while resolving "
              "dependencies:", file=sys.stderr)
        for line in (proc.stdout + proc.stderr).splitlines()[-15:]:
            print(f"  {line}", file=sys.stderr)
        return 1

    # `export` prints the classpath unadorned; sbt's own logging is bracketed.
    # Splitting on os.pathsep rather than ":" keeps Windows drive letters intact.
    entries = [
        part.strip()
        for line in proc.stdout.splitlines() if not line.startswith("[")
        for part in line.split(os.pathsep)
        if part.strip().endswith(".jar")
    ]
    if not entries:
        print(f"::error::sbt succeeded but reported no jars for "
              f"'{SBT_EXPORT_TASK}'. Output:", file=sys.stderr)
        for line in proc.stdout.splitlines()[-15:]:
            print(f"  {line}", file=sys.stderr)
        return 1

    RUNTIME_JARS_LIST.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_JARS_LIST.write_text("\n".join(entries) + "\n")
    print(f"  {len(entries)} jars -> "
          f"{RUNTIME_JARS_LIST.relative_to(REPO)}")
    return 0


def _resolved_jars() -> list[Path]:
    """Dependency jars sbt resolved, read from target/dependency-jars.txt."""
    if not RUNTIME_JARS_LIST.is_file():
        return []
    return [Path(line.strip()) for line in RUNTIME_JARS_LIST.read_text().splitlines()
            if line.strip() and Path(line.strip()).suffix == ".jar"]


def _pinned_runtime_version() -> str | None:
    """The omle-runtime version build.sbt asks for, if it can be read."""
    m = re.search(r'^val\s+omleRuntimeVersion\s*=\s*"([^"]+)"',
                  (REPO / "build.sbt").read_text(), re.MULTILINE)
    return m.group(1) if m else None


def _pick(jars: list[Path], stem: str) -> Path | None:
    """The newest jar whose name starts with *stem* and is not an aux artifact."""
    hits = [p for p in jars
            if p.name.startswith(stem) and not p.name.endswith(_AUX)]
    return _newest(hits) if hits else None


def _find_jna_jar() -> Path | None:
    jars = _resolved_jars()
    # jna-platform- also starts with "jna-", so match the version digit.
    return next((p for p in jars
                 if p.name.startswith("jna-") and not p.name.startswith("jna-platform")
                 and not p.name.endswith(_AUX)), None)


def _find_runtime_jar() -> Path | None:
    return _pick(_resolved_jars(), "omle-runtime-")


def _natives_in(jar: Path) -> list[str]:
    with zipfile.ZipFile(jar) as z:
        names = z.namelist()
    return [n for n in names
            if n.startswith(JNA_PREFIXES) and n.rsplit("/", 1)[-1]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--require-natives", action="store_true",
        help="fail unless the omle-runtime JAR carries a native library for "
             "every platform (what a published wheel needs)",
    )
    ap.add_argument(
        "--scala", action="append", metavar="VERSION",
        help="Scala binary version to stage, repeatable. Defaults to both, "
             "which is what a published wheel needs. A test job that built "
             "only one version with `sbt ++<version> package` should name it, "
             "otherwise staging fails on the absent one.",
    )
    ap.add_argument(
        "--sbt-scala", metavar="VERSION",
        help="full Scala version to resolve against (e.g. 2.13.16), passed to "
             "sbt as ++VERSION. The jars taken from the resolution are plain "
             "Java and identical either way, so this is purely to avoid "
             "resolving the default version's Spark dependencies in a job that "
             "built the other one. Unrelated to --scala, which selects which "
             "already-built omle-spark jars to stage.",
    )
    ap.add_argument(
        "--no-refresh", action="store_true",
        help="reuse the existing target/dependency-jars.txt instead of asking "
             "sbt for it. For environments without sbt; the version check "
             "below still refuses a list that disagrees with build.sbt.",
    )
    args = ap.parse_args()

    if args.no_refresh:
        print(f"--no-refresh: reusing {RUNTIME_JARS_LIST.relative_to(REPO)}")
    elif (rc := _refresh_jars_list(args.sbt_scala)) != 0:
        return rc

    wanted = args.scala or ["2.12", "2.13"]

    spark_jars = _find_spark_jars()
    missing = [v for v in wanted if v not in spark_jars]
    if missing:
        built = ", ".join(sorted(spark_jars)) or "none"
        print(f"::error::no omle-spark JAR for Scala {', '.join(missing)} "
              f"(built: {built}) — run `sbt +package` for both, or name the "
              f"one you built with --scala", file=sys.stderr)
        return 1
    spark_jars = {v: j for v, j in spark_jars.items() if v in wanted}

    runtime = _find_runtime_jar()
    if runtime is None:
        print(f"::error::no omle-runtime jar among the resolved dependencies "
              f"listed in {RUNTIME_JARS_LIST}.", file=sys.stderr)
        return 1

    # Belt and braces. A refreshed list agrees with build.sbt by construction,
    # so this only ever fires for --no-refresh — which is the one path that can
    # still hand us last week's resolution.
    pinned = _pinned_runtime_version()
    if pinned and runtime.name != f"omle-runtime-{pinned}.jar":
        print(f"::error::build.sbt pins omle-runtime {pinned} but the resolved "
              f"dependencies name {runtime.name}. "
              f"{RUNTIME_JARS_LIST.relative_to(REPO)} is stale; rerun without "
              "--no-refresh.", file=sys.stderr)
        return 1

    jna = _find_jna_jar()
    if jna is None:
        print(f"::error::no JNA jar among the resolved dependencies in "
              f"{RUNTIME_JARS_LIST}. Without it the JVM side fails with "
              "NoClassDefFoundError: com/sun/jna/Library.", file=sys.stderr)
        return 1

    natives = _natives_in(runtime)
    if natives:
        print(f"{runtime.name} carries {len(natives)} native librar"
              f"{'y' if len(natives) == 1 else 'ies'}:")
        for n in sorted(natives):
            print(f"    {n}")
    if args.require_natives:
        have = {n.split("/", 1)[0] for n in natives}
        absent = [p for p in JNA_PREFIXES if p not in have]
        if absent:
            print(f"::error::{runtime.name} has no native library for: "
                  f"{', '.join(absent)}. A wheel built from this would work "
                  "only where libomleruntime is already installed. Depend on "
                  "an omle-runtime release that ships them.", file=sys.stderr)
            return 1

    # Wipe first: stale JARs from an earlier version would otherwise ship
    # alongside the current ones, and jars() would put both on the classpath.
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    for jar in [*spark_jars.values(), runtime, jna]:
        shutil.copy2(jar, DEST / jar.name)
        print(f"staged {jar.name}  ({jar.stat().st_size / 1024:.0f} KB)")

    total = sum(p.stat().st_size for p in DEST.glob("*.jar"))
    print(f"\n{DEST.relative_to(REPO)}: {len(list(DEST.glob('*.jar')))} JARs, "
          f"{total / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
