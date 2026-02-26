#!/bin/bash

set -e

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-dial-production}"

echo "=========================================="
echo "DIAL Post-Deployment Setup"
echo "=========================================="
echo "Stack: $STACK_NAME"
echo "Region: $REGION"
echo ""

# Check and install kubectl if needed
echo "🔍 Checking required tools..."
echo ""

if ! command -v kubectl &> /dev/null; then
    echo "📦 kubectl not found - installing..."
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    chmod +x kubectl
    sudo mv kubectl /usr/local/bin/ 2>/dev/null || mv kubectl ~/.local/bin/ 2>/dev/null || {
        mkdir -p ~/bin
        mv kubectl ~/bin/
        export PATH="$HOME/bin:$PATH"
    }
    echo "✅ kubectl installed"
else
    KUBECTL_VERSION=$(kubectl version --client --short 2>/dev/null | head -1 || kubectl version --client 2>&1 | head -1)
    echo "✅ kubectl found: $KUBECTL_VERSION"
fi

# Check and install helm if needed
if ! command -v helm &> /dev/null; then
    echo "📦 helm not found - installing..."
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    echo "✅ helm installed"
else
    HELM_VERSION=$(helm version --short 2>/dev/null || echo "unknown version")
    echo "✅ helm found: $HELM_VERSION"
fi

echo ""

# Get all outputs from CloudFormation
echo "📋 Collecting CloudFormation outputs..."
OUTPUTS=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs' \
    --output json)

# Parse outputs
EKS_CLUSTER_NAME=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="EKSClusterName") | .OutputValue')
VPC_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="VPCId") | .OutputValue')
S3_BUCKET=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="S3BucketName") | .OutputValue')
REDIS_ENDPOINT=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RedisEndpoint") | .OutputValue')
REDIS_CLUSTER=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RedisClusterName") | .OutputValue')
REDIS_USER_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RedisUserId") | .OutputValue')
DB_ENDPOINT=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RDSEndpoint") | .OutputValue')
DB_PORT=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RDSPort") | .OutputValue')

# Validate DB_PORT (should be 5432 for PostgreSQL)
if [ -z "$DB_PORT" ]; then
    echo "⚠️  Warning: DB_PORT not found in CloudFormation outputs, defaulting to 5432"
    DB_PORT="5432"
elif [ "$DB_PORT" != "5432" ]; then
    echo "⚠️  Warning: DB_PORT is $DB_PORT, expected 5432 for PostgreSQL. Using $DB_PORT from CloudFormation output."
fi
COGNITO_POOL_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoUserPoolId") | .OutputValue')
COGNITO_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoClientId") | .OutputValue')
# Use a single Cognito user pool with separate app clients for chat and admin.
COGNITO_ADMIN_POOL_ID="$COGNITO_POOL_ID"
COGNITO_ADMIN_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="AdminCognitoClientId") | .OutputValue')
CORE_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CoreRoleArn") | .OutputValue')
BEDROCK_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="BedrockRoleArn") | .OutputValue')
APP_CONTROLLER_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="AppControllerRoleArn") | .OutputValue')
ALB_CONTROLLER_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="ALBControllerRoleArn") | .OutputValue')

# Get additional info from AWS
echo "🔍 Fetching additional AWS information..."

# Get ALB Security Group
ALB_SG=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="ALBSecurityGroupId") | .OutputValue')

# Get EKS Cluster Security Group ID (the one actually used by nodes)
EKS_CLUSTER_SG=$(aws eks describe-cluster \
    --name "$EKS_CLUSTER_NAME" \
    --region "$REGION" \
    --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' \
    --output text 2>/dev/null || true)

# Get Redis Security Group ID
REDIS_SG=$(aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=*redis-sg*" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)

if [ "$REDIS_SG" = "None" ] || [ "$REDIS_SG" = "null" ]; then
    REDIS_SG=""
fi

if [ "$EKS_CLUSTER_SG" = "None" ] || [ "$EKS_CLUSTER_SG" = "null" ]; then
    EKS_CLUSTER_SG=""
fi



# Get Cognito Client Secret
COGNITO_CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id "$COGNITO_POOL_ID" \
    --client-id "$COGNITO_CLIENT_ID" \
    --region "$REGION" \
    --query 'UserPoolClient.ClientSecret' \
    --output text)

COGNITO_ADMIN_CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id "$COGNITO_POOL_ID" \
    --client-id "$COGNITO_ADMIN_CLIENT_ID" \
    --region "$REGION" \
    --query 'UserPoolClient.ClientSecret' \
    --output text)

# Generate secrets
echo "🔐 Generating encryption keys..."
DIAL_API_KEY=$(openssl rand -base64 48)
CORE_ENCRYPTION_SECRET=$(openssl rand -hex 16)
CORE_ENCRYPTION_KEY=$(openssl rand -hex 16)
CHAT_NEXTAUTH_SECRET=$(openssl rand -base64 64)
COGNITO_LOGGING_SALT=$(openssl rand -hex 32)
ADMIN_NEXTAUTH_SECRET=$(openssl rand -base64 64)

# Analytics (InfluxDB) credentials (used by realtime analytics + admin backend metrics)
INFLUXDB_ORG="${INFLUXDB_ORG:-dial}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-dial-analytics}"
INFLUXDB_USER="${INFLUXDB_USER:-admin}"
INFLUXDB_PASSWORD="${INFLUXDB_PASSWORD:-$(openssl rand -base64 32)}"
INFLUXDB_TOKEN="${INFLUXDB_TOKEN:-$(openssl rand -hex 32)}"

