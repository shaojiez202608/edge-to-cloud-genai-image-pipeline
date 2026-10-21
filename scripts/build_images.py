"""Start the ARM CodeBuild project that builds the simulator and Greengrass images, wait for it,
and confirm both tags exist in ECR. Usage: uv run python scripts/build_images.py [--params FILE]"""

from __future__ import annotations

import argparse
import sys
import time

from aws_ops import Ctx
from guards import mask_text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    args = ap.parse_args()
    ctx = Ctx(args.params)
    cb, ecr = ctx.client("codebuild"), ctx.client("ecr")
    tag = ctx.out["ImageTag"]
    repos = [f"{ctx.prefix}-simulator", f"{ctx.prefix}-greengrass"]

    def present() -> list[str]:
        have = []
        for r in repos:
            try:
                ecr.describe_images(repositoryName=r, imageIds=[{"imageTag": tag}])
                have.append(r)
            except ecr.exceptions.ImageNotFoundException:
                pass
        return have

    if present() == repos:
        print(f"images with tag {tag} already present")
        return
    build = cb.start_build(projectName=ctx.out["ImageBuildProject"])["build"]
    print(f"build started: {build['id'].split(':')[-1]}")
    while True:
        b = cb.batch_get_builds(ids=[build["id"]])["builds"][0]
        if b["buildStatus"] != "IN_PROGRESS":
            break
        time.sleep(20)
    phases = [(p["phaseType"], p.get("phaseStatus")) for p in b.get("phases", []) if p.get("phaseStatus")]
    print(mask_text(f"build {b['buildStatus']}: {phases}"))
    if b["buildStatus"] != "SUCCEEDED" or present() != repos:
        sys.exit(f"image build failed; see log group /aws/codebuild/{ctx.prefix}-images")
    print(f"images pushed: {repos} tag {tag}")


if __name__ == "__main__":
    main()
