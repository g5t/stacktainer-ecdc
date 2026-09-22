#!/usr/bin/env python3
"""Build, verify and publish a stacktainer-ecdc image.

The GitHub workflow cannot do this while the ESS artifactory is unreachable from
GitHub runners. This runs the same steps locally, and refuses to publish an image
it cannot show to be sound.

That refusal is the point. image.def fetches the ECDC binaries with no `set -e`
in %post and with %test commented out, so a failed fetch used to yield an image
that built cleanly and contained nothing. ecdc-fetch.py now fails loudly on its
own, but the checks here are what stand between a bad build and ghcr.

    ./build-image.py 6.0                     # pre-flight, build, verify
    ./build-image.py 6.0 --push              # ...and publish
    ./build-image.py 6.0 --check-only        # pre-flight only
    ./build-image.py 5.1 --expect-identical  # a patch release: binaries must not move
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
REGISTRY = "oras://ghcr.io/g5t/stacktainer/ecdc"
ARTIFACTORY = "https://artifactory.esss.lu.se/artifactory"

# Files whose absence means the artifactory fetch silently did nothing.
MUST_HAVE = [
    "/event-formation-unit/bin/bifrost",
    "/event-formation-unit/util/efushell/efustats.py",
    "/kafka-to-nexus/bin/kafka-to-nexus",
]
# Binaries that must resolve by name -- this is what the 5.1 PATH fix was about.
MUST_RESOLVE = ["bifrost", "kafka-to-nexus", "efustats.py"]


def run(*cmd, capture=True, check=False, **kw):
    return subprocess.run(cmd, text=True, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None, **kw)


def ok(msg):
    print(f"  \033[32mok\033[0m   {msg}")


def bad(msg):
    print(f"  \033[31mFAIL\033[0m {msg}")


def note(msg):
    print(f"  \033[33m--\033[0m   {msg}")


def build_arguments() -> dict[str, str]:
    """The %arguments block, so pins are read from one place."""
    text = (REPO / "image.def").read_text()
    block = text.split("%arguments", 1)[1].split("%files", 1)[0]
    out = {}
    for line in block.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"')
    return out


def pinned_packages(args_map: dict[str, str]) -> list[tuple[str, str]]:
    """(package, revision) for every <x>_name / <x>_revision pair."""
    out = []
    for key, value in args_map.items():
        if key.endswith("_name"):
            rev = args_map.get(key[:-5] + "_revision")
            if rev:
                out.append((value, rev))
    return out


def previous_tag() -> str | None:
    tags = run("git", "-C", str(REPO), "tag", "--sort=-v:refname").stdout.split()
    return tags[0] if tags else None


def preflight(args) -> bool:
    print(f"Pre-flight for {args.version}")
    good = True

    status = run("git", "-C", str(REPO), "status", "--porcelain",
                 "--untracked-files=no").stdout.strip()
    if status:
        bad(f"tracked files are modified:\n{status}")
        good = False
    else:
        ok("no modified tracked files")

    prev = args.base_tag or previous_tag()
    if prev:
        diff = run("git", "-C", str(REPO), "diff", f"{prev}..HEAD").stdout
        changed = [l for l in diff.splitlines()
                   if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        note(f"{prev}..HEAD touches {len(changed)} line(s)")
        if args.expect_only:
            matched = all(args.expect_only in l for l in changed)
            if changed and matched:
                ok(f"every changed line mentions {args.expect_only!r}")
            else:
                bad(f"changed lines are not all {args.expect_only!r}")
                good = False

    a = build_arguments()
    dist = a.get("ecdc_dist", "almalinux9")
    for package, revision in pinned_packages(a):
        r = run(sys.executable, str(REPO / "ecdc-fetch.py"),
                "--dist", dist, "--resolve-only", package, revision)
        if r.returncode == 0:
            built = next((l.strip() for l in r.stdout.splitlines() if "branch" in l), "")
            ok(f"{package} {revision} resolves  [{built}]")
        else:
            bad(f"{package} {revision} does not resolve:\n{r.stdout.rstrip()}")
            good = False

    # The config clone is a separate pin and drifts if it is not a tag.
    ref, remote = a.get("efu_ref"), a.get("efu_remote")
    if ref and remote:
        r = run("git", "ls-remote", "--tags", remote, ref)
        if r.returncode == 0 and f"refs/tags/{ref}" in r.stdout:
            sha = r.stdout.split()[0][:8]
            ok(f"config clone ref {ref} exists on the remote ({sha})")
            if ref != a.get("efu_revision"):
                note(f"efu_ref {ref} != efu_revision {a.get('efu_revision')}; "
                     "configs and binaries will come from different commits")
        else:
            bad(f"config clone ref {ref} not found at {remote}")
            good = False

    if run("which", "apptainer").returncode != 0:
        bad("apptainer not found")
        good = False
    else:
        ok(run("apptainer", "--version").stdout.strip())

    return good


def build(args, sif: Path) -> bool:
    print(f"\nBuilding {sif.name}")
    if sif.exists():
        if not args.force:
            bad(f"{sif.name} exists; --force to rebuild, or verify it as-is")
            return False
        sif.unlink()
    cmd = ["apptainer", "build"]
    if args.fakeroot:
        cmd.append("--fakeroot")
    cmd += [str(sif), str(REPO / "image.def")]
    print("  $ " + " ".join(cmd))
    if run(*cmd, capture=False).returncode != 0:
        bad("build failed")
        return False
    ok(f"built {sif.name} ({sif.stat().st_size / 2**20:.0f} MiB)")
    return True


def verify(args, sif: Path) -> bool:
    print("\nVerifying")
    good = True
    a = build_arguments()

    # The PATH fix of 5.1. Read the resolved PATH, not the def text: {{ efu_name }}
    # expands complete with the quotes from %arguments, so the source reads
    # /"event-formation-unit"/util/efushell while the effective entry has none.
    path = run("apptainer", "exec", str(sif), "sh", "-c", 'printf "%s" "$PATH"').stdout
    efushell = [e for e in path.split(":") if "efushell" in e]
    if not efushell:
        bad("no efushell entry on PATH")
        good = False
    elif all(e.startswith("/") for e in efushell):
        ok(f"efushell on PATH and absolute: {efushell[0]}")
    else:
        bad(f"efushell PATH entry is relative: {efushell!r}")
        good = False

    for p in MUST_HAVE:
        if run("apptainer", "exec", str(sif), "test", "-s", p).returncode == 0:
            ok(f"present: {p}")
        else:
            bad(f"MISSING or empty: {p} -- the artifactory fetch did not work")
            good = False

    r = run("apptainer", "exec", str(sif), "sh", "-lc",
            "command -v " + " ".join(MUST_RESOLVE))
    if len(r.stdout.split()) >= len(MUST_RESOLVE):
        ok(f"{', '.join(MUST_RESOLVE)} all resolve on PATH")
    else:
        bad(f"not all resolve; found {r.stdout.split()}")
        good = False

    # What the image says it is, against what was pinned.
    for var, key in (("EFU_VERSION", "efu_revision"), ("FILEWRITER_VERSION", "writer_revision")):
        got = run("apptainer", "exec", str(sif), "sh", "-c", f'printf "%s" "${var}"').stdout.strip()
        want = a.get(key, "")
        if got == want:
            ok(f"{var}={got}")
        else:
            bad(f"{var} is {got!r}, expected {want!r}")
            good = False

    # Binaries self-report a version when built from a tag; informational, because
    # builds from a branch tip report 'dev' instead.
    ver = run("apptainer", "exec", str(sif), "bifrost", "--version").stdout.strip()
    if ver:
        line = ver.splitlines()[0][:70]
        (ok if a.get("efu_revision", "") in ver else note)(f"bifrost --version: {line}")

    # The JSON module configs, which come from the git clone rather than the tarball.
    n = run("apptainer", "exec", str(sif), "sh", "-c",
            "find /event-formation-unit/configs -name '*.json' | wc -l").stdout.strip()
    (ok if n.isdigit() and int(n) > 0 else bad)(f"{n} module config(s) present")
    if not (n.isdigit() and int(n) > 0):
        good = False

    if args.compare:
        good &= compare_with_previous(args, sif)
    return good


def compare_with_previous(args, sif: Path) -> bool:
    """Diff the binary trees against the last published image.

    Informational unless --expect-identical. For a version bump the manifests are
    *supposed* to differ; even for a patch release they can, because the module
    configs come from a git clone rather than the pinned tarball.
    """
    prev = args.compare_with or (previous_tag() or "").lstrip("v")
    if not prev:
        note("no previous tag to compare against")
        return True
    old = REPO / f"ecdc_{prev}.sif"
    print(f"  comparing against {prev} ...")
    if not old.exists():
        if run("apptainer", "pull", str(old), f"{REGISTRY}:{prev}", capture=False).returncode != 0:
            note(f"could not pull {prev}; skipping comparison")
            return True
    manifests = {}
    for label, image in ((prev, old), (args.version, sif)):
        out = run("apptainer", "exec", str(image), "sh", "-c",
                  "find /event-formation-unit /kafka-to-nexus -type f | sort").stdout
        manifests[label] = set(out.split())
    only_old = manifests[prev] - manifests[args.version]
    only_new = manifests[args.version] - manifests[prev]
    if not only_old and not only_new:
        ok(f"file manifests identical to {prev} ({len(manifests[args.version])} files)")
        return True
    msg = (f"manifests differ from {prev}: "
           f"{len(only_old)} removed, {len(only_new)} added")
    if args.expect_identical:
        bad(msg)
        for p in sorted(only_old)[:5] + sorted(only_new)[:5]:
            print(f"       {p}")
        return False
    note(msg + " (expected for a version bump)")
    for p in sorted(only_new)[:5]:
        print(f"       + {p}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", help="image version to build and publish, e.g. 6.0")
    p.add_argument("--base-tag", help="git tag to diff against (default: newest)")
    p.add_argument("--expect-only", metavar="TEXT",
                   help="require every changed line since --base-tag to contain TEXT, "
                        "e.g. 'export PATH' for a PATH-only patch release")
    p.add_argument("--expect-identical", action="store_true",
                   help="require the binary trees to match the previous image")
    p.add_argument("--compare-with", metavar="VERSION",
                   help="published version to diff against (default: newest tag)")
    p.add_argument("--push", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--force", action="store_true", help="rebuild if the sif exists")
    p.add_argument("--no-fakeroot", dest="fakeroot", action="store_false")
    p.add_argument("--no-compare", dest="compare", action="store_false")
    args = p.parse_args()

    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", args.version):
        return print(f"version {args.version!r} should look like 6.0") or 2

    sif = REPO / f"ecdc_{args.version}.sif"
    target = f"{REGISTRY}:{args.version}"

    if not preflight(args):
        print("\nPre-flight failed; not building.", file=sys.stderr)
        return 1
    if args.check_only:
        print("\nPre-flight passed.")
        return 0
    if not build(args, sif):
        return 1
    if not verify(args, sif):
        print("\nVerification failed; NOT pushing.", file=sys.stderr)
        return 1

    print("\nVerification passed.")
    if args.push:
        print(f"\nPushing to {target}")
        print("  (needs `apptainer remote login --username <you> oras://ghcr.io`"
              " with a PAT carrying write:packages)")
        if run("apptainer", "push", str(sif), target, capture=False).returncode != 0:
            bad("push failed")
            return 1
        ok(f"pushed {target}")
        return 0
    print(f"Not pushing. When ready:\n  apptainer push {sif} {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