# Grafana admin password is used for UI login (username: admin).
# Generate a 12-char password if not provided.
if [ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]; then
    GRAFANA_ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
import string

alphabet = string.ascii_letters + string.digits
while True:
    pwd = ''.join(secrets.choice(alphabet) for _ in range(12))
    if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
        print(pwd)
        break
PY
)"
fi

if [ "${#GRAFANA_ADMIN_PASSWORD}" -lt 12 ]; then
    echo "❌ Error: GRAFANA_ADMIN_PASSWORD must be at least 12 characters"
    exit 1
fi

# Get DB password from parameters.conf
DB_PASSWORD=$(grep "^DB_PASSWORD=" parameters.conf | cut -d'=' -f2- | tr -d '"')
DB_NAME="dialadmin"
DB_USER="postgres"

# Get domain from parameters.conf
DOMAIN_NAME=$(grep "^DOMAIN_NAME=" parameters.conf | cut -d'=' -f2- | tr -d '"')

# Helm release and namespace defaults (override via env if needed)
DIAL_RELEASE_NAME="${DIAL_RELEASE_NAME:-dial}"
DIAL_NAMESPACE="${DIAL_NAMESPACE:-dial}"

# Helm chart location (AWS Marketplace/ECR)
CHART_OCI_REPO="${CHART_OCI_REPO:-709825985650.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam/naya-helm-deployment}"
CHART_VERSION="${CHART_VERSION:-2026.2.1}"

# Public hosts
DIAL_PUBLIC_HOST="core.${DOMAIN_NAME}"
CHAT_PUBLIC_HOST="chat.${DOMAIN_NAME}"
THEMES_PUBLIC_HOST="themes.${DOMAIN_NAME}"
ADMIN_PUBLIC_HOST="admin.${DOMAIN_NAME}"
GRAFANA_PUBLIC_HOST="grafana.${DOMAIN_NAME}"
GRAFANA_LINK="${GRAFANA_LINK:-https://${GRAFANA_PUBLIC_HOST}}"

# Construct URLs
COGNITO_HOST="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}"
COGNITO_JWKS_URL="${COGNITO_HOST}/.well-known/jwks.json"
COGNITO_ADMIN_HOST="$COGNITO_HOST"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
APP_DOCKER_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Save outputs
echo ""
echo "💾 Saving deployment outputs..."

cat > deployment-outputs.env << EOF
# DIAL Deployment Outputs
# Generated: $(date)
# Stack: $STACK_NAME
# Region: $REGION

# AWS Infrastructure
AWS_REGION="$REGION"
AWS_ACCOUNT_ID="$AWS_ACCOUNT_ID"
EKS_CLUSTER_NAME="$EKS_CLUSTER_NAME"
VPC_ID="$VPC_ID"

# Storage
S3_BUCKET_NAME="$S3_BUCKET"

# Redis
REDIS_ENDPOINT="$REDIS_ENDPOINT"
REDIS_CLUSTER_NAME="$REDIS_CLUSTER"
REDIS_USER_ID="$REDIS_USER_ID"

# Database
DB_ENDPOINT="$DB_ENDPOINT"
DB_PORT="$DB_PORT"
DB_NAME="$DB_NAME"
DB_USER="$DB_USER"
DB_PASSWORD="$DB_PASSWORD"

# Cognito (Chat Users)
COGNITO_USER_POOL_ID="$COGNITO_POOL_ID"
COGNITO_CLIENT_ID="$COGNITO_CLIENT_ID"
COGNITO_CLIENT_SECRET="$COGNITO_CLIENT_SECRET"
COGNITO_HOST="$COGNITO_HOST"
COGNITO_JWKS_URL="$COGNITO_JWKS_URL"

# Cognito (Admin)
COGNITO_ADMIN_USER_POOL_ID="$COGNITO_ADMIN_POOL_ID"
COGNITO_ADMIN_CLIENT_ID="$COGNITO_ADMIN_CLIENT_ID"
COGNITO_ADMIN_CLIENT_SECRET="$COGNITO_ADMIN_CLIENT_SECRET"
COGNITO_ADMIN_HOST="$COGNITO_ADMIN_HOST"

# IAM Roles
CORE_SERVICE_ROLE_ARN="$CORE_ROLE_ARN"
BEDROCK_SERVICE_ROLE_ARN="$BEDROCK_ROLE_ARN"
APP_CONTROLLER_SERVICE_ROLE_ARN="$APP_CONTROLLER_ROLE_ARN"
ALB_CONTROLLER_ROLE_ARN="$ALB_CONTROLLER_ROLE_ARN"

# Security Groups
ALB_SECURITY_GROUP_ID="$ALB_SG"

# Generated Secrets
DIAL_API_KEY="$DIAL_API_KEY"
CORE_ENCRYPTION_SECRET="$CORE_ENCRYPTION_SECRET"
CORE_ENCRYPTION_KEY="$CORE_ENCRYPTION_KEY"
CHAT_NEXTAUTH_SECRET="$CHAT_NEXTAUTH_SECRET"
COGNITO_LOGGING_SALT="$COGNITO_LOGGING_SALT"
ADMIN_NEXTAUTH_SECRET="$ADMIN_NEXTAUTH_SECRET"

# Domain Configuration
DOMAIN_NAME="$DOMAIN_NAME"
DIAL_RELEASE_NAME="$DIAL_RELEASE_NAME"
DIAL_NAMESPACE="$DIAL_NAMESPACE"
DIAL_PUBLIC_HOST="$DIAL_PUBLIC_HOST"
CHAT_PUBLIC_HOST="$CHAT_PUBLIC_HOST"
THEMES_PUBLIC_HOST="$THEMES_PUBLIC_HOST"
ADMIN_PUBLIC_HOST="$ADMIN_PUBLIC_HOST"
GRAFANA_PUBLIC_HOST="$GRAFANA_PUBLIC_HOST"
GRAFANA_LINK="$GRAFANA_LINK"

