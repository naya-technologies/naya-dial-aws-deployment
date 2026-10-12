"""cni/cilium - platform values that an overlay network needs.

The installation itself (images, Helm release between the two compute phases) is done by dial_install.cilium.
AWS cannot route to pod addresses of an overlay network, so what the VPC reaches must go through the node:
load balancers use instance targets (NodePort services) and webhooks called by the control plane use host networking.
"""

from dial_install import cilium

TARGET_TYPE = "alb.ingress.kubernetes.io/target-type"


def check(ctx):
    return cilium.findings(ctx)


def value_overrides(ctx, chart_values):
    return cilium.platform_values(chart_values)
