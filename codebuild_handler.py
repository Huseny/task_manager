from pathlib import Path

import boto3
import time
import sys


def _load_buildspec():
    default_path = Path(__file__).parent / "another_buildspec.yml"
    try:
        content = default_path.read_text()
        content = content if content.strip() else None
        return content
    except Exception as e:
        print(f"⚠️ Warning: Could not load buildspec file: {str(e)}")
        return None


def start_codebuild_task(project_name, repo_url):
    # Initialize the CodeBuild client
    # Ensure your AWS CLI is configured or env vars are set
    cb = boto3.client("codebuild")

    print(f"🚀 Triggering build for project: {project_name}")

    try:
        # Start the build with an environment variable override
        response = cb.start_build(
            projectName=project_name,
            environmentVariablesOverride=[
                {"name": "repo_url", "value": repo_url, "type": "PLAINTEXT"},
            ],
            buildspecOverride=_load_buildspec(),
        )

        build_id = response["build"]["id"]
        print(f"✅ Build started! ID: {build_id}")
        return build_id

    except Exception as e:
        print(f"❌ Failed to start build: {str(e)}")
        sys.exit(1)


def monitor_build(build_id):
    cb = boto3.client("codebuild")
    print("⏳ Monitoring build status...")

    while True:
        # Batch get builds to check current status
        build_info = cb.batch_get_builds(ids=[build_id])
        status = build_info["builds"][0]["buildStatus"]

        if status == "SUCCEEDED":
            print(f"\n✨ {status}: Project validated and tests passed!")
            break
        elif status in ["FAILED", "FAULT", "STOPPED"]:
            print(f"\n🚨 {status}: Check CodeBuild logs for details.")
            sys.exit(1)
        else:
            # Still IN_PROGRESS
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(10)  # Poll every 10 seconds


if __name__ == "__main__":
    # CONFIGURATION
    PROJECT_NAME = "MindflowTaskExecutor"
    TEST_URL = "https://github.com/eaglepointAiLabs/TASK-req_226cd37694ae"

    bid = start_codebuild_task(PROJECT_NAME, TEST_URL)
    monitor_build(bid)