# Analytics (InfluxDB)
INFLUXDB_ORG="$INFLUXDB_ORG"
INFLUXDB_BUCKET="$INFLUXDB_BUCKET"
INFLUXDB_USER="$INFLUXDB_USER"
INFLUXDB_PASSWORD="$INFLUXDB_PASSWORD"
INFLUXDB_TOKEN="$INFLUXDB_TOKEN"
GRAFANA_ADMIN_PASSWORD="$GRAFANA_ADMIN_PASSWORD"
EOF

echo "✅ Saved to: deployment-outputs.env"

# Generate Helm values with placeholders replaced
echo ""
echo "📝 Generating Helm values file..."

# Get ACM certificate ARN from parameters.conf
ACM_CERTIFICATE_ARN=$(grep "^ACM_CERTIFICATE_ARN=" parameters.conf | cut -d'=' -f2- | tr -d '"')
if [ -z "$ACM_CERTIFICATE_ARN" ]; then
    echo "❌ Error: ACM_CERTIFICATE_ARN is empty in parameters.conf"
    echo "   Please set ACM certificate ARN for *.${DOMAIN_NAME}"
    echo "   Or run: bash create-certificate.sh"
    exit 1
fi

# Validate ACM certificate is usable for this domain in current account/region
CERT_STATUS="$(aws acm describe-certificate \
    --certificate-arn "$ACM_CERTIFICATE_ARN" \
    --region "$REGION" \
    --query 'Certificate.Status' \
    --output text 2>/dev/null || true)"

if [ -z "$CERT_STATUS" ] || [ "$CERT_STATUS" = "None" ]; then
    echo "❌ Error: ACM certificate not found: $ACM_CERTIFICATE_ARN"
    echo "   Ensure ARN exists in account $(aws sts get-caller-identity --query Account --output text)"
    echo "   and region ${REGION}"
    exit 1
fi

if [ "$CERT_STATUS" != "ISSUED" ]; then
    echo "❌ Error: ACM certificate is not ISSUED (status: $CERT_STATUS)"
    echo "   Certificate ARN: $ACM_CERTIFICATE_ARN"
    exit 1
fi

WILDCARD_DOMAIN="*.${DOMAIN_NAME}"
CERT_SANS="$(aws acm describe-certificate \
    --certificate-arn "$ACM_CERTIFICATE_ARN" \
    --region "$REGION" \
    --query 'Certificate.SubjectAlternativeNames' \
    --output text 2>/dev/null || true)"

if ! printf '%s\n' "$CERT_SANS" | tr '\t' '\n' | grep -Fxq "$WILDCARD_DOMAIN"; then
    echo "❌ Error: ACM certificate does not include required SAN: ${WILDCARD_DOMAIN}"
    echo "   Certificate ARN: $ACM_CERTIFICATE_ARN"
    echo "   Current SANs: $CERT_SANS"
    echo "   Use a certificate that includes *.${DOMAIN_NAME}"
    exit 1
fi

# Export all variables for Python
export REGION VPC_ID EKS_CLUSTER_NAME ALB_CONTROLLER_ROLE_ARN CORE_ROLE_ARN
export APP_CONTROLLER_ROLE_ARN APP_DOCKER_REGISTRY
export DOMAIN_NAME DIAL_RELEASE_NAME DIAL_NAMESPACE DIAL_PUBLIC_HOST CHAT_PUBLIC_HOST THEMES_PUBLIC_HOST ADMIN_PUBLIC_HOST
export ALB_SG ACM_CERTIFICATE_ARN CORE_ENCRYPTION_KEY CORE_ENCRYPTION_SECRET
export S3_BUCKET REDIS_ENDPOINT REDIS_USER_ID REDIS_CLUSTER COGNITO_JWKS_URL COGNITO_LOGGING_SALT
export COGNITO_HOST COGNITO_CLIENT_ID COGNITO_CLIENT_SECRET CHAT_NEXTAUTH_SECRET DIAL_API_KEY
export BEDROCK_ROLE_ARN DB_ENDPOINT DB_PORT DB_NAME DB_USER DB_PASSWORD
export COGNITO_ADMIN_HOST COGNITO_ADMIN_CLIENT_ID COGNITO_ADMIN_CLIENT_SECRET ADMIN_NEXTAUTH_SECRET
export INFLUXDB_ORG INFLUXDB_BUCKET INFLUXDB_USER INFLUXDB_PASSWORD INFLUXDB_TOKEN
export GRAFANA_PUBLIC_HOST GRAFANA_LINK GRAFANA_ADMIN_PASSWORD

# Create helm values using Python to avoid YAML issues
python3 << 'PYEOF'
import json
import os
import yaml

release = os.environ.get('DIAL_RELEASE_NAME', 'dial')
namespace = os.environ.get('DIAL_NAMESPACE', 'dial')
core_service = f"{release}-core"
bedrock_service = f"{release}-bedrock"
admin_backend_service = f"{release}-admin-backend"
analytics_service = f"{release}-realtime-analytics"
influxdb_service = f"{release}-influxdb"
app_controller_service = f"{release}-app-controller"
core_service_url = f"http://{core_service}.{namespace}.svc.cluster.local"
bedrock_service_url = f"http://{bedrock_service}.{namespace}.svc.cluster.local"
admin_backend_url = f"http://{admin_backend_service}.{namespace}.svc.cluster.local/"
analytics_sink_uri = f"http://{analytics_service}.{namespace}.svc.cluster.local:80/data"
influxdb_url = f"http://{influxdb_service}.{namespace}.svc.cluster.local:8086"
app_controller_url = f"http://{app_controller_service}.{namespace}.svc.cluster.local:80"

