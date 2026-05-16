#!/usr/bin/env python3
"""
Deploy a GitHub repo to a new AWS EC2 instance, install Docker + Docker Compose,
open port 3000, clone the repo, and run: docker compose up

Usage example:
  pip install boto3 botocore
  python deploy_ec2_docker_compose.py \
    --region us-east-1 \
    --repo-url https://github.com/YOUR_ORG/YOUR_REPO.git \
    --ssh-cidr YOUR_PUBLIC_IP/32
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import random
import shlex
import stat
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import boto3
from botocore.exceptions import ClientError


AL2023_AMI_PARAMETER_BY_ARCH = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
}


APP_PORT = 3000
DEFAULT_APP_DIR = "/opt/github-docker-app"
SYSTEMD_SERVICE_NAME = "github-docker-app.service"


def die(message: str, exit_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(exit_code)


def validate_cidr(value: Optional[str], name: str) -> Optional[str]:
    if not value:
        return None
    try:
        ipaddress.ip_network(value, strict=False)
        return value
    except ValueError as exc:
        die(f"{name} must be a valid CIDR, for example 203.0.113.10/32. Got: {value!r}. {exc}")
    return None


def random_suffix(length: int = 7) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def default_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"docker-compose-app-{stamp}-{random_suffix()}"


def get_latest_al2023_ami_id(ssm_client: Any, arch: str) -> str:
    parameter_name = AL2023_AMI_PARAMETER_BY_ARCH[arch]
    try:
        response = ssm_client.get_parameter(Name=parameter_name)
        return response["Parameter"]["Value"]
    except ClientError as exc:
        die(f"Could not read AL2023 AMI parameter {parameter_name!r}: {exc}")
    raise AssertionError("unreachable")


def resolve_vpc_and_subnet(ec2_client: Any, subnet_id: Optional[str]) -> Tuple[str, str]:
    """Return (vpc_id, subnet_id). Uses the default VPC/subnet unless subnet_id is provided."""
    if subnet_id:
        try:
            response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            subnet = response["Subnets"][0]
            return subnet["VpcId"], subnet["SubnetId"]
        except (ClientError, IndexError) as exc:
            die(f"Could not describe subnet {subnet_id!r}: {exc}")

    try:
        vpcs = ec2_client.describe_vpcs(
            Filters=[{"Name": "is-default", "Values": ["true"]}]
        )["Vpcs"]
    except ClientError as exc:
        die(f"Could not describe default VPC: {exc}")

    if not vpcs:
        die(
            "No default VPC found in this region. Re-run with --subnet-id for a public subnet "
            "that has a route to the internet."
        )

    vpc_id = vpcs[0]["VpcId"]

    try:
        subnets = ec2_client.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "default-for-az", "Values": ["true"]},
            ]
        )["Subnets"]
        if not subnets:
            subnets = ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )["Subnets"]
    except ClientError as exc:
        die(f"Could not describe subnets for VPC {vpc_id}: {exc}")

    if not subnets:
        die(f"No subnet found in default VPC {vpc_id}.")

    subnets.sort(key=lambda item: item["SubnetId"])
    return vpc_id, subnets[0]["SubnetId"]


def ensure_key_pair(ec2_client: Any, key_name: str, pem_path: Path) -> bool:
    """
    Ensure an EC2 key pair exists.
    Returns True if a new private key file was created locally; False if the key already existed.
    """
    try:
        ec2_client.describe_key_pairs(KeyNames=[key_name])
        return False
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "InvalidKeyPair.NotFound":
            die(f"Could not describe key pair {key_name!r}: {exc}")

    try:
        response = ec2_client.create_key_pair(KeyName=key_name)
    except ClientError as exc:
        die(f"Could not create key pair {key_name!r}: {exc}")

    pem_path = pem_path.expanduser().resolve()
    pem_path.parent.mkdir(parents=True, exist_ok=True)
    pem_path.write_text(response["KeyMaterial"], encoding="utf-8")
    pem_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # chmod 600
    return True


def create_security_group(
    ec2_client: Any,
    *,
    name: str,
    vpc_id: str,
    app_cidr: str,
    ssh_cidr: Optional[str],
) -> str:
    group_name = f"{name}-sg"[:255]
    description = f"Expose app port {APP_PORT} for {name}"

    try:
        response = ec2_client.create_security_group(
            GroupName=group_name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": group_name},
                        {"Key": "CreatedBy", "Value": "deploy_ec2_docker_compose.py"},
                    ],
                }
            ],
        )
        group_id = response["GroupId"]
    except ClientError as exc:
        die(f"Could not create security group in VPC {vpc_id}: {exc}")

    ingress_permissions = [
        {
            "IpProtocol": "tcp",
            "FromPort": APP_PORT,
            "ToPort": APP_PORT,
            "IpRanges": [
                {
                    "CidrIp": app_cidr,
                    "Description": f"App/frontend/swagger port {APP_PORT}",
                }
            ],
        }
    ]

    if ssh_cidr:
        ingress_permissions.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH"}],
            }
        )

    try:
        ec2_client.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=ingress_permissions,
        )
    except ClientError as exc:
        die(f"Could not add ingress rules to security group {group_id}: {exc}")

    return group_id


def build_user_data(
    *,
    repo_url: str,
    branch: Optional[str],
    app_dir: str,
    app_subdir: str,
    env_lines: list[str],
) -> str:
    env_content = "\n".join(env_lines).strip()
    if env_content:
        env_content += "\n"
    env_b64 = base64.b64encode(env_content.encode("utf-8")).decode("ascii")

    repo_url_q = shlex.quote(repo_url)
    branch_q = shlex.quote(branch or "")
    app_dir_q = shlex.quote(app_dir)
    app_subdir_q = shlex.quote(app_subdir)
    env_b64_q = shlex.quote(env_b64)

    return f"""#!/bin/bash
