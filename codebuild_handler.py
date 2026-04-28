from pathlib import Path

import boto3
import time
import sys


def _load_buildspec():
    default_path = Path(__file__).parent / "enhanced_buildspec.yml"
    try:
        content = default_path.read_text()
        content = content if content.strip() else None
        return content
    except Exception as e:
        print(f"⚠️ Warning: Could not load buildspec file: {str(e)}")
        return None


def start_codebuild_task(project_name, task_id, aquila_token):
    # Initialize the CodeBuild client
    # Ensure your AWS CLI is configured or env vars are set
    cb = boto3.client("codebuild")

    print(f"🚀 Triggering build for project: {project_name}")

    try:
        # Start the build with an environment variable override
        response = cb.start_build(
            projectName=project_name,
            environmentVariablesOverride=[
                {
                    "name": "REPORT_PREFIX",
                    "value": "mindflow-task-reports",
                    "type": "PLAINTEXT",
                },
                {"name": "TASK_ID", "value": task_id, "type": "PLAINTEXT"},
                {
                    "name": "AQUILA_ACCESS_TOKEN",
                    "value": aquila_token,
                    "type": "PLAINTEXT",
                },
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
    TASK_ID = "req_3a63e30d3771"
    AQUILA_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI5MGQ2NGQ1YS0zZDg2LTRjOTQtOWNjMS1kYjY3MTI1NTc3NzAiLCJleHAiOjE3NzczNjI2MzB9.vLzr3u2Vlo89_IKNHyHf5BpB0m23YgDjHp-HEhgTzCk"

    bid = start_codebuild_task(PROJECT_NAME, TASK_ID, AQUILA_TOKEN)
    monitor_build(bid)