influxdb_org = os.environ.get("INFLUXDB_ORG", "dial")
influxdb_bucket = os.environ.get("INFLUXDB_BUCKET", "dial-analytics")
influxdb_user = os.environ.get("INFLUXDB_USER", "admin")
influxdb_password = os.environ["INFLUXDB_PASSWORD"]
influxdb_token = os.environ["INFLUXDB_TOKEN"]
grafana_public_host = os.environ.get("GRAFANA_PUBLIC_HOST", "")
grafana_admin_password = os.environ["GRAFANA_ADMIN_PASSWORD"]
grafana_link = os.environ.get("GRAFANA_LINK") or (
    f"https://{grafana_public_host}" if grafana_public_host else "http://localhost:3000"
)

dial_public_host = os.environ['DIAL_PUBLIC_HOST']
chat_public_host = os.environ['CHAT_PUBLIC_HOST']
themes_public_host = os.environ['THEMES_PUBLIC_HOST']
admin_public_host = os.environ['ADMIN_PUBLIC_HOST']

aidial_config = {
    "models": {
        "anthropic.claude-sonnet-4": {
            "type": "chat",
            "displayName": "Anthropic (Claude)",
            "iconUrl": f"https://{themes_public_host}/anthropic.svg",
            "endpoint": (
                f"{bedrock_service_url}/openai/deployments/"
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0/chat/completions"
            ),
        }
    },
    "roles": {"default": {"limits": {"anthropic.claude-v1": {}}}},
}
aidial_config_json = json.dumps(aidial_config, indent=2)

