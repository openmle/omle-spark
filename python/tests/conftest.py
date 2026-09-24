r"""Pytest fixtures for the omle-spark Python integration tests.

Requires the jars staged into python/omle_spark/jars/:

    sbt +package
    sbt 'set Compile / fullClasspath / exportJars := true' \
        'export Compile / dependencyClasspath' \
      | tr ':' '\n' | grep '\.jar$' > target/dependency-jars.txt
    python scripts/stage_jars.py

The omle_runtime Python package is optional and only gates the native-match
tests. Everything is skipped, with the reason, when a jar is missing.
"""

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Everything the JVM needs comes from python/omle_spark/jars/ — the directory
# stage_jars.py fills and the wheel ships. Before the repository split these
# paths reached sideways into an omle-runtime checkout for the runtime jar and
# its freshly built native library, and into the Maven/Coursier caches for JNA.
# None of that exists here, and the stale reach was silent: _RUNTIME_JAR became
# None and got filtered straight out of spark.jars, surfacing only once a test
# ran, as ClassNotFoundException: io.github.openmle.runtime.Model.
#
# Reading the staged directory also means the tests exercise the same jars a
# user installs, rather than build output only CI ever assembles.

_HERE       = Path(__file__).parent
_REPO_ROOT  = _HERE.parent.parent              # omle-spark/
_RESOURCES  = _REPO_ROOT / "src" / "test" / "resources"
_PKG_JARS   = _HERE.parent / "omle_spark" / "jars"

_STAGE_HINT = (
    "Stage the jars first: sbt +package, then python scripts/stage_jars.py "
    "(see README.md)."
)


def _staged(pattern):
    """Newest staged jar matching `pattern`, or None.

    Newest by mtime rather than by name: versions come from the git tag, so the
    directory can hold jars from earlier builds and version strings do not sort
    lexically (0.10.0 < 0.2.0).
    """
    if not _PKG_JARS.is_dir():
        return None
    jars = [
        p for p in _PKG_JARS.glob(pattern)
        if not p.name.endswith(("-sources.jar", "-javadoc.jar"))
    ]
    return max(jars, key=lambda p: p.stat().st_mtime) if jars else None


_RUNTIME_JAR = _staged("omle-runtime-*.jar")
# The [0-9] keeps this off jna-platform, which is a different artifact and does
# not contain com.sun.jna.Library.
_JNA_JAR = _staged("jna-[0-9]*.jar")

def _pyspark_scala_version():
    """Scala binary version of the installed PySpark, e.g. "2.12" or "2.13".

    PyPI ships Spark 3.x built with Scala 2.12 and Spark 4.x with Scala 2.13,
    and the two are binary-incompatible. Reading it from the bundled jar name
    is exact, where inferring it from the PySpark version number would be a
    guess that silently rots when upstream changes its build.
    """
    try:
        import pyspark
    except ImportError:
        return None
    jars = Path(pyspark.__file__).parent / "jars"
    for jar in jars.glob("spark-core_*.jar"):
        # spark-core_2.13-4.0.2.jar -> 2.13
        return jar.name.split("_", 1)[1].split("-", 1)[0]
    return None