set -euxo pipefail

exec > >(tee -a /var/log/bootstrap-github-docker-app.log) 2>&1

REPO_URL={repo_url_q}
BRANCH={branch_q}
APP_DIR={app_dir_q}
APP_SUBDIR={app_subdir_q}
ENV_FILE_B64={env_b64_q}
SERVICE_NAME={SYSTEMD_SERVICE_NAME!r}

export GIT_TERMINAL_PROMPT=0

# Base packages and Docker on Amazon Linux 2023
yum update -y
yum install -y docker git curl --allowerasing
systemctl enable --now docker

# Validate Docker + Buildx
docker version
docker buildx version

# Install Docker Compose v2 plugin so the command is: docker compose ...
MACHINE_ARCH="$(uname -m)"
case "$MACHINE_ARCH" in
  x86_64) COMPOSE_ARCH="x86_64" ;;
  aarch64) COMPOSE_ARCH="aarch64" ;;
  *) echo "Unsupported machine architecture for Docker Compose: $MACHINE_ARCH" >&2; exit 1 ;;
esac

COMPOSE_DIR="/usr/local/lib/docker/cli-plugins"
mkdir -p "$COMPOSE_DIR"

curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$COMPOSE_ARCH" \
  -o "$COMPOSE_DIR/docker-compose"

chmod +x "$COMPOSE_DIR/docker-compose"
docker compose version

# Install Buildx plugin for multi-platform build support
BUILDX_VERSION="v0.17.0"

mkdir -p /usr/local/lib/docker/cli-plugins

curl -fsSL \
  https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/buildx-$BUILDX_VERSION.linux-amd64 \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx

chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

# Clone the repo
rm -rf "$APP_DIR"
if [ -n "$BRANCH" ]; then
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

WORKDIR="$APP_DIR"
if [ "$APP_SUBDIR" != "." ]; then
  WORKDIR="$APP_DIR/$APP_SUBDIR"
fi