helm_values = {
	    'global': {
	        'region': os.environ['REGION']
	    },
	    'albcontroller': {
	        'enabled': True,
	        'clusterName': os.environ['EKS_CLUSTER_NAME'],
	        'region': os.environ['REGION'],
	        'vpcId': os.environ['VPC_ID'],
	        # Disable Ingress validation webhook to avoid install-time race:
	        # Ingress objects can be created before the controller's webhook service has endpoints.
	        'webhookConfig': {
	            'disableIngressValidation': True
	        },
	        'serviceAccount': {
	            'create': True,
	            'name': 'aws-load-balancer-controller',
	            'annotations': {
	                'eks.amazonaws.com/role-arn': os.environ['ALB_CONTROLLER_ROLE_ARN']
	            }
	        }
	    },
	    'dial': {
	        'core': {
            'serviceAccount': {
                'create': True,
                'name': 'dial-core',
                'annotations': {
                    'eks.amazonaws.com/role-arn': os.environ['CORE_ROLE_ARN']
                }
            },
            'ingress': {
                'enabled': True,
                'ingressClassName': 'alb',
                'hosts': [dial_public_host],
                'annotations': {
                    'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
                    'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN']
                }
            },
            'configuration': {
                'encryption': {
                    'key': os.environ['CORE_ENCRYPTION_KEY'],
                    'secret': os.environ['CORE_ENCRYPTION_SECRET']
                }
            },
            'logger': {
                'enabled': True,
                'config': f"""
sources:
  aidial_logs:
    type: file
    max_line_bytes: 100000000
    oldest_first: true
    include:
      - /app/log/*.log
sinks:
  http_analytics_opensource:
    inputs:
      - aidial_logs
    type: http
    uri: {analytics_sink_uri}
    request:
      timeout_secs: 300
    batch:
      max_bytes: 1049000
      timeout_secs: 60
    encoding:
      codec: json
""".strip() + "\n"
            },
            'env': {
                'aidial.applications.controllerEndpoint': app_controller_url,
                'aidial.storage.bucket': os.environ['S3_BUCKET'],
                'aidial.redis.singleServerConfig.address': os.environ['REDIS_ENDPOINT'],
                'aidial.redis.provider.userId': os.environ['REDIS_USER_ID'],
                'aidial.redis.provider.region': os.environ['REGION'],
                'aidial.redis.provider.clusterName': os.environ['REDIS_CLUSTER'],
                'aidial.identityProviders.cognito.jwksUrl': os.environ['COGNITO_JWKS_URL'],
                'aidial.identityProviders.cognito.issuerPattern': '^https:\\/\\/cognito-idp\\.' + os.environ['REGION'] + '\\.amazonaws\\.com.+$'
            },
            'secrets': {
                'aidial.identityProviders.cognito.loggingSalt': os.environ['COGNITO_LOGGING_SALT'],
                'aidial.config.json': aidial_config_json
            }
        },
        'chat': {
            'ingress': {
                'enabled': True,
                'ingressClassName': 'alb',
                'hosts': [chat_public_host],
                'annotations': {
                    'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
                    'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN']
                }
            },
            'env': {
                'NEXTAUTH_URL': 'https://' + chat_public_host,
                'DIAL_API_HOST': core_service_url,
                'THEMES_CONFIG_HOST': 'https://' + themes_public_host
            },
            'secrets': {
                'AUTH_COGNITO_HOST': os.environ['COGNITO_HOST'],
                'AUTH_COGNITO_CLIENT_ID': os.environ['COGNITO_CLIENT_ID'],
                'AUTH_COGNITO_SECRET': os.environ['COGNITO_CLIENT_SECRET'],
                'NEXTAUTH_SECRET': os.environ['CHAT_NEXTAUTH_SECRET'],
                'DIAL_API_KEY': os.environ['DIAL_API_KEY']
            }
        },
        'themes': {
            'ingress': {
                'enabled': True,
                'ingressClassName': 'alb',
                'hosts': [themes_public_host],
                'annotations': {
                    'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
                    'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN']
                }
            }
        },
        'bedrock': {
            'env': {
                'DIAL_URL': core_service_url
            },
            'serviceAccount': {
                'create': True,
                'name': 'dial-bedrock',
                'annotations': {
                    'eks.amazonaws.com/role-arn': os.environ['BEDROCK_ROLE_ARN']
                }
            }
        }
    },
    'admin': {
        # Used by admin backend env values via tpl rendering in dial-admin chart.
        'analyticsToken': influxdb_token,
        'backend': {
            'configuration': {
                'export': {
                    'names': [core_service],
                    'namespace': namespace
                }
            }
        },
        'frontend': {
            'ingress': {
                'enabled': True,
                'ingressClassName': 'alb',
                'hosts': [admin_public_host],
                'annotations': {
                    'alb.ingress.kubernetes.io/healthcheck-path': '/health',
                    'alb.ingress.kubernetes.io/success-codes': '200-399',
                    'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
                    'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN']
                }
            },
            'env': {
                'NEXTAUTH_URL': 'https://' + admin_public_host,
                'DIAL_ADMIN_API_URL': admin_backend_url,
                'GRAFANA_LINK': grafana_link,
                'THEMES_CONFIG_URL': 'https://' + themes_public_host,
                'AUTH_COGNITO_HOST': os.environ['COGNITO_ADMIN_HOST'],
                'AUTH_COGNITO_CLIENT_ID': os.environ['COGNITO_ADMIN_CLIENT_ID'],
                'AUTH_COGNITO_SECRET': os.environ['COGNITO_ADMIN_CLIENT_SECRET'],
            },
            'secrets': {
                'NEXTAUTH_SECRET': os.environ['ADMIN_NEXTAUTH_SECRET']
            }
        },
        'externalDatabase': {
            'host': os.environ['DB_ENDPOINT'],
            'port': int(os.environ.get('DB_PORT', '5432')),  # Default to 5432 if not set
            'database': os.environ['DB_NAME'],
            'user': os.environ['DB_USER'],
            'password': os.environ['DB_PASSWORD']
        }
    },
    'influxdb': {
        'adminUser': {
            'password': influxdb_password,
            'token': influxdb_token,
        },
    },
    'realtime-analytics': {
        'env': {
            'INFLUX_API_TOKEN': influxdb_token,
        },
    },
    'rag': {
        'env': {
            'DIAL_URL': core_service_url,
            'DIAL_API_KEY': os.environ['DIAL_API_KEY'],
        },
    },
    'quickapps': {
        'env': {
            'DIAL_URL': core_service_url,
            'REMOTE_DIAL_API_KEY': os.environ['DIAL_API_KEY'],
        },
    },
    'app-controller': {
        'serviceAccount': {
            'create': True,
            'automountServiceAccountToken': True,
            'annotations': {
                'eks.amazonaws.com/role-arn': os.environ['APP_CONTROLLER_ROLE_ARN'],
            },
        },
        'env': {
            'APP_DOCKER_REGISTRY': os.environ['APP_DOCKER_REGISTRY'],
            'APP_DIAL_BASE_URL': core_service_url,
            'APP_BUILD_NAMESPACE': namespace,
            'APP_DEPLOY_NAMESPACE': namespace,
        },
    },
    'grafana': {
        # Used by Grafana datasources.yaml via tpl rendering in grafana chart.
        'analyticsToken': influxdb_token,
        'adminPassword': grafana_admin_password,
        'ingress': {
            'annotations': {
                'alb.ingress.kubernetes.io/healthcheck-path': '/api/health',
                'alb.ingress.kubernetes.io/success-codes': '200',
                'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN'],
                'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
            },
            'hosts': [grafana_public_host],
        },
    },
}

# Write YAML file
with open('helm-values.yaml', 'w') as f:
    yaml.dump(helm_values, f, default_flow_style=False, allow_unicode=True)
PYEOF

echo "✅ Saved to: helm-values.yaml"

# Execute kubectl configuration
echo ""
echo "=========================================="
echo "🔧 Configuring kubectl for EKS..."
echo "=========================================="

aws eks update-kubeconfig \
  --name "$EKS_CLUSTER_NAME" \
  --region "$REGION"

echo "✅ kubectl configured"
echo ""
echo "Verifying cluster access..."
kubectl get nodes

# Pin CoreDNS to the dedicated system node group (so core nodes are used only by application pods).
# CoreDNS is bootstrapped as a self-managed Deployment (not an EKS addon) in this setup.
echo ""
echo "=========================================="
echo "🧩 Pinning CoreDNS to system node group..."
echo "=========================================="

if kubectl -n kube-system get deploy coredns -o json | jq -e '.spec.template.spec.nodeSelector.workload == "system"' >/dev/null 2>&1; then
    echo "ℹ️  CoreDNS nodeSelector already set to workload=system"
else
    kubectl -n kube-system patch deployment coredns \
        --type merge \
        -p '{"spec":{"template":{"spec":{"nodeSelector":{"workload":"system"}}}}}' >/dev/null
    echo "✅ CoreDNS nodeSelector set to workload=system"
fi

if kubectl -n kube-system get deploy coredns -o json | jq -e '(.spec.template.spec.tolerations // []) | any(.key=="dedicated" and .value=="system" and .effect=="NoSchedule")' >/dev/null 2>&1; then
    echo "ℹ️  CoreDNS toleration for dedicated=system already present"
