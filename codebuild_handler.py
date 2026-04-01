import boto3
import time
import sys


def start_codebuild_task(project_name, gdrive_url):
    # Initialize the CodeBuild client
    # Ensure your AWS CLI is configured or env vars are set
    cb = boto3.client("codebuild")

    print(f"🚀 Triggering build for project: {project_name}")

    try:
        # Start the build with an environment variable override
        response = cb.start_build(
            projectName=project_name,
            environmentVariablesOverride=[
                {"name": "GDRIVE_URL", "value": gdrive_url, "type": "PLAINTEXT"},
            ],
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
    PROJECT_NAME = "TaskValidator"
    TEST_URL = "https://drive.google.com/open?id=1zq4CH4sUgTL2OBxkbWHaVTyjPOeO2nfc"

    bid = start_codebuild_task(PROJECT_NAME, TEST_URL)
    monitor_build(bid)
