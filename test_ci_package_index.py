"""Exercise the real post-publish CI loop; only registry/signing tools are fake."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
ORIGINAL_DIGEST = "sha256:" + "1" * 64
REPOSITORY = "registry.invalid/provider-family-aws"
VERSION = "v2.4.0-dd.test"


def package_index():
    # Child digests pin the complete child manifests, including their config refs.
    return {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "artifactType": "application/vnd.crossplane.package.v1",
        "annotations": {"existing": "keep/é", "target": "previous-group"},
        "subject": {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "size": 90, "digest": "sha256:" + "9" * 64},
        "manifests": [
            {"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
             "size": 101, "digest": "sha256:" + "a" * 64,
             "platform": {"os": "linux", "architecture": "amd64"},
             "annotations": {"child": "keep"}, "urls": ["https://example.invalid/child"]},
            {"mediaType": "application/vnd.crossplane.package.v1+json",
             "size": 102, "digest": "sha256:" + "b" * 64,
             "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
            {"mediaType": "application/vnd.oci.image.manifest.v1+json",
             "size": 103, "digest": "sha256:" + "c" * 64,
             "platform": {"os": "unknown", "architecture": "unknown"},
             "annotations": {"extra": "retain this descriptor too"}},
        ],
    }


# This boundary implements only the commands used by the CI step. It stores the
# actual submitted bytes, never constructs or annotates a package index itself.
FAKE_TOOL = r'''
import copy, hashlib, json, os, pathlib, sys
state_path = pathlib.Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with pathlib.Path(os.environ["FAKE_CALLS"]).open("a") as log:
    log.write(json.dumps([tool] + args) + "\n")
ref = args[1] if tool == "ddsign" or args[0] != "edit" else args[2]
repository = ref.split("@")[0].rsplit(":", 1)[0] if ":" in ref.split("@")[0].rsplit("/", 1)[-1] else ref.split("@")[0]
assert repository.startswith("registry.invalid/"), "only fake registry references allowed"
package = state.setdefault("packages", {}).setdefault(repository, {"resolves": 0})
tagged = repository + ":" + state["version"]
original = "sha256:" + "1" * 64

def save():
    state_path.write_text(json.dumps(state))

def fail(stage):
    save()
    if state.get("fail_at") == stage:
        sys.exit("fake " + stage + " failure")

if tool == "crane" and args[0] == "digest":
    assert args == ["digest", tagged], args
    package["resolves"] += 1
    stage = {1: "resolve-original", 2: "resolve-final", 3: "resolve-before-sign"}[package["resolves"]]
    fail(stage)
    digest = package.get("final_digest", original)
    if state.get("mode") == "digest-mismatch" and package["resolves"] == 2:
        digest = "sha256:" + "d" * 64
    if state.get("mode") == "tag-race" and package["resolves"] == 3:
        digest = original
    if state.get("mode") == "malformed-digest":
        digest = "not-a-digest"
    print(digest)
elif tool == "crane" and args[0] == "manifest":
    assert len(args) == 2 and "@sha256:" in ref, args
    if ref == repository + "@" + original:
        fail("manifest-original")
        sys.stdout.write(state.get("raw_original") or json.dumps(state["original"]))
    else:
        assert ref == repository + "@" + package["final_digest"], args
        fail("manifest-final")
        raw = package["edited_raw"]
        changed = json.loads(raw)
        mode = state.get("mode")
        if mode == "final-target-missing":
            changed["annotations"].pop("target")
        elif mode == "final-child-changed":
            changed["manifests"][0]["digest"] = "sha256:" + "e" * 64
        elif mode == "final-extra-child":
            changed["manifests"].append(copy.deepcopy(changed["manifests"][0]))
        elif mode == "final-subject-changed":
            changed["subject"]["digest"] = "sha256:" + "f" * 64
        elif mode == "final-schema-changed":
            changed["schemaVersion"] = 1
        elif mode == "final-child-annotation-changed":
            changed["manifests"][0]["annotations"]["child"] = "changed"
        sys.stdout.write(json.dumps(changed))
elif tool == "crane" and args[:2] == ["edit", "manifest"]:
    assert args == ["edit", "manifest", repository + "@" + original,
                    "--tag", tagged, "--media-type", state["index_media_type"]], args
    fail("edit")
    raw = sys.stdin.buffer.read()
    package["edited_raw"] = raw.decode()
    package["final_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    save()
    fail("edit-after-write")
elif tool == "ddsign":
    assert len(args) == 2 and args[0] == "sign" and "@sha256:" in ref, args
    fail("sign")
    state.setdefault("signed", []).append(ref)
    save()
else:
    sys.exit("unexpected fake command: " + repr([tool] + args))
'''


def ci_package_index_step():
    # Execute the real step immediately after publish, including tool checks.
    # Exclude the provider build and its /tmp Dockerfile: never invoke make.
    config = (Path(__file__).parent / ".gitlab-ci.yml").read_text()
    script = textwrap.dedent(config.split("    - |\n", 1)[1])
    return script.split("make build.all publish", 1)[1].split("\n", 1)[1]


class PackageIndexCITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-package-index-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.state_path = self.root / "state.json"
        self.calls_path = self.root / "calls.jsonl"
        for tool in ("crane", "ddsign"):
            path = self.bin / tool
            path.write_text("#!" + sys.executable + "\n" + FAKE_TOOL)
            path.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)

    def run_ci(self, original=None, *, packages="config", **settings):
        state = {"original": package_index() if original is None else original,
                 "version": VERSION, "index_media_type": OCI_INDEX, **settings}
        self.state_path.write_text(json.dumps(state))
        self.calls_path.write_text("")
        env = {**os.environ, "PATH": str(self.bin),
               "FAKE_STATE": str(self.state_path), "FAKE_CALLS": str(self.calls_path),
               "REG": "registry.invalid", "VERSION": VERSION, "SUBPACKAGES": packages,
               "TMPDIR": str(self.scratch), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(["/bin/bash", "-euo", "pipefail"], input=ci_package_index_step(),
                                text=True, capture_output=True, env=env,
                                cwd=self.root, timeout=15)
        state = json.loads(self.state_path.read_text())
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        self.assertEqual(list(self.scratch.iterdir()), [], "helper leaked temporary files")
        return result, state, calls

    def assert_not_signed(self, calls):
        self.assertFalse(any(call[0] == "ddsign" for call in calls), calls)

    def test_sets_only_root_target_and_signs_exact_submitted_index(self):
        result, state, calls = self.run_ci()
        self.assertEqual(result.returncode, 0, result.stderr)
        package = state["packages"][REPOSITORY]
        self.assertIn("edited_raw", package, "published index was never annotated before signing")
        expected = package_index()
        expected["annotations"]["target"] = "staging"
        self.assertEqual(json.loads(package["edited_raw"]), expected)
        digest = "sha256:" + hashlib.sha256(package["edited_raw"].encode()).hexdigest()
        self.assertNotEqual(digest, ORIGINAL_DIGEST)
        immutable = REPOSITORY + "@" + digest
        self.assertEqual(state["signed"], [immutable])
        self.assertEqual(calls, [
            ["crane", "digest", REPOSITORY + ":" + VERSION],
            ["crane", "manifest", REPOSITORY + "@" + ORIGINAL_DIGEST],
            ["crane", "edit", "manifest", REPOSITORY + "@" + ORIGINAL_DIGEST,
             "--tag", REPOSITORY + ":" + VERSION, "--media-type", OCI_INDEX],
            ["crane", "digest", REPOSITORY + ":" + VERSION],
            ["crane", "manifest", immutable],
            ["crane", "digest", REPOSITORY + ":" + VERSION],
            ["ddsign", "sign", immutable],
        ])

    def test_adds_root_annotations_when_absent(self):
        original = package_index()
        del original["annotations"]
        result, state, _ = self.run_ci(original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("edited_raw", state["packages"][REPOSITORY])
        amended = json.loads(state["packages"][REPOSITORY]["edited_raw"])
        self.assertEqual(amended["annotations"], {"target": "staging"})
        del amended["annotations"]
        self.assertEqual(amended, original)

    def test_real_ci_loop_annotates_family_s3_and_iam(self):
        result, state, _ = self.run_ci(packages="config s3 iam")
        self.assertEqual(result.returncode, 0, result.stderr)
        repositories = ["registry.invalid/provider-family-aws", "registry.invalid/provider-aws-s3",
                        "registry.invalid/provider-aws-iam"]
        self.assertEqual(set(state["packages"]), set(repositories))
        for package in state["packages"].values():
            self.assertIn("final_digest", package, "each package must be annotated before signing")
        self.assertEqual(state["signed"], [repo + "@" + state["packages"][repo]["final_digest"]
                                          for repo in repositories])
        for package in state["packages"].values():
            self.assertEqual(json.loads(package["edited_raw"])["annotations"]["target"], "staging")

    def test_preserves_extra_descriptor_without_platform(self):
        original = package_index()
        del original["manifests"][2]["platform"]
        result, state, _ = self.run_ci(original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("edited_raw", state["packages"][REPOSITORY])
        amended = json.loads(state["packages"][REPOSITORY]["edited_raw"])
        original["annotations"]["target"] = "staging"
        self.assertEqual(amended, original)

    def test_invalid_indexes_fail_before_edit_or_sign(self):
        cases = [("not-object", []), ("empty-object", {})]
        for key, value in [("schemaVersion", 1), ("schemaVersion", 2.0),
                           ("mediaType", "application/vnd.docker.distribution.manifest.list.v2+json"),
                           ("manifests", []), ("manifests", {}), ("annotations", []),
                           ("annotations", {"existing": 5}), ("annotations", None),
                           ("annotations", {"bad": "nul\u0000value"})]:
            index = package_index()
            index[key] = value
            cases.append((key + repr(value), index))
        for architecture in ("amd64", "arm64"):
            index = package_index()
            index["manifests"] = [c for c in index["manifests"]
                                  if c["platform"]["architecture"] != architecture]
            cases.append(("missing-" + architecture, index))
        for key, value in [("digest", "sha256:short"), ("digest", None),
                           ("size", -1), ("size", True), ("size", "101"),
                           ("mediaType", ""), ("mediaType", 1), ("mediaType", "not-a-type"),
                           ("platform", []), ("platform", {}),
                           ("platform", {"os": "linux", "architecture": ["amd64"]})]:
            index = package_index()
            index["manifests"][0][key] = value
            cases.append(("child-" + key + repr(value), index))
        for key in ("digest", "size", "mediaType"):
            index = package_index()
            del index["manifests"][0][key]
            cases.append(("child-missing-" + key, index))
        index = package_index()
        index["manifests"].append(None)
        cases.append(("non-object-child", index))
        for name, original in cases:
            with self.subTest(name=name):
                result, _, calls = self.run_ci(original)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(any(call[:2] == ["crane", "edit"] for call in calls), calls)
                self.assert_not_signed(calls)

    def test_invalid_json_fails_before_edit_or_sign(self):
        duplicate = json.dumps(package_index()).replace('"schemaVersion": 2',
                                                       '"schemaVersion": 1, "schemaVersion": 2')
        nonfinite = json.dumps({**package_index(), "opaque": float("nan")})
        for raw in ("{bad json", duplicate, nonfinite):
            with self.subTest(raw=raw):
                result, _, calls = self.run_ci(raw_original=raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[:2] == ["crane", "edit"] for call in calls), calls)
                self.assert_not_signed(calls)

    def test_bad_digest_or_tag_race_never_signs(self):
        for mode in ("malformed-digest", "digest-mismatch", "tag-race"):
            with self.subTest(mode=mode):
                result, _, calls = self.run_ci(mode=mode)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assert_not_signed(calls)

    def test_final_index_must_match_every_original_field_except_target(self):
        for mode in ("final-target-missing", "final-child-changed", "final-extra-child",
                     "final-subject-changed", "final-schema-changed", "final-child-annotation-changed"):
            with self.subTest(mode=mode):
                result, _, calls = self.run_ci(mode=mode)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assert_not_signed(calls)

    def test_missing_tool_preflight_prevents_registry_calls(self):
        for tool in ("python3", "crane", "ddsign"):
            with self.subTest(tool=tool):
                path = self.bin / tool
                disabled = self.bin / (tool + ".disabled")
                path.rename(disabled)
                try:
                    result, _, calls = self.run_ci()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [], "missing tool must fail before registry access")
                finally:
                    disabled.rename(path)

    def test_external_tool_failures_stop_the_ci_loop(self):
        for stage, last_command in [
                ("resolve-original", ["crane", "digest"]),
                ("manifest-original", ["crane", "manifest"]),
                ("edit", ["crane", "edit"]),
                ("edit-after-write", ["crane", "edit"]),
                ("resolve-final", ["crane", "digest"]),
                ("manifest-final", ["crane", "manifest"]),
                ("resolve-before-sign", ["crane", "digest"]),
                ("sign", ["ddsign", "sign"])]:
            with self.subTest(stage=stage):
                result, state, calls = self.run_ci(fail_at=stage, packages="config s3 iam")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("fake " + stage + " failure", result.stderr)
                self.assertEqual(set(state["packages"]), {REPOSITORY})
                self.assertEqual(calls[-1][:2], last_command, "continued after tool failure")
                if stage == "sign":
                    self.assertEqual(calls[-1][0:2], ["ddsign", "sign"])
                    self.assertNotIn("signed", state)
                else:
                    self.assert_not_signed(calls)


if __name__ == "__main__":
    unittest.main()