else
    kubectl -n kube-system patch deployment coredns \
        --type json \
        -p='[{"op":"add","path":"/spec/template/spec/tolerations/-","value":{"key":"dedicated","operator":"Equal","value":"system","effect":"NoSchedule"}}]' >/dev/null
    echo "✅ CoreDNS toleration for dedicated=system added"
fi

echo "🔄 Restarting CoreDNS to apply scheduling changes..."
kubectl -n kube-system rollout restart deployment coredns >/dev/null
kubectl -n kube-system rollout status deployment coredns --timeout=120s >/dev/null || true

# Configure StorageClass for EBS CSI (required for InfluxDB/Grafana persistence)
echo ""
echo "=========================================="
echo "💾 Configuring StorageClass (EBS CSI / gp3)..."
echo "=========================================="

# Do not gate StorageClass creation on presence of nodes.
# On fresh clusters there may be 0 nodes at this point, but we still need the StorageClass
# to exist before Helm installs PVC-based workloads.
DEFAULT_SC_NAME="$(kubectl get storageclass -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1 || true)"
if [ -n "${DEFAULT_SC_NAME}" ]; then
    echo "ℹ️  Default StorageClass already set: ${DEFAULT_SC_NAME}. Not changing default StorageClass."
    AUTO_SC_ANNOTATIONS_BLOCK=""
else
    echo "ℹ️  No default StorageClass detected. Marking auto-ebs-gp3 as default."
    AUTO_SC_ANNOTATIONS_BLOCK=$'  annotations:\n    storageclass.kubernetes.io/is-default-class: "true"\n'
fi

cat > /tmp/auto-ebs-gp3-storage-class.yaml << EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: auto-ebs-gp3
${AUTO_SC_ANNOTATIONS_BLOCK}provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
  encrypted: "true"
EOF

kubectl apply -f /tmp/auto-ebs-gp3-storage-class.yaml
echo "✅ StorageClass applied: auto-ebs-gp3"

# Install AWS Load Balancer Controller CRDs
echo ""
echo "=========================================="
echo "📦 Installing AWS Load Balancer Controller CRDs..."
echo "=========================================="

kubectl apply -f https://raw.githubusercontent.com/aws/eks-charts/master/stable/aws-load-balancer-controller/crds/crds.yaml

echo "✅ CRDs installed"

# Add Helm repo
echo ""
echo "=========================================="
echo "📚 Using OCI Helm chart..."
echo "=========================================="

if [ -z "$CHART_OCI_REPO" ]; then
    echo "❌ Error: CHART_OCI_REPO is not set"
    exit 1
fi

CHART_REGISTRY="${CHART_OCI_REPO%%/*}"
echo "🔐 Authenticating to OCI registry: ${CHART_REGISTRY}"
aws ecr get-login-password --region "$REGION" | helm registry login --username AWS --password-stdin "$CHART_REGISTRY"
echo "✅ OCI registry login complete"

# Check if DIAL is already installed and uninstall
echo ""
echo "=========================================="
echo "🔍 Checking for existing DIAL installation..."
echo "=========================================="

helm_release_exists() {
    local release="$1"
    local namespace="$2"
    helm list -n "$namespace" -q 2>/dev/null | grep -Fxq "$release"
}

alb_controller_present() {
    kubectl get deployment -A -l "app.kubernetes.io/name=aws-load-balancer-controller" --no-headers 2>/dev/null | grep -q .
}

force_clear_stuck_ingress_finalizers() {
    local release="$1"
    local namespace="$2"

    if ! kubectl get namespace "$namespace" >/dev/null 2>&1; then
        return 0
    fi

    # Only touch ingress resources that belong to this release and are already terminating.
    local stuck
    stuck="$(kubectl -n "$namespace" get ingress -l "app.kubernetes.io/instance=${release}" \
      -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)"

    if [ -z "$stuck" ]; then
        return 0
    fi

    if alb_controller_present; then
        return 0
    fi

    echo "⚠️  ALB controller is not running. Removing stuck ingress finalizers for release '${release}'..."
    while IFS= read -r ingress_name; do
        [ -z "$ingress_name" ] && continue
        kubectl -n "$namespace" patch ingress "$ingress_name" \
          --type=merge \
          -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    done <<< "$stuck"
}

