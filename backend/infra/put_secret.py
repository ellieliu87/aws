"""Put a secret into SSM Parameter Store as a SecureString.

This is a script rather than a line in `infra/cdk/async_stack.py`, and the
reason is not laziness: **CloudFormation cannot create a SecureString.**
`AWS::SSM::Parameter` supports `String` and `StringList` only, because a
template is a plaintext document that ends up in the CloudFormation console,
the change set, and anyone's `cdk diff`. Declaring the secret in CDK would
mean writing the key into exactly the places this whole exercise exists to
keep it out of.

So the parameter is created out of band, once, by whoever holds the key. The
app then reads it at boot through `services/secrets.py`.

Usage (from backend/):
    python -m infra.put_secret OPENAI_API_KEY               # dry run, value from .env
    python -m infra.put_secret OPENAI_API_KEY --write
    python -m infra.put_secret OPENAI_API_KEY --stdin --write

`CMA_SECRETS_PREFIX` decides where it lands: with `/cma/workbench` set, the
above writes `/cma/workbench/OPENAI_API_KEY`, which `services/secrets.py`
turns back into `OPENAI_API_KEY` in the process environment.

Deliberately does NOT edit `backend/.env`. Removing the only copy of a key
before anyone has confirmed the parameter reads back is a bad afternoon, and
the whole point of this file is that the value should be somewhere a script
does not casually rewrite. It prints the line to delete instead.

The parameter uses the AWS-managed `alias/aws/ssm` key, which is free. A
customer-managed KMS key is a compliance requirement rather than a technical
one — see the deliberately-absent list in docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _region() -> str:
    for var in ("CMA_JOBS_REGION", "CMA_BEDROCK_REGION", "AWS_REGION",
                "AWS_DEFAULT_REGION"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return "us-east-1"


def _mask(value: str) -> str:
    """Enough to recognise the right key, not enough to be one."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}…{value[-4:]} ({len(value)} chars)"


def _read_value(name: str, from_stdin: bool) -> str:
    if from_stdin:
        # Not `input()`: a key pasted at a prompt lands in the shell's scroll
        # buffer, and piping it in from a password manager is the point.
        return sys.stdin.read().strip()
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set in the environment or {ENV_PATH.name}.\n"
            f"Either set it there first, or pipe the value in:\n"
            f"    echo -n '<value>' | python -m infra.put_secret {name} --stdin --write"
        )
    return value


def apply(name: str, *, write: bool, from_stdin: bool) -> int:
    prefix = os.getenv("CMA_SECRETS_PREFIX", "").strip()
    if not prefix:
        raise SystemExit(
            "CMA_SECRETS_PREFIX must be set in backend/.env — it is the path "
            "the parameter is written to and the path the app reads back.\n"
            "    CMA_SECRETS_PREFIX=/cma/workbench"
        )

    path = "/" + prefix.strip("/") + "/" + name
    value = _read_value(name, from_stdin)

    print(f"  {path}")
    print("    type   SecureString (alias/aws/ssm)")
    print(f"    region {_region()}")
    print(f"    value  {_mask(value)}")

    if not write:
        print("\n(dry run — re-run with --write to apply)")
        return 0

    import boto3

    boto3.client("ssm", region_name=_region()).put_parameter(
        Name=path,
        Value=value,
        Type="SecureString",
        # Overwrite so re-running after a key rotation is the same command.
        # Parameter Store keeps the previous versions either way.
        Overwrite=True,
        Description="CMA Workbench — read at boot by services/secrets.py",
    )
    print(f"\nwrote {path}")
    print(
        "\nNow, in order:\n"
        f"  1. confirm it reads back:  aws ssm get-parameter --name {path} "
        f"--with-decryption --region {_region()}\n"
        f"  2. delete the `{name}=` line from {ENV_PATH}\n"
        "  3. restart the backend — startup logs the names it loaded"
    )
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="environment variable name, e.g. OPENAI_API_KEY")
    parser.add_argument("--write", action="store_true",
                        help="actually write the parameter (default is a dry run)")
    parser.add_argument("--stdin", action="store_true",
                        help="read the value from stdin instead of the environment")
    args = parser.parse_args()

    raise SystemExit(apply(args.name, write=args.write, from_stdin=args.stdin))
