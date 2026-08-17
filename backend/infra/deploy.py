"""One command for a full deploy, in the order the steps actually depend on.

A deploy is four things, and three of them are easy to forget:

    1. cdk diff              what would change
    2. cdk deploy            the stack itself
    3. infra.sync_env        physical names CDK chose -> backend/.env
    4. infra.apply_lifecycle S3 rules, on a bucket the stack does not own

Step 3 matters because CDK names resources itself, so the table name and the
queue URL are not knowable until after a deploy. Step 4 exists because
`CMA_CORPUS_BUCKET` names a bucket that predates the stack: CDK imports it with
`Bucket.from_bucket_name`, which yields a reference rather than a resource, and
you cannot attach a lifecycle configuration to something CloudFormation does
not manage. See the header of infra/apply_lifecycle.py.

This wrapper does not pretend those are one mechanism. It runs them in order,
stops at the first failure, and says which one it is on.

Usage (from backend/):
    python -m infra.deploy            # diff + both dry runs; changes nothing
    python -m infra.deploy --write    # actually deploy

**The default is a dry run, and that is the review gate.** The runbook's rule —
always `cdk diff`, confirm nothing is being replaced — only works if a human
reads the diff. So the bare command shows you everything and applies nothing,
and `--write` is the second, deliberate invocation. Nothing here prompts,
because a prompt is a thing people learn to press through.

As a backstop, `--write` refuses to deploy if the diff says any resource
requires replacement. On this stack that is not hypothetical: the state table
is `RemovalPolicy.RETAIN`, so a replacement does not destroy the records — it
orphans them and stands up an empty table in their place, and the app follows
the new name out of stack outputs. The data survives and the workbench comes
back blank, which is its own kind of bad morning. Override with
`--allow-replacement` once you have read the diff and meant it.

**`infra.put_secret` is deliberately not a step here.** It writes a secret
value, which has to come from a human, and `put_parameter` mints a new version
on every call — so running it per deploy would churn versions of an unchanged
key for no reason. It belongs to key rotation, not to deployment.

Also fixes a real footgun: the CDK CLI does not read `backend/.env`, so
`AWS_PROFILE` set there is invisible to it and the deploy either targets the
wrong account or fails on credentials. This loads the file and passes the
profile through, so one place configures both.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
CDK_DIR = BACKEND / "infra" / "cdk"
ENV_PATH = BACKEND / ".env"

# `cdk.json` runs `python app.py`, so the venv has to be on PATH for that
# `python` to be the one with aws-cdk-lib installed.
VENV_BIN = BACKEND / ".venv" / ("Scripts" if os.name == "nt" else "bin")


def _step(n: int, total: int, title: str) -> None:
    print(f"\n{'-' * 72}\n[{n}/{total}] {title}\n{'-' * 72}")


def _cdk_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    # Quietens the jsii banner about the Node version, which is several
    # screens long and buries the diff it is printed above.
    env.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")
    return env


def _run_cdk(args: list[str], capture: bool = False) -> tuple[int, str]:
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit(
            "npx not found on PATH. The CDK CLI is invoked through it; install "
            "Node.js, or run the cdk commands by hand from backend/infra/cdk."
        )
    cmd = [npx, "--no-install", "cdk", *args]
    print(f"$ {' '.join(cmd[1:])}  (in {CDK_DIR})\n")
    if not capture:
        return subprocess.run(cmd, cwd=CDK_DIR, env=_cdk_env()).returncode, ""
    # Captured so the replacement check below can read it, then echoed —
    # a diff nobody sees is not a review gate.
    #
    # `encoding` is explicit rather than left to `text=True`, which decodes
    # using the locale codec: cp1252 on Windows, while the CDK CLI emits UTF-8
    # box-drawing and emoji. That combination raises UnicodeDecodeError and
    # takes down the deploy wrapper over a decorative character — and only
    # once the diff is non-empty, so it looks like the change caused it.
    proc = subprocess.run(
        cmd, cwd=CDK_DIR, env=_cdk_env(),
        encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # Echo through the same replacement policy, because a console that cannot
    # decode UTF-8 usually cannot print it either.
    sys.stdout.write(proc.stdout.encode(
        sys.stdout.encoding or "utf-8", "replace",
    ).decode(sys.stdout.encoding or "utf-8", "replace"))
    return proc.returncode, proc.stdout


# CDK prints this next to any resource CloudFormation would destroy and
# recreate. Matching on the phrase rather than parsing the diff keeps this
# honest about being a backstop rather than a parser.
_REPLACEMENT_MARKERS = ("requires replacement", "may be replaced")


def main(write: bool, allow_replacement: bool) -> int:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)

    profile = os.getenv("AWS_PROFILE", "").strip()
    bucket = os.getenv("CMA_CORPUS_BUCKET", "").strip()
    if not bucket:
        raise SystemExit("CMA_CORPUS_BUCKET must be set in backend/.env.")

    print(f"profile : {profile or '(default credentials)'}")
    print(f"bucket  : {bucket}")
    print(f"mode    : {'WRITE — this will change AWS' if write else 'dry run — nothing will be changed'}")

    total = 4 if write else 3
    n = 1

    _step(n, total, "cdk diff")
    code, diff_out = _run_cdk(["diff"], capture=True)
    if code != 0:
        print("\ncdk diff failed — stopping before anything was changed.")
        return code

    replacing = [m for m in _REPLACEMENT_MARKERS if m in diff_out.lower()]
    if replacing:
        print(f"\n!! the diff reports a REPLACEMENT ({', '.join(replacing)}).")
        if write and not allow_replacement:
            print(
                "   Refusing to deploy. A replaced state table is orphaned, not\n"
                "   deleted — the records survive but the app follows the new,\n"
                "   empty table out of stack outputs. Read the diff above; if it\n"
                "   is what you meant, re-run with --allow-replacement."
            )
            return 2
        print("   proceeding anyway (--allow-replacement)" if write else "   (dry run)")

    if write:
        n += 1
        _step(n, total, "cdk deploy")
        # `--require-approval never` needs justifying, because it reads like
        # switching off a safety check. It is not: the check moved.
        #
        # The CDK CLI prompts on security-sensitive changes so a human sees
        # the IAM diff before it applies. This wrapper already printed that
        # table — the "IAM Statement Changes" block is part of the `cdk diff`
        # output above — and then required a *second, separate* invocation
        # with --write to get here. The human has therefore already reviewed
        # exactly what the prompt would have shown, and answering the same
        # question twice in one command trains people to answer it without
        # reading.
        #
        # It is also the difference between working and not: without a TTY the
        # CLI cannot prompt and refuses outright ("terminal (TTY) is not
        # attached"), so every IAM-touching deploy fails from CI, from a
        # script, or from any non-interactive shell.
        code, _ = _run_cdk(["deploy", "--require-approval", "never"])
        if code != 0:
            print("\ncdk deploy failed — .env and the S3 rules were left alone.")
            return code

    n += 1
    _step(n, total, "sync stack outputs into backend/.env")
    from infra import sync_env

    code = sync_env.apply(write)
    if code != 0:
        return code

    n += 1
    _step(n, total, "S3 lifecycle rules")
    from infra import apply_lifecycle

    code = apply_lifecycle.apply(write)
    if code != 0:
        return code

    print(f"\n{'-' * 72}")
    if write:
        print("done.")
        # This line used to say "restart the backend", which was true when the
        # API was started by hand and became quietly wrong the day it moved to
        # Fargate. A running task has the code baked into its image, so a
        # backend change needs a new image and a new tag - restarting anything
        # redeploys the same code. It cost a confusing 404 against an endpoint
        # that existed in the source and not in the container.
        if os.getenv("CMA_WEB_IMAGE_TAG", "").strip():
            print("Note: backend code changes are NOT live yet. The tasks run the")
            print("image named by CMA_WEB_IMAGE_TAG, so changing Python means:")
            print("    python -m infra.build_web_image     # prints a new tag")
            print("    set CMA_WEB_IMAGE_TAG, then deploy again")
            print("Frontend changes need `npm run build` and infra.deploy_frontend.")
        else:
            print("Restart the backend for code changes to take effect — nothing")
            print("here deploys the API itself; it is still started by hand.")
    else:
        print("dry run complete. Re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="actually deploy (default is a dry run)")
    parser.add_argument("--allow-replacement", action="store_true",
                        help="deploy even if the diff reports a replacement")
    args = parser.parse_args()
    sys.exit(main(args.write, args.allow_replacement))
