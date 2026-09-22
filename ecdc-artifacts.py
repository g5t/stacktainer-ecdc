#!/usr/bin/env python3
"""List what ECDC builds exist on the ESS artifactory.

image.def's grab_ecdc_release looks up one exact revision:

    api/search/artifact?name=<pkg>-<rev>-almalinux9*&repos=ecdc-generic-release

which answers "does this revision exist" but not "which revisions are there".
This queries the same endpoint by package name alone and unpacks the results.

Artifacts are stored as

    <repo>/<package>/<branch>/<build>/<package>-<revision>-<dist>.tar.gz

so the build number gives a reliable ordering -- the search endpoint does not
promise one.

    ./ecdc-artifacts.py                          # packages in the repo
    ./ecdc-artifacts.py event-formation-unit     # newest 20 almalinux9 builds
    ./ecdc-artifacts.py kafka-to-nexus --details # ...with dates and sizes
    ./ecdc-artifacts.py event-formation-unit --all --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ARTIFACTORY = "https://artifactory.esss.lu.se/artifactory"
DEFAULT_REPO = "ecdc-generic-release"
DEFAULT_DIST = "almalinux9"
TIMEOUT = 60

# <repo>/<package>/<branch>/<build>/<filename>
_PATH = re.compile(
    r"/api/storage/(?P<repo>[^/]+)/(?P<package>[^/]+)/(?P<branch>.+)"
    r"/(?P<build>[^/]+)/(?P<filename>[^/]+)$"
)
_EXTENSIONS = (".tar.gz", ".tgz", ".zip")


def parse_uri(uri: str) -> dict | None:
    """Split a storage URI into its parts.

    The filename is <package>[-<revision>]-<dist><ext>, and none of those three
    can be pinned down by a single regex:

      * the revision may contain dashes -- releases are named 2.0.0-rc.5,
        2.4.2-dev.1, 2.0.0-alpha.3-dev.1 alongside the git-hash builds;
      * it may be absent (<package>-<dist>) or empty (<package>--<dist>) in
        older artifacts.

    The distribution is always the final dash-separated field, so split from the
    right and let the revision keep whatever is left.
    """
    m = _PATH.search(uri)
    if not m:
        return None
    d = m.groupdict()
    filename = d.pop("filename")
    for ext in _EXTENSIONS:
        if filename.endswith(ext):
            stem, d["ext"] = filename[: -len(ext)], ext
            break
    else:
        return None
    prefix = d["package"] + "-"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix):]
    revision, sep, dist = rest.rpartition("-")
    if not sep:  # <package>-<dist>, no revision at all
        revision, dist = "", rest
    d["revision"] = revision or "-"
    d["dist"] = dist
    d["uri"] = uri
    d["build_sort"] = _order_key(d["build"])
    return d


def _order_key(build: str):
    """Order builds newest-first.

    Branch builds are numbered (1084, 2042) but tag builds are the version string
    itself (2.0.0, 2.0.0-rc.5, 6.4.0-dev.2), so a plain int() puts every release
    in the same bucket and the listing comes out unordered. Split into digit and
    non-digit runs and compare those, which sorts 2.4.2 above 2.0.0 above 1.3.4
    and keeps 2.0.0-rc.5 beside 2.0.0.
    """
    parts = []
    for chunk in re.findall(r"\d+|\D+", build):
        if chunk.isdigit():
            parts.append((1, int(chunk), ""))
        else:
            parts.append((0, 0, chunk))
    return parts


def fetch(url: str):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        sys.exit(f"artifactory returned {exc.code} for {url}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"could not reach artifactory: {exc}")


def list_packages(repo: str) -> list[str]:
    data = fetch(f"{ARTIFACTORY}/api/storage/{repo}/")
    return sorted(c["uri"].lstrip("/") for c in data.get("children", []) if c.get("folder"))


def search(repo: str, package: str) -> list[dict]:
    data = fetch(f"{ARTIFACTORY}/api/search/artifact?name={package}&repos={repo}")
    out, unparsed = [], 0
    for item in data.get("results", []):
        d = parse_uri(item["uri"])
        if d is None:
            unparsed += 1
            continue
        out.append(d)
    if unparsed:
        print(f"note: {unparsed} result(s) did not match the expected layout",
              file=sys.stderr)
    return out


def add_details(rows: list[dict]) -> None:
    """created/size come from the storage entry -- one request per artifact."""
    def one(row):
        try:
            info = fetch(row["uri"])
            row["created"] = info.get("created", "")[:19].replace("T", " ")
            row["size"] = int(info.get("size", 0))
        except SystemExit:
            row["created"], row["size"] = "?", 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, rows))


def pinned_revisions(def_path="image.def") -> dict[str, str]:
    """Revisions image.def pins, keyed by package name, so they can be marked."""
    try:
        text = open(def_path).read()
        block = text.split("%arguments", 1)[1].split("%post", 1)[0]
    except (OSError, IndexError):
        return {}
    args = {}
    for line in block.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            args[k.strip()] = v.strip().strip('"')
    out = {}
    for key, value in args.items():
        if key.endswith("_name"):
            rev = args.get(key[:-5] + "_revision")
            if rev:
                out[value] = rev
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("package", nargs="?", help="package name; omit to list packages")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--dist", default=DEFAULT_DIST,
                   help=f"distribution filter (default {DEFAULT_DIST}; 'any' for all)")
    p.add_argument("--branch", help="branch filter, e.g. master")
    p.add_argument("-n", type=int, default=20, help="how many to show (default 20)")
    p.add_argument("--all", action="store_true", help="show every build")
    p.add_argument("--releases", action="store_true",
                   help="only tagged releases (revisions like 2.0.0-rc.5), not git-hash builds")
    p.add_argument("--details", action="store_true", help="also fetch date and size")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args()

    if not args.package:
        names = list_packages(args.repo)
        if args.json:
            print(json.dumps(names, indent=2))
        else:
            print(f"{len(names)} package(s) in {args.repo}:")
            for n in names:
                print(f"  {n}")
        return 0

    rows = search(args.repo, args.package)
    if args.dist != "any":
        rows = [r for r in rows if r["dist"] == args.dist]
    if args.branch:
        rows = [r for r in rows if r["branch"] == args.branch]
    if args.releases:
        rows = [r for r in rows if "." in r["revision"]]
    rows.sort(key=lambda r: r["build_sort"], reverse=True)

    total = len(rows)
    if not args.all:
        rows = rows[: args.n]
    if args.details:
        add_details(rows)

    pinned = pinned_revisions().get(args.package)
    for r in rows:
        r["pinned_in_image_def"] = (r["revision"] == pinned)

    if args.json:
        for r in rows:
            r.pop("build_sort", None)
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print(f"no {args.dist} artifacts for {args.package} in {args.repo}")
        return 1

    w_build = max(5, *(len(r["build"]) for r in rows))
    w_rev = max(8, *(len(r["revision"]) for r in rows))
    w_branch = max(6, *(len(r["branch"]) for r in rows))
    head = f"{'BUILD':>{w_build}}  {'REVISION':<{w_rev}}  {'BRANCH':<{w_branch}}  {'DIST':<10}"
    if args.details:
        head += f"  {'CREATED':<19} {'SIZE':>9}"
    print(head)
    for r in rows:
        line = (f"{r['build']:>{w_build}}  {r['revision']:<{w_rev}}  "
                f"{r['branch']:<{w_branch}}  {r['dist']:<10}")
        if args.details:
            line += f"  {r.get('created',''):<19} {r.get('size',0)/2**20:>8.1f}M"
        if r["pinned_in_image_def"]:
            line += "  <- pinned in image.def"
        print(line)
    shown = len(rows)
    print(f"\n{shown} of {total} {args.dist} build(s)"
          + ("" if args.all or shown == total else f"; --all for the rest"))
    if pinned and not any(r["pinned_in_image_def"] for r in rows):
        print(f"note: image.def pins {pinned}, which is older than the {shown} shown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