def _find_spark_jar():
    """The omle-spark jar matching the running PySpark, and why if there isn't one.

    Returns (jar_or_None, reason_or_None). Picking the wrong one here is worse
    than finding nothing: a Scala 2.12 jar on a 2.13 Spark loads fine and then
    dies inside transform() with a NoSuchMethodError naming an internal Scala
    runtime method, which points nowhere near the actual mismatch.
    """
    jars = [
        p for p in (_PKG_JARS.glob("omle-spark_*.jar") if _PKG_JARS.is_dir() else [])
        if not p.name.endswith(("-javadoc.jar", "-sources.jar"))
    ]
    if not jars:
        return None, f"no omle-spark JAR staged in {_PKG_JARS}. {_STAGE_HINT}"

    want = _pyspark_scala_version()
    if want is None:
        return jars[0], None

    # Newest by mtime: versions come from the git tag now, so target/ commonly
    # holds jars from earlier builds and glob order would pick between them at
    # random.
    matched = sorted(
        (p for p in jars if f"_{want}-" in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matched:
        have = ", ".join(sorted(p.name for p in jars))
        return None, (
            f"no omle-spark JAR for Scala {want}, which this PySpark "
            f"({'.'.join(str(v) for v in _pyspark_version())}) is built with. "
            f"Found: {have}. {_STAGE_HINT}"
        )
    return matched[0], None


def _pyspark_version():
    try:
        import pyspark
        return tuple(pyspark.__version__.split("."))
    except Exception:
        return ("unknown",)


_JAR_PATH, _JAR_SKIP_REASON = _find_spark_jar()

REGR_MODEL_PATH  = _RESOURCES / "test_model_2f.omle"
CLASS_MODEL_PATH = _RESOURCES / "test_model_3class.omle"

# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

_NO_JAR = _JAR_PATH is None or not _JAR_PATH.exists()

# The runtime jar and JNA matter just as much, and used to be dropped from
# spark.jars by a bare `if j` when discovery returned None. That turned a
# staging problem into a Py4JError deep inside transform(), naming a missing
# class rather than a missing jar. Check them here, where the reason is known.
if not _NO_JAR:
    _missing = [
        name for name, jar in (("omle-runtime", _RUNTIME_JAR), ("JNA", _JNA_JAR))
        if jar is None
    ]
    if _missing:
        _NO_JAR = True
        _JAR_SKIP_REASON = (
            f"staged jars incomplete — missing {', '.join(_missing)} "
            f"in {_PKG_JARS}. {_STAGE_HINT}"
        )

skip_no_jar = pytest.mark.skipif(
    _NO_JAR,
    reason=_JAR_SKIP_REASON or "omle-spark JAR not found",
)

# ---------------------------------------------------------------------------
# SparkSession
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def spark():
    """Local SparkSession with the omle-spark JAR on the classpath."""
    if _NO_JAR:
        pytest.skip(_JAR_SKIP_REASON or "omle-spark JAR not found")

    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder
        .master("local[2]")
        .appName("omle-spark-python-tests")
        # No `if j` filter: the skip above guarantees all three exist, and
        # quietly dropping one is what made a staging fault look like a
        # missing class.
        .config("spark.jars", ",".join(str(j) for j in [_JAR_PATH, _RUNTIME_JAR, _JNA_JAR]))
        # No -Djna.library.path: the omle-runtime jar carries a native library
        # per platform at JNA's resource paths, so JNA extracts the right one
        # itself. build.sbt dropped the same setting for the Scala tests.
        .config("spark.driver.extraJavaOptions",
                " --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"
                " --add-opens=java.base/java.nio=ALL-UNNAMED"
                " --add-opens=java.base/java.lang=ALL-UNNAMED"
                " --add-opens=java.base/java.lang.invoke=ALL-UNNAMED"
                " --add-opens=java.base/java.util=ALL-UNNAMED")
        .config("spark.executor.extraJavaOptions",
                " --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"
                " --add-opens=java.base/java.nio=ALL-UNNAMED"
                " --add-opens=java.base/java.lang=ALL-UNNAMED"
                " --add-opens=java.base/java.lang.invoke=ALL-UNNAMED"
                " --add-opens=java.base/java.util=ALL-UNNAMED")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
    )
    session = builder.getOrCreate()
    yield session
    session.stop()


# ---------------------------------------------------------------------------
# Model file fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def regr_model_path():
    """Path to the 2-feature regression model (.omle)."""
    assert REGR_MODEL_PATH.exists(), f"Missing fixture: {REGR_MODEL_PATH}"
    return str(REGR_MODEL_PATH)


@pytest.fixture(scope="session")
def class_model_path():
    """Path to the 3-class classifier model (.omle)."""
    assert CLASS_MODEL_PATH.exists(), f"Missing fixture: {CLASS_MODEL_PATH}"
    return str(CLASS_MODEL_PATH)


# ---------------------------------------------------------------------------
# Native omle_runtime models (for match tests)
# ---------------------------------------------------------------------------

def _import_omle_runtime():
    """Import the native package, or skip — it is an optional cross-check.

    These tests compare the Spark output against omle_runtime's own predictions.
    That package is a separate published wheel; it is deliberately not a test
    dependency, because the rest of the suite exercises the JVM path and needs
    nothing native on the Python side. Without the skip a missing import
    surfaces as a fixture ERROR carrying the package's whole module docstring,
    which reads like a failure of the code under test.
    """
    try:
        import omle_runtime as omr
    except ImportError as exc:
        pytest.skip(
            "omle_runtime is not importable, so the Spark output cannot be "
            f"compared against the native runtime ({exc}). "
            "Install it with: pip install omle-runtime"
        )
    return omr


@pytest.fixture(scope="session")
def native_regr_model(regr_model_path):
    """Loaded omle_runtime Model for the regression fixture."""
    return _import_omle_runtime().load(regr_model_path)


@pytest.fixture(scope="session")
def native_class_model(class_model_path):
    """Loaded omle_runtime Model for the 3-class classifier fixture."""
    return _import_omle_runtime().load(class_model_path)
