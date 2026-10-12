"""Thin, explicit wrappers around the aws, kubectl and helm command lines.

Every AWS call carries --region (and --profile when given); every kubectl/helm
call carries an explicit context. Nothing relies on ambient defaults.
"""

from __future__ import annotations

import json
from typing import Any

from .runner import Result, Runner


class Cloud:
    def __init__(self, runner: Runner, region: str, profile: str | None, kube_context: str) -> None:
        self.runner = runner
        self.region = region
        self.profile = profile
        self.kube_context = kube_context

    # ---- aws -------------------------------------------------------------
    def _aws_argv(self, args: list[str], region: str | None = None) -> list[str]:
        argv = ["aws", *args, "--region", region or self.region]
        if self.profile:
            argv += ["--profile", self.profile]
        return argv

    def aws(self, *args: str, write: bool = False, check: bool = True, region: str | None = None,
            stream: bool = False) -> Result:
        return self.runner.run(self._aws_argv(list(args), region), write=write, check=check, stream=stream)

    def aws_json(self, *args: str, region: str | None = None, check: bool = True) -> Any:
        result = self.runner.run(
            self._aws_argv([*args, "--output", "json"], region), check=check
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    # ---- kubectl ---------------------------------------------------------
    def kubectl(self, *args: str, write: bool = False, check: bool = True, input_text: str | None = None,
                stream: bool = False) -> Result:
        return self.runner.run(
            ["kubectl", "--context", self.kube_context, *args],
            write=write, check=check, input_text=input_text, stream=stream,
        )

    def kubectl_json(self, *args: str, check: bool = True) -> Any:
        result = self.kubectl(*args, "-o", "json", check=check)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    # ---- helm ------------------------------------------------------------
    def helm(self, *args: str, write: bool = False, check: bool = True, stream: bool = False) -> Result:
        return self.runner.run(
            ["helm", *args, "--kube-context", self.kube_context], write=write, check=check, stream=stream
        )
