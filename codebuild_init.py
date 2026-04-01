from __future__ import annotations
import logging
import time
import json
from typing import List, Optional, Dict

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config as BotoConfig

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("app.infra.codebuild.cli")
logging.basicConfig(level=logging.INFO)


class CodeBuildSetupError(RuntimeError):
    pass


class CodeBuildSetup:
    """
    Idempotent setup helper for CodeBuild environment:
    - ensures IAM role (with trust policy) exists
    - attaches required policies (managed or custom)
    - ensures CloudWatch log group exists
    - creates or updates CodeBuild project
    """

    DEFAULT_POLICIES = [
        # Managed policy for convenience - adjust for least-privilege in prod
        "arn:aws:iam::aws:policy/AWSCodeBuildDeveloperAccess",
        "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        "arn:aws:iam::aws:policy/SecretsManagerReadWrite",
    ]

    def __init__(
        self,
        aws_region: str,
        project_name: str,
        role_name: str,
        image: str,
        compute_type: str,
        privileged_mode: bool,
        log_group: str,
        policy_arns: Optional[List[str]] = None,
        boto_config: Optional[BotoConfig] = None,
        iam_client=None,
        codebuild_client=None,
        logs_client=None,
    ):
        self.region = aws_region
        self.project_name = project_name
        self.role_name = role_name
        self.image = image
        self.compute_type = compute_type
        self.privileged_mode = privileged_mode
        self.log_group = log_group
        self.policy_arns = policy_arns or self.DEFAULT_POLICIES

        # boto3 clients (injected for testability)
        cfg = boto_config or BotoConfig(retries={"max_attempts": 6})
        self.iam = iam_client or boto3.client(
            "iam", region_name=self.region, config=cfg
        )
        self.cb = codebuild_client or boto3.client(
            "codebuild", region_name=self.region, config=cfg
        )
        self.logs = logs_client or boto3.client(
            "logs", region_name=self.region, config=cfg
        )

    # -------------------------
    # IAM Role & policies
    # -------------------------
    def _trust_policy(self) -> Dict:
        """Trust policy allowing CodeBuild to assume the role."""
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "codebuild.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

    def ensure_iam_role(self) -> str:
        """Create role if missing, return role ARN."""
        try:
            logger.info("Ensuring IAM role %s exists", self.role_name)
            resp = self.iam.get_role(RoleName=self.role_name)
            role_arn = resp["Role"]["Arn"]
            logger.info("IAM role already exists: %s", role_arn)
            return role_arn
        except ClientError as e:
            if e.response["Error"]["Code"] not in (
                "NoSuchEntity",
                "NoSuchEntityException",
            ):
                logger.exception("Unexpected error fetching IAM role")
                raise CodeBuildSetupError("failed to get role") from e

        # role doesn't exist -> create
        try:
            resp = self.iam.create_role(
                RoleName=self.role_name,
                AssumeRolePolicyDocument=json.dumps(self._trust_policy()),
                Description="Execution role for CodeBuild TaskExecutor project",
            )
            role_arn = resp["Role"]["Arn"]
            logger.info("Created IAM role: %s", role_arn)
            # Wait for role to be fully available (propagation)
            self._wait_for_iam_propagation()
            return role_arn
        except ClientError as e:
            logger.exception("Failed to create IAM role")
            raise CodeBuildSetupError("failed to create role") from e

    def ensure_policies_attached(self, role_name: Optional[str] = None):
        """Attach managed policies if not attached yet (idempotent)."""
        role_name = role_name or self.role_name
        logger.info("Ensuring policies attached to role %s", role_name)
        try:
            # list attached role policies and attach those missing
            attached = []
            paginator = self.iam.get_paginator("list_attached_role_policies")
            for page in paginator.paginate(RoleName=role_name):
                for p in page.get("AttachedPolicies", []):
                    attached.append(p["PolicyArn"])

            for policy in self.policy_arns:
                if policy in attached:
                    logger.info("Policy %s already attached", policy)
                    continue
                logger.info("Attaching policy %s to role %s", policy, role_name)
                try:
                    self.iam.attach_role_policy(RoleName=role_name, PolicyArn=policy)
                except ClientError:
                    logger.exception("Failed attaching policy %s", policy)
                    raise
        except ClientError as e:
            logger.exception("Error ensuring attached policies")
            raise CodeBuildSetupError("failed to attach policies") from e

    def _wait_for_iam_propagation(self, wait_seconds: int = 15):
        """Simple sleep to allow IAM propagation after role creation (idempotent)."""
        logger.info("Waiting %ds for IAM propagation", wait_seconds)
        time.sleep(wait_seconds)

    # -------------------------
    # CloudWatch Logs
    # -------------------------
    def ensure_log_group(self):
        """Ensure CloudWatch Logs group exists (idempotent)."""
        try:
            logger.info("Ensuring CloudWatch log group %s exists", self.log_group)
            self.logs.create_log_group(logGroupName=self.log_group)
            logger.info("Created log group %s", self.log_group)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ResourceAlreadyExistsException", "ResourceAlreadyExists"):
                logger.info("Log group %s already exists", self.log_group)
            else:
                logger.exception("Failed to ensure log group")
                raise CodeBuildSetupError("failed to ensure log group") from e

    # -------------------------
    # CodeBuild Project
    # -------------------------
    def _make_project_payload(self, role_arn: str) -> Dict:
        """
        Build the create_project payload. Keep it minimal and allow overrides later.
        We set source to GITHUB with a placeholder; actual runs should supply source overrides.
        """
        env = {
            "type": "LINUX_CONTAINER",
            "image": self.image,
            "computeType": self.compute_type,
            "privilegedMode": bool(self.privileged_mode),
        }

        logs_config = {
            "cloudWatchLogs": {
                "status": "ENABLED",
                "groupName": self.log_group,
                # streamName can be a prefix; CodeBuild will append build id
                "streamName": "build-log",
            }
        }

        payload = {
            "name": self.project_name,
            "description": "Clones, builds, runs user repos and streams logs (created by infra setup)",
            "source": {
                "type": "GITHUB",
                "location": "https://github.com/Huseny/task_manager",
            },
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": env,
            "serviceRole": role_arn,
            "logsConfig": logs_config,
        }
        return payload

    def create_or_update_project(self, role_arn: str):
        """
        Create the CodeBuild project if missing; if it exists, attempt an update to
        keep compute/image/privileged settings in sync.
        """
        payload = self._make_project_payload(role_arn)

        try:
            # try to get existing project
            existing = self.cb.batch_get_projects(names=[self.project_name])
            if existing.get("projects"):
                logger.info("Project %s already exists, updating", self.project_name)
                # strip fields not allowed in update or handle with update_project API
                update_kwargs = {
                    "name": payload["name"],
                    "description": payload["description"],
                    "artifacts": payload["artifacts"],
                    "environment": payload["environment"],
                    "serviceRole": payload["serviceRole"],
                    "logsConfig": payload["logsConfig"],
                }
                try:
                    self.cb.update_project(**update_kwargs)
                    logger.info("Project %s updated", self.project_name)
                    return
                except ClientError as e:
                    logger.exception("Failed to update project %s", self.project_name)
                    raise CodeBuildSetupError("failed to update project") from e
            # create new project
            logger.info("Creating CodeBuild project %s", self.project_name)
            self.cb.create_project(**payload)
            logger.info("Created CodeBuild project %s", self.project_name)
        except ClientError as e:
            logger.exception("Error creating/updating project")
            raise CodeBuildSetupError("failed to create/update project") from e

    # -------------------------
    # High-level setup
    # -------------------------
    def setup(self):
        """Run full idempotent setup flow."""
        logger.info("Starting CodeBuild environment setup")
        # 1. Ensure IAM role
        role_arn = self.ensure_iam_role()

        # 2. Attach policies
        self.ensure_policies_attached(role_name=self.role_name)

        # 3. Create log group
        self.ensure_log_group()

        # 4. Create or update CodeBuild project
        self.create_or_update_project(role_arn=role_arn)

        logger.info("CodeBuild environment setup complete")
        return {"roleArn": role_arn, "projectName": self.project_name}


def main():
    setup = CodeBuildSetup(
        aws_region="us-east-1",
        project_name="TaskValidator",
        role_name="CodeBuildExecutionRole",
        image="aws/codebuild/standard:7.0",
        compute_type="BUILD_GENERAL1_SMALL",
        privileged_mode=True,
        log_group="/codebuild/TaskValidator",
    )
    result = setup.setup()
    logger.info("Setup result: %s", result)


if __name__ == "__main__":
    main()
