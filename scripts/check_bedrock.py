#!/usr/bin/env python3
"""
Verify that Bedrock is usable from this machine, and discover the right model IDs.

Run this before pointing Aegis at Bedrock. It answers, in order:

  1. Are AWS credentials resolvable at all, and which identity are they?
  2. Which Claude inference profiles can this account actually see?
  3. Does a real Converse call succeed, and does prompt caching engage?
  4. What did that call cost?

Model IDs move around between releases, so this discovers them from your account
rather than trusting a hardcoded list. Whatever it prints under "recommended
configuration" can be pasted straight into your .env.

    python scripts/check_bedrock.py
    python scripts/check_bedrock.py --region us-west-2
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DIVIDER = "-" * 74

# Preference order within each tier: first match wins.
CHEAP_PREFERENCES = ("haiku-4-5", "haiku-4", "nova-lite", "haiku-3-5")
STRONG_PREFERENCES = ("sonnet-5", "sonnet-4-6", "sonnet-4-5", "nova-pro")

# Cross-region inference profile prefix by region family.
REGION_PREFIX = {"us": "us", "eu": "eu", "ap": "apac", "ca": "us", "sa": "us"}

# Probed when the credentials cannot list profiles (typical for a Bedrock API key).
# Newest first: the probe stops at the first one that actually answers.
CANDIDATE_MODELS = (
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-sonnet-5-20260514-v1:0",
    "anthropic.claude-sonnet-4-6-20260101-v1:0",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0",
)


def _fail(message: str, hint: str = "") -> None:
    print(f"  FAIL  {message}")
    if hint:
        print(f"        {hint}")


def _ok(message: str) -> None:
    print(f"  ok    {message}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=None, help="AWS region (default: from your profile)")
    parser.add_argument("--no-invoke", action="store_true", help="Skip the paid test call")
    args = parser.parse_args()

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        _fail("boto3 is not installed.", "pip install boto3")
        return 1

    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    region = session.region_name or "us-east-1"
    print(DIVIDER)
    print(f"Bedrock preflight  |  region: {region}")
    print(DIVIDER)

    # -- 1. credentials ----------------------------------------------------- #
    #
    # Two supported auth modes:
    #   a) a Bedrock API key in AWS_BEARER_TOKEN_BEDROCK -- scoped to Bedrock only,
    #      so STS is not callable with it and there is no identity to print;
    #   b) ordinary SigV4 credentials, where STS tells us exactly who we are.
    print("\n[1/4] AWS credentials")
    bearer = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if bearer:
        _ok(f"using a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK, {len(bearer)} chars)")
        print("        identity check skipped -- an API key is scoped to Bedrock only")
    else:
        try:
            identity = session.client("sts").get_caller_identity()
            _ok(f"authenticated as {identity['Arn']}")
            _ok(f"account {identity['Account']}")
        except NoCredentialsError:
            _fail(
                "No AWS credentials found.",
                "Either export AWS_BEARER_TOKEN_BEDROCK=<your Bedrock API key>, "
                "or run 'aws configure' with an access key.",
            )
            return 1
        except ClientError as exc:
            _fail(f"STS rejected the credentials: {exc.response['Error']['Code']}")
            return 1
        except Exception as exc:  # network, proxy, DNS, TLS
            _fail(
                f"Could not reach AWS ({type(exc).__name__}).",
                "Check your network, VPN or proxy settings. This is a connectivity "
                "problem, not a credentials problem.",
            )
            return 1

    # -- 2. inference profiles ---------------------------------------------- #
    #
    # Discovery is the nice path but not the only one. A Bedrock API key is scoped to
    # invocation and often cannot list profiles at all, so a denial here is not a
    # failure -- we fall back to probing a candidate list directly. Being unable to
    # ask "what exists" does not stop us finding out what works.
    print("\n[2/4] Visible inference profiles")
    prefix = REGION_PREFIX.get(region.split("-")[0], "us")
    discovered = True
    try:
        control = session.client("bedrock", region_name=region)
        profiles = control.list_inference_profiles(maxResults=100).get(
            "inferenceProfileSummaries", []
        )
        ids = [p["inferenceProfileId"] for p in profiles]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            print("  note  cannot list profiles with these credentials -- probing instead")
            print("        (normal for a Bedrock API key; invocation still works)")
            discovered = False
            ids = [f"{prefix}.{candidate}" for candidate in CANDIDATE_MODELS]
        else:
            _fail(f"{code}: {exc.response['Error']['Message']}")
            return 1
    except Exception as exc:  # network, proxy, DNS, TLS
        _fail(
            f"Could not reach the Bedrock endpoint ({type(exc).__name__}).",
            "Check your network, VPN or proxy. Also confirm the region is one where "
            "Bedrock is available.",
        )
        return 1

    claude = [
        i for i in ids if "anthropic" in i.lower() or "nova" in i.lower()
    ]
    if not claude:
        _fail(
            "No Anthropic or Nova inference profiles are visible in this region.",
            "Enable model access in the Bedrock console (Model catalog -> pick a model "
            "-> submit the use case form), or try a different region.",
        )
        return 1

    if discovered:
        _ok(f"{len(claude)} usable profile(s) found")
        for profile_id in sorted(claude):
            print(f"        {profile_id}")
    else:
        _ok(f"{len(claude)} candidate(s) to probe")

    def pick(preferences: tuple[str, ...]) -> str | None:
        for token in preferences:
            for profile_id in claude:
                if token in profile_id:
                    return profile_id
        return None

    cheap = pick(CHEAP_PREFERENCES)
    strong = pick(STRONG_PREFERENCES)

    print("\n[3/4] Model tier selection")
    if cheap:
        _ok(f"cheap tier  (specialists)  -> {cheap}")
    else:
        _fail("No small/fast model available. Enable Claude Haiku or Amazon Nova Lite.")
    if strong:
        _ok(f"strong tier (RCA, disclosure) -> {strong}")
    else:
        _fail("No strong model available. Enable a Claude Sonnet model.")
    if not (cheap and strong):
        return 1

    # -- 4. a real call ------------------------------------------------------ #
    print("\n[4/4] Live Converse call")
    if args.no_invoke:
        print("  skipped (--no-invoke)")
    else:
        runtime = session.client("bedrock-runtime", region_name=region)

        def probe(model_id: str) -> tuple[bool, dict, str]:
            """One tiny Converse call. Returns (worked, usage, message)."""
            try:
                response = runtime.converse(
                    modelId=model_id,
                    system=[
                        {
                            "text": "You are a preflight check. Reply with a single JSON "
                            "object and nothing else."
                        },
                        {"cachePoint": {"type": "default"}},
                    ],
                    messages=[
                        {"role": "user", "content": [{"text": 'Reply exactly: {"ok": true}'}]}
                    ],
                    inferenceConfig={"maxTokens": 64, "temperature": 0.0},
                )
            except ClientError as exc:
                return False, {}, f"{exc.response['Error']['Code']}: {exc.response['Error']['Message']}"
            text = "".join(
                b.get("text", "")
                for b in response.get("output", {}).get("message", {}).get("content", [])
            )
            return True, response.get("usage", {}) or {}, text.strip()

        # Walk the tier in preference order rather than giving up on the first miss.
        # A model can be listed but not enabled, or enabled but not in this region.
        def first_working(preferences: tuple[str, ...], label: str) -> tuple[str | None, dict]:
            tried: list[str] = []
            for token in preferences:
                for model_id in claude:
                    if token not in model_id or model_id in tried:
                        continue
                    tried.append(model_id)
                    worked, usage, message = probe(model_id)
                    if worked:
                        _ok(f"{label}: {model_id}")
                        _ok(f"        replied {message[:40]!r}")
                        return model_id, usage
                    print(f"  --    {model_id} unavailable ({message[:70]})")
            return None, {}

        cheap, cheap_usage = first_working(CHEAP_PREFERENCES, "cheap tier verified")
        if cheap is None:
            _fail(
                "No small/fast model could be invoked.",
                "Submit the use case form for Claude Haiku in the Bedrock console, "
                "and confirm your region matches where you enabled it.",
            )
            return 1

        strong, _ = first_working(STRONG_PREFERENCES, "strong tier verified")
        if strong is None:
            print(
                "  note  no Sonnet-class model is invokable; falling back to the cheap "
                "model for every agent. Aegis runs, RCA quality is just lower."
            )
            strong = cheap

        if not cheap_usage.get("cacheReadInputTokens") and not cheap_usage.get(
            "cacheWriteInputTokens"
        ):
            print(
                "  note  no prompt-cache fields returned on the first call. That is normal "
                "on a cold cache; if it persists, caching is unsupported here and input "
                "cost is simply higher."
            )

    # -- output -------------------------------------------------------------- #
    print("\n" + DIVIDER)
    print("Recommended configuration -- paste into your .env")
    print(DIVIDER)
    print(
        "\n".join(
            [
                "AEGIS_MODEL_BACKEND=bedrock",
                f"AWS_REGION={region}",
                f"AEGIS_CHEAP_MODEL={cheap}",
                f"AEGIS_STRONG_MODEL={strong}",
                "AEGIS_MAX_USD_PER_INCIDENT=0.50",
            ]
        )
    )
    print()
    print("Verify pricing for these model IDs in aegis/config.py:MODEL_PRICES")
    print("so the budget governor charges you accurately.")
    print(json.dumps({"region": region, "cheap": cheap, "strong": strong}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