wait_for_release_resources_gone() {
    local release="$1"
    local namespace="$2"

    if ! kubectl get namespace "$namespace" >/dev/null 2>&1; then
        return 0
    fi

    echo "⏳ Waiting for resources to be deleted (up to 10 minutes)..."
    local deadline=$((SECONDS + 600))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local remaining="0"

        remaining=$(kubectl -n "$namespace" get ingress -l "app.kubernetes.io/instance=${release}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        if [ "$remaining" != "0" ]; then
            force_clear_stuck_ingress_finalizers "$release" "$namespace"
            sleep 5
            continue
        fi

        remaining=$(kubectl -n "$namespace" get deploy,sts,svc,job -l "app.kubernetes.io/instance=${release}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        if [ "$remaining" != "0" ]; then
            sleep 5
            continue
        fi

        return 0
    done

    echo "⚠️  Timeout waiting for resources deletion. Continuing anyway."
}

cleanup_persistent_state() {
    # Variant A: ensure every install behaves like a first install:
    # delete PVCs + related secrets so InfluxDB/Grafana re-init with fresh tokens.
    local release="$1"
    local namespace="$2"

    if ! kubectl get namespace "$namespace" >/dev/null 2>&1; then
        return 0
    fi

    echo "🧹 Removing persistent state (PVCs/secrets) for a clean install..."

    # Best-effort: remove any leftovers in the namespace for this release.
    # This covers rare cases where a previous uninstall partially completed.
    kubectl -n "$namespace" delete ingress,svc,deploy,sts,job,cm,secret,sa,role,rolebinding,pdb,hpa \
      -l "app.kubernetes.io/instance=${release}" --ignore-not-found=true 2>/dev/null || true

    # Best-effort: delete all PVCs created by this release (covers InfluxDB/Grafana and any future PVCs).
    kubectl -n "$namespace" delete pvc -l "app.kubernetes.io/instance=${release}" --ignore-not-found=true 2>/dev/null || true

    # Fallback: delete well-known PVC names (in case labels are missing/changed).
    kubectl -n "$namespace" delete pvc "${release}-influxdb" grafana --ignore-not-found=true 2>/dev/null || true

    # Also remove known secrets that carry auth tokens (helm uninstall should delete them, but be strict).
    kubectl -n "$namespace" delete secret "${release}-influxdb-auth" dial-influxdb-auth grafana --ignore-not-found=true 2>/dev/null || true

    # Wait until resources are actually gone before reinstall (avoids reuse of old boltdb and "already exists" races).
    local deadline=$((SECONDS + 600))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local remaining="0"
        local pvc_count="0"

        remaining=$(kubectl -n "$namespace" get ingress -l "app.kubernetes.io/instance=${release}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        if [ "$remaining" != "0" ]; then
            force_clear_stuck_ingress_finalizers "$release" "$namespace"
        fi
        pvc_count=$(kubectl -n "$namespace" get pvc -l "app.kubernetes.io/instance=${release}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        if [ "$remaining" = "0" ] && [ "$pvc_count" = "0" ] \
          && ! kubectl -n "$namespace" get pvc "${release}-influxdb" grafana >/dev/null 2>&1; then
            remaining=$(kubectl -n "$namespace" get deploy,sts,svc,job -l "app.kubernetes.io/instance=${release}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
            if [ "$remaining" = "0" ]; then
                echo "✅ Namespace state cleaned for fresh install"
                return 0
            fi
        fi

        sleep 5
    done

    echo "⚠️  Timeout waiting for namespace cleanup. Continuing anyway."
}

if helm_release_exists "$DIAL_RELEASE_NAME" "$DIAL_NAMESPACE"; then
    echo "⚠️  Found existing DIAL installation in namespace '${DIAL_NAMESPACE}' - uninstalling..."
    helm uninstall "$DIAL_RELEASE_NAME" -n "$DIAL_NAMESPACE"
    echo "✅ Uninstalled existing DIAL"
    wait_for_release_resources_gone "$DIAL_RELEASE_NAME" "$DIAL_NAMESPACE"
elif helm list -q 2>/dev/null | grep -Fxq "$DIAL_RELEASE_NAME"; then
    echo "⚠️  Found existing DIAL installation in default namespace - uninstalling..."
    helm uninstall "$DIAL_RELEASE_NAME"
    echo "✅ Uninstalled existing DIAL"
    wait_for_release_resources_gone "$DIAL_RELEASE_NAME" default
else
    echo "✅ No existing installation found"
fi

# Always enforce "first install" behavior for analytics components (Variant A).
cleanup_persistent_state "$DIAL_RELEASE_NAME" "$DIAL_NAMESPACE"

# Deploy DIAL
echo ""
echo "=========================================="
echo "🚀 Deploying DIAL with Helm..."
echo "=========================================="
echo "This will take 5-10 minutes..."
echo ""

helm install "$DIAL_RELEASE_NAME" "oci://${CHART_OCI_REPO}" \
  --version "$CHART_VERSION" \
  --namespace "$DIAL_NAMESPACE" \
  --create-namespace \
  --values helm-values.yaml \
  --timeout 15m

echo ""
echo "✅ DIAL deployed successfully!"

# Wait for ALB to be provisioned
echo ""
echo "⏳ Waiting for Load Balancer to be provisioned (this takes 2-3 minutes)..."
sleep 120

# Get ALB DNS names
echo ""
echo "=========================================="
echo "🌐 DNS Configuration Required"
echo "=========================================="
echo ""

get_ingress_hostname() {
    local ingress_name="$1"
    local hostname
    hostname=$(kubectl get ingress "$ingress_name" -n "$DIAL_NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
    if [ -z "$hostname" ]; then
        echo "pending..."
    else
        echo "$hostname"
    fi
}

CHAT_ALB_DNS=$(get_ingress_hostname "${DIAL_RELEASE_NAME}-chat")
ADMIN_ALB_DNS=$(get_ingress_hostname "${DIAL_RELEASE_NAME}-admin-frontend")
THEMES_ALB_DNS=$(get_ingress_hostname "${DIAL_RELEASE_NAME}-themes")
CORE_ALB_DNS=$(get_ingress_hostname "${DIAL_RELEASE_NAME}-core")
GRAFANA_ALB_DNS=$(get_ingress_hostname "grafana")
if [ "$GRAFANA_ALB_DNS" = "pending..." ]; then
    # Fallback to release-prefixed name if fullnameOverride is not applied in the Grafana chart.
    GRAFANA_ALB_DNS=$(get_ingress_hostname "${DIAL_RELEASE_NAME}-grafana")
fi

if [ "$CHAT_ALB_DNS" = "pending..." ] || [ "$ADMIN_ALB_DNS" = "pending..." ] || [ "$THEMES_ALB_DNS" = "pending..." ] || [ "$CORE_ALB_DNS" = "pending..." ] || [ "$GRAFANA_ALB_DNS" = "pending..." ]; then
    echo "⏳ One or more Load Balancers still provisioning..."
    echo "   Run this command in a few minutes to check:"
    echo "   kubectl get ingress -n ${DIAL_NAMESPACE}"
    echo ""
fi

install_knative_with_istio() {
    echo "Installing knative-serving"
    kubectl apply -f https://github.com/knative/serving/releases/download/knative-v1.21.0/serving-crds.yaml
    kubectl apply -f https://github.com/knative/serving/releases/download/knative-v1.21.0/serving-core.yaml

    echo "Installing istioctl..."
    curl -sL https://istio.io/downloadIstioctl | sh -
    export PATH=$HOME/.istioctl/bin:$PATH

    echo "Installing istio on cluster..."
    istioctl install -y

    echo "Putting istio pods on dynamic nodegroup"
    for d in istiod istio-ingressgateway; do
            kubectl -n istio-system patch deployment $d \
            --type merge \
            -p '{"spec":{"template":{"spec":{"nodeSelector":{"workload":"dynamic"},"tolerations":[{"key":"dedicated","operator":"Equal","value":"dynamic","effect":"NoSchedule"}]}}}}'
    done

    kubectl apply -f https://github.com/knative-extensions/net-istio/releases/download/knative-v1.21.0/net-istio.yaml
    kubectl apply -f https://github.com/knative/serving/releases/download/knative-v1.21.0/serving-default-domain.yaml

    echo "Putting knative pods on system nodegroup"
    for d in controller webhook activator autoscaler net-istio-controller net-istio-webhook; do
        kubectl -n knative-serving patch deployment $d \
        --type merge \
        -p '{"spec":{"template":{"spec":{"nodeSelector":{"workload":"system"},"tolerations":[{"key":"dedicated","operator":"Equal","value":"system","effect":"NoSchedule"}]}}}}'
    done
    echo "Patching knative config..."
    kubectl patch configmap/config-autoscaler --namespace knative-serving --type merge --patch '{"data":{"allow-zero-initial-scale":"true"}}'
    kubectl patch configmap/config-network --namespace knative-serving --type merge --patch '{"data":{"ingress-class":"istio.ingress.networking.knative.dev"}}'
    kubectl -n knative-serving patch cm config-features --type merge -p '{
     "data": {
       "kubernetes.podspec-nodeselector": "enabled",
       "kubernetes.podspec-tolerations": "enabled"
     }
    }'

    kubectl -n knative-serving patch configmap config-deployment --type merge \
      -p "{\"data\":{\"registries-skipping-tag-resolving\":\"$AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com\"}}"

    echo "Enabling istio sidecar injection on knative..."
    kubectl label namespace knative-serving istio-injection=enabled

    echo "Patching App-Controller Service Account role with needed permissions..."
    kubectl apply -f app-controller-sa-role.yaml

    echo "Restarting relevant pods to set new configs..."
    kubectl rollout restart deployment autoscaler -n knative-serving
    kubectl rollout restart deployment controller -n knative-serving
    kubectl rollout restart deployment dial-app-controller -n dial
}

install_knative_with_istio

echo "📝 Add these DNS records to your domain provider:"
echo "=========================================="
echo ""
echo "Record 1:"
echo "  Type:  CNAME"
echo "  Name:  ${CHAT_PUBLIC_HOST}"
echo "  Value: ${CHAT_ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 2:"
echo "  Type:  CNAME"  
echo "  Name:  ${ADMIN_PUBLIC_HOST}"
echo "  Value: ${ADMIN_ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 3:"
echo "  Type:  CNAME"
echo "  Name:  ${THEMES_PUBLIC_HOST}"
echo "  Value: ${THEMES_ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 4:"
echo "  Type:  CNAME"
echo "  Name:  ${DIAL_PUBLIC_HOST}"
echo "  Value: ${CORE_ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 5:"
echo "  Type:  CNAME"
echo "  Name:  ${GRAFANA_PUBLIC_HOST}"
echo "  Value: ${GRAFANA_ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "=========================================="
echo ""
echo "💡 If using Route53, you can create ALIAS records instead"
echo "   (preferred over CNAME for better performance)"
echo ""

# Summary
echo ""
echo "=========================================="
echo "✅ DIAL Deployment Complete!"
echo "=========================================="
echo ""
echo "Files created:"
echo "  📄 deployment-outputs.env  - Infrastructure outputs"
echo "  📄 helm-values.yaml        - Helm values used"
echo ""
echo "⚠️  FINAL STEPS:"
echo "  1. Add the 5 DNS records shown above"
echo "  2. Wait 5-30 minutes for DNS propagation"
echo "  3. Access DIAL:"
echo "     - Chat:  https://chat.${DOMAIN_NAME}"
echo "     - Admin: https://admin.${DOMAIN_NAME}"
echo "     - Grafana: https://grafana.${DOMAIN_NAME}"
echo ""
echo "🔍 Verify deployment:"
echo "  kubectl get pods -n ${DIAL_NAMESPACE}"
echo "  kubectl get ingress -n ${DIAL_NAMESPACE}"
echo ""
echo "⚠️  SECURITY: Keep deployment-outputs.env and helm-values.yaml secure!"
echo "   They contain passwords and secrets."
echo ""

echo "Grafana credentials:"
echo "  URL: https://${GRAFANA_PUBLIC_HOST}"
echo "  Username: admin"
echo "  Password: ${GRAFANA_ADMIN_PASSWORD}"
echo "  (also saved in deployment-outputs.env as GRAFANA_ADMIN_PASSWORD)"
echo ""
