#!/usr/bin/env python3
"""Fetch and unpack one ECDC release artifact from the ESS artifactory.

Replaces the grab_ecdc_release shell function that used to live in image.def.
Copied into the image by %files and run from %post.

What it does that the shell function did not:

  * fails when the search matches nothing. The old function piped
    `jq .results[-1].uri` -- null for an empty result -- into curl and then into
    tar, and with no `set -e` in %post the build carried on and produced an image
    with no binaries in it. Nothing downstream noticed.
  * fails when the search is ambiguous, instead of silently taking the last hit.
  * verifies the sha256 artifactory reports, which the old function left as a
    "verify one of info.checksums..." comment.
  * prints what it resolved, so the build log records which build went in.

A version argument is whatever appears between the package name and the
distribution in the filename: a git revision (6418ea63) or a release tag
(3.1.0, 2.0.0-rc.5, 6.4.7-dev) alike.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

ARTIFACTORY = "https://artifactory.esss.lu.se/artifactory"
DEFAULT_REPO = "ecdc-generic-release"
DEFAULT_DIST = "almalinux9"
TIMEOUT = 300


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        return json.load(response)


def resolve(package: str, version: str, repo: str, dist: str) -> dict:
    """The single storage entry for this package/version/dist, or exit."""
    query = (f"{ARTIFACTORY}/api/search/artifact"
             f"?name={package}-{version}-{dist}&repos={repo}")
    try:
        results = get_json(query).get("results", [])
    except urllib.error.HTTPError as exc:
        sys.exit(f"ecdc-fetch: artifactory returned {exc.code} for {query}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"ecdc-fetch: cannot reach artifactory: {exc}")

    # The search is a substring match, so 3.1.0 also finds 3.1.0-rc.1; keep only
    # the exact filename this package/version/dist names.
    wanted = f"{package}-{version}-{dist}.tar.gz"
    exact = [r for r in results if r["uri"].rsplit("/", 1)[-1] == wanted]

    if not exact:
        near = sorted({r["uri"].rsplit("/", 1)[-1] for r in results})[:8]
        sys.exit(
            f"ecdc-fetch: no {wanted} in {repo}.\n"
            + (f"  did you mean one of:\n" + "".join(f"    {n}\n" for n in near)
               if near else "  the search matched nothing at all.\n")
            + "  list what exists with: ./ecdc-artifacts.py " + package
        )
    if len(exact) > 1:
        sys.exit(f"ecdc-fetch: {len(exact)} artifacts named {wanted}; refusing to guess:\n"
                 + "".join(f"    {r['uri']}\n" for r in exact))

    return get_json(exact[0]["uri"])


def fetch(info: dict, verify: bool) -> bytes:
    url = info["downloadUri"]
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        blob = response.read()

    declared = int(info.get("size", 0))
    if declared and len(blob) != declared:
        sys.exit(f"ecdc-fetch: size mismatch: got {len(blob)}, expected {declared}")

    checksums = info.get("checksums", {})
    if verify:
        sha256 = checksums.get("sha256")
        if not sha256:
            sys.exit("ecdc-fetch: artifactory reported no sha256; pass --no-verify "
                     "to accept the download unchecked")
        actual = hashlib.sha256(blob).hexdigest()
        if actual != sha256:
            sys.exit(f"ecdc-fetch: sha256 mismatch\n  expected {sha256}\n  got      {actual}")
        print(f"  sha256 ok {sha256}")
    elif checksums.get("sha256"):
        print(f"  sha256 NOT verified ({checksums['sha256']})")
    return blob


def extract(blob: bytes, dest: Path) -> int:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            # The archives unpack to <package>/... ; nothing should escape dest.
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                sys.exit(f"ecdc-fetch: refusing path outside {dest}: {m.name}")
        try:
            tar.extractall(dest, filter="tar")  # py>=3.12
        except TypeError:
            tar.extractall(dest)
    return len(members)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("package", help="e.g. event-formation-unit")
    p.add_argument("version", help="git revision or release tag, e.g. 6418ea63 or 3.1.0")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--dist", default=DEFAULT_DIST)
    p.add_argument("--dest", default="/", type=Path, help="where to unpack (default /)")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the sha256 check")
    p.add_argument("--resolve-only", action="store_true",
                   help="report what would be fetched, download nothing")
    args = p.parse_args()

    info = resolve(args.package, args.version, args.repo, args.dist)
    path = info["path"].lstrip("/")
    branch, build = path.split("/")[1:3] if path.count("/") >= 3 else ("?", "?")
    print(f"ecdc-fetch: {args.package} {args.version} ({args.dist})")
    print(f"  branch {branch}  build {build}  created {info.get('created','?')[:19]}")
    print(f"  {info['downloadUri']}")
    if args.resolve_only:
        return 0

    blob = fetch(info, args.verify)
    n = extract(blob, args.dest)
    print(f"  unpacked {n} entries into {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
