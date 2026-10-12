"""Everything a module hook may need, in one object."""

from __future__ import annotations

from dataclasses import dataclass, field

from .cloud import Cloud


@dataclass
class Context:
    cfg: dict                       # resolved config (see config.resolve)
    release: dict                   # installer.yaml + the selected release manifest
    cloud: Cloud
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)   # slot -> stack outputs
    secret: dict[str, str] | None = None
    cache: dict = field(default_factory=dict)                          # per-run memo for read-only lookups
    modules: list = field(default_factory=list)                        # the selected modules, in execution order

    @property
    def name(self) -> str:
        return self.cfg["name"]

    @property
    def region(self) -> str:
        return self.cfg["aws"]["region"]

    @property
    def cluster_name(self) -> str:
        return f"{self.name}-cluster"

    def stack_name(self, slot: str) -> str:
        return f"{self.name}-{slot}"

    @property
    def secret_arn(self) -> str:
        return self.cfg["secrets"]["arn"]

    def account_id(self) -> str:
        if "account_id" not in self.cache:
            data = self.cloud.aws_json("sts", "get-caller-identity")
            self.cache["account_id"] = data["Account"]
        return self.cache["account_id"]

    def caller_principal(self) -> str | None:
        """IAM user or role ARN of whoever runs the installer, in the form EKS access entries accept.

        Sessions (`assumed-role/NAME/session`, including AWS IAM Identity Center and CloudShell) map to the role
        itself, with its real path, which only IAM knows. Returns None for the account root user, which EKS does
        not accept as a principal.
        """
        if "caller_principal" not in self.cache:
            arn = self.cloud.aws_json("sts", "get-caller-identity")["Arn"]
            principal: str | None = arn
            if ":assumed-role/" in arn:
                role = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
                data = self.cloud.aws_json("iam", "get-role", "--role-name", role, check=False)
                principal = data["Role"]["Arn"] if data else None
            elif arn.endswith(":root"):
                principal = None
            self.cache["caller_principal"] = principal
        return self.cache["caller_principal"]

