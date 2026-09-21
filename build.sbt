name := "omle-spark"

// The omle-runtime release this transformer is built and tested against.
// Published jars from 0.1.0-rc9 onward carry the native library for every
// supported platform at JNA's resource paths, so nothing here has to locate
// libomleruntime: JNA extracts it from the classpath. Earlier release
// candidates shipped classes only and will fail with UnsatisfiedLinkError.
val omleRuntimeVersion = "0.1.0-rc9"

// Maven Central rejects a release without homepage, licenses, developers and
// scmInfo. sbt-ci-release reads them from ThisBuild, and supplies the version
// itself from the git tag — do not set `version` here.
inThisBuild(List(
  organization := "io.github.openmle",
  homepage     := Some(url("https://github.com/openmle/omle-spark")),
  licenses     := List(
    "Apache-2.0" -> url("https://www.apache.org/licenses/LICENSE-2.0.txt")),
  developers   := List(
    Developer("openmle", "OMLE", "", url("https://github.com/openmle"))),
  scmInfo      := Some(ScmInfo(
    url("https://github.com/openmle/omle-spark"),
    "scm:git:https://github.com/openmle/omle-spark.git")),
))
// Scala 2.12 and 2.13 are binary-incompatible, and a Spark cluster can only
// load a jar built for the Scala version its own jars were built with — a
// mismatch fails at run time with NoSuchMethodError deep inside the Scala
// runtime, not at load time. Spark 4 dropped Scala 2.12 and Spark 3.5 is the
// last line that still publishes it, so the Spark version follows from the
// Scala version rather than being chosen independently:
//
//   Scala 2.12 -> Spark 3.5.x  (what `pip install pyspark~=3.5` bundles)
//   Scala 2.13 -> Spark 4.x    (what `pip install pyspark>=4` bundles)
//
// `sbt +package` builds both; `sbt ++2.12.18 package` builds just the one.
val scala212 = "2.12.18"
val scala213 = "2.13.14"

scalaVersion       := scala213
crossScalaVersions := Seq(scala212, scala213)

def sparkVersionFor(scalaVer: String): String =
  CrossVersion.partialVersion(scalaVer) match {
    case Some((2, 12)) => "3.5.3"
    case _             => "4.0.2"
  }

libraryDependencies ++= {
  val sparkVersion = sparkVersionFor(scalaVersion.value)
  Seq(
    "org.apache.spark" %% "spark-sql"   % sparkVersion % "provided",
    "org.apache.spark" %% "spark-mllib" % sparkVersion % "provided",
    // Resolved from Maven Central rather than globbed out of a sibling
    // checkout's target/. It brings JNA transitively, but JNA is named
    // explicitly as well: the Python wheel stages it by artifact name (see
    // scripts/stage_jars.py), and an implicit transitive version would change
    // under us without the pin moving.
    "io.github.openmle"  % "omle-runtime" % omleRuntimeVersion,
    "net.java.dev.jna"  % "jna"         % "5.14.0",
    // Test scope re-declares Spark so tests run without a cluster
    "org.apache.spark" %% "spark-sql"   % sparkVersion % Test,
    "org.apache.spark" %% "spark-mllib" % sparkVersion % Test,
    "org.scalatest"    %% "scalatest"   % "3.2.17"     % Test,
  )
}

Test / fork        := true
Test / javaOptions ++= Seq(
  // No -Djna.library.path. It used to point into a sibling omle-runtime
  // checkout's python package; now the omle-runtime jar on the classpath
  // carries the native library for each platform and JNA extracts the right
  // one itself — which is also what a real consumer gets, so the tests
  // exercise the same path rather than a development shortcut.
  //
  // Spark 3.5 requires access to internal JDK classes on Java 17+
  "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
  "--add-opens=java.base/java.nio=ALL-UNNAMED",
  "--add-opens=java.base/java.lang=ALL-UNNAMED",
  "--add-opens=java.base/java.lang.invoke=ALL-UNNAMED",
  "--add-opens=java.base/java.util=ALL-UNNAMED",
)