if [ ! -f "$WORKDIR/compose.yaml" ] && [ ! -f "$WORKDIR/compose.yml" ] && [ ! -f "$WORKDIR/docker-compose.yaml" ] && [ ! -f "$WORKDIR/docker-compose.yml" ]; then
  echo "No Compose file found in $WORKDIR" >&2
  exit 1
fi

# Optional .env file for Docker Compose
if [ -n "$ENV_FILE_B64" ]; then
  echo "$ENV_FILE_B64" | base64 -d > "$WORKDIR/.env"
  chmod 600 "$WORKDIR/.env"
fi

# Run exactly `docker compose up` under systemd so it survives SSH/session exit and reboots.

echo "Waiting for Docker daemon..."
until docker info >/dev/null 2>&1; do
  sleep 2
done
echo "Docker is ready"

cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=GitHub Docker Compose App
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$WORKDIR
ExecStart=/usr/bin/env docker compose up
ExecStop=/usr/bin/env docker compose down
Restart=on-failure
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

cat <<EOF
Bootstrap complete.
App service: $SERVICE_NAME
App working directory: $WORKDIR
App URL path: http://<this-instance-public-ip>:{APP_PORT}
Bootstrap log: /var/log/bootstrap-github-docker-app.log
Service logs: journalctl -u $SERVICE_NAME --no-pager
EOF
"""


def launch_instance(
    ec2_client: Any,
    *,
    ami_id: str,
    instance_type: str,
    subnet_id: str,
    security_group_id: str,
    key_name: Optional[str],
    user_data: str,
    name: str,
    root_volume_gb: int,
    iam_instance_profile: Optional[str],
) -> str:
    params: dict[str, Any] = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": user_data,
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [security_group_id],
                "AssociatePublicIpAddress": True,
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": root_volume_gb,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": name},
                    {"Key": "CreatedBy", "Value": "deploy_ec2_docker_compose.py"},
                    {"Key": "AppPort", "Value": str(APP_PORT)},
                ],
            },
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": f"{name}-root"},
                    {"Key": "CreatedBy", "Value": "deploy_ec2_docker_compose.py"},
                ],
            },
        ],
    }

    if key_name:
        params["KeyName"] = key_name

    if iam_instance_profile:
        params["IamInstanceProfile"] = {"Name": iam_instance_profile}

    try:
        response = ec2_client.run_instances(**params)
        return response["Instances"][0]["InstanceId"]
    except ClientError as exc:
        die(f"Could not launch EC2 instance: {exc}")
    raise AssertionError("unreachable")


def get_instance_public_address(ec2_client: Any, instance_id: str) -> tuple[Optional[str], Optional[str]]:
    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]
        return instance.get("PublicIpAddress"), instance.get("PublicDnsName")
    except (ClientError, IndexError) as exc:
        die(f"Could not describe instance {instance_id}: {exc}")
    raise AssertionError("unreachable")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a GitHub Docker Compose app to a new EC2 instance and expose port 3000."
    )
    parser.add_argument("--repo-url", required=True, help="GitHub repo URL, for example https://github.com/org/repo.git")
    parser.add_argument("--branch", default=None, help="Optional Git branch/tag to clone")
    parser.add_argument("--app-subdir", default=".", help="Subdirectory containing compose.yaml/docker-compose.yml")
    parser.add_argument("--env", dest="compose_env", action="append", default=[], help="Line to write to .env, e.g. --env API_URL=https://... Repeatable.")

    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    parser.add_argument("--instance-type", default="t3.small")
    parser.add_argument("--arch", choices=sorted(AL2023_AMI_PARAMETER_BY_ARCH), default="x86_64", help="Use arm64 for Graviton types like t4g.small")
    parser.add_argument("--subnet-id", default=None, help="Optional public subnet ID. Defaults to a default subnet in the default VPC.")
    parser.add_argument("--iam-instance-profile", default=None, help="Optional EC2 IAM instance profile name")
    parser.add_argument("--root-volume-gb", type=int, default=30)

    parser.add_argument("--name", default=default_name(), help="Name tag prefix for created resources")
    parser.add_argument("--app-cidr", default="0.0.0.0/0", help="CIDR allowed to access port 3000")
    parser.add_argument("--ssh-cidr", default=None, help="Optional CIDR allowed to SSH on port 22, e.g. YOUR_PUBLIC_IP/32")
    parser.add_argument("--key-name", default=None, help="Existing or new EC2 key pair name. If omitted, one is created.")
    parser.add_argument("--pem-path", default=None, help="Where to save the created private key. Defaults to ./{key-name}.pem")
    parser.add_argument("--no-key-pair", action="store_true", help="Launch without an EC2 key pair")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.root_volume_gb < 8:
        die("--root-volume-gb must be at least 8")

    app_cidr = validate_cidr(args.app_cidr, "--app-cidr") or "0.0.0.0/0"
    ssh_cidr = validate_cidr(args.ssh_cidr, "--ssh-cidr")

    if args.no_key_pair and args.key_name:
        die("Use either --key-name or --no-key-pair, not both")

    ec2_client = boto3.client("ec2", region_name=args.region)
    ssm_client = boto3.client("ssm", region_name=args.region)

    ami_id = get_latest_al2023_ami_id(ssm_client, args.arch)
    vpc_id, subnet_id = resolve_vpc_and_subnet(ec2_client, args.subnet_id)

    key_name: Optional[str]
    pem_path: Optional[Path] = None
    created_key = False
    if args.no_key_pair:
        key_name = None
    else:
        key_name = args.key_name or f"{args.name}-key"[:255]
        pem_path = Path(args.pem_path or f"./{key_name}.pem")
        created_key = ensure_key_pair(ec2_client, key_name, pem_path)

    security_group_id = create_security_group(
        ec2_client,
        name=args.name,
        vpc_id=vpc_id,
        app_cidr=app_cidr,
        ssh_cidr=ssh_cidr,
    )

    user_data = build_user_data(
        repo_url=args.repo_url,
        branch=args.branch,
        app_dir=DEFAULT_APP_DIR,
        app_subdir=args.app_subdir,
        env_lines=args.compose_env,
    )

    instance_id = launch_instance(
        ec2_client,
        ami_id=ami_id,
        instance_type=args.instance_type,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        key_name=key_name,
        user_data=user_data,
        name=args.name,
        root_volume_gb=args.root_volume_gb,
        iam_instance_profile=args.iam_instance_profile,
    )

    print(f"Launched EC2 instance: {instance_id}")
    print("Waiting for EC2 state: running")
    try:
        ec2_client.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    except ClientError as exc:
        die(f"Instance waiter failed for {instance_id}: {exc}")

    public_ip, public_dns = get_instance_public_address(ec2_client, instance_id)

    result = {
        "instance_id": instance_id,
        "region": args.region,
        "ami_id": ami_id,
        "instance_type": args.instance_type,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": security_group_id,
        "key_name": key_name,
        "created_private_key_file": str(pem_path) if created_key and pem_path else None,
        "public_ip": public_ip,
        "public_dns": public_dns,
        "app_url": f"http://{public_ip}:{APP_PORT}" if public_ip else None,
        "ssh_command": f"ssh -i {pem_path} ec2-user@{public_ip}" if (public_ip and pem_path and ssh_cidr) else None,
        "logs": {
            "bootstrap": "/var/log/bootstrap-github-docker-app.log",
            "service": f"journalctl -u {SYSTEMD_SERVICE_NAME} --no-pager",
        },
    }

    print(json.dumps(result, indent=2))

    if not ssh_cidr:
        print("\nSSH was not opened because --ssh-cidr was not supplied.")
    if args.instance_type.startswith("t4g") and args.arch != "arm64":
        print("\nNote: t4g instances require --arch arm64.", file=sys.stderr)
    if args.arch == "arm64" and not args.instance_type.startswith(("t4g", "c6g", "m6g", "r6g", "c7g", "m7g", "r7g")):
        print("\nNote: arm64 AMIs require an ARM/Graviton instance type.", file=sys.stderr)


if __name__ == "__main__":
    main()
