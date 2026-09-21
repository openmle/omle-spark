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
rest of the dependencies. Releases from 0.1.0-rc9 onward carry the native
libraries under JNA's resource prefixes (linux-x86-64, darwin-aarch64, ...), so
JNA extracts the right one from the classpath on each executor and no
-Djna.library.path is needed. Earlier release candidates shipped classes only;
--require-natives refuses to build a wheel around one of those, since it would
install cleanly and then fail with UnsatisfiedLinkError at first use.

Run from anywhere:

    python scripts/stage_jars.py [--require-natives]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPARK_TARGET = REPO / "target"
DEST = REPO / "python" / "omle_spark" / "jars"

# omle-runtime and JNA are resolved from Maven Central by sbt rather than built
# here, so they are collected out of the Coursier cache that `sbt package`
# already populated. `sbt exportDependencies` writes their paths to this file;
# see the CI workflow.
RUNTIME_JARS_LIST = REPO / "target" / "dependency-jars.txt"

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


def _resolved_jars() -> list[Path]:
    """Dependency jars sbt resolved, read from target/dependency-jars.txt.

    Written by the build (`sbt "export runtime:fullClasspath"` or the equivalent
    task) because the Coursier cache layout is platform-specific and globbing it
    is how the old JNA lookup came to work on macOS and fail on Linux.
    """
    if not RUNTIME_JARS_LIST.is_file():
        return []
    return [Path(line.strip()) for line in RUNTIME_JARS_LIST.read_text().splitlines()
            if line.strip() and Path(line.strip()).suffix == ".jar"]


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
    args = ap.parse_args()

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
              f"listed in {RUNTIME_JARS_LIST}. Generate it with the sbt export "
              "task before staging (see .github/workflows/publish.yml).",
              file=sys.stderr)
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

    for jar in list(spark_jars.values()) + [runtime, jna]:
        shutil.copy2(jar, DEST / jar.name)
        print(f"staged {jar.name}  ({jar.stat().st_size / 1024:.0f} KB)")

    total = sum(p.stat().st_size for p in DEST.glob("*.jar"))
    print(f"\n{DEST.relative_to(REPO)}: {len(list(DEST.glob('*.jar')))} JARs, "
          f"{total / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
