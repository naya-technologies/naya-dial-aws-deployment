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
COGNITO_ADMIN_POOL_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="AdminCognitoUserPoolId") | .OutputValue')
COGNITO_ADMIN_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="AdminCognitoClientId") | .OutputValue')
CORE_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CoreRoleArn") | .OutputValue')
BEDROCK_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="BedrockRoleArn") | .OutputValue')
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
    --user-pool-id "$COGNITO_ADMIN_POOL_ID" \
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
CHART_VERSION="${CHART_VERSION:-2026.1.8}"

# Public hosts
DIAL_PUBLIC_HOST="core.${DOMAIN_NAME}"
CHAT_PUBLIC_HOST="chat.${DOMAIN_NAME}"
THEMES_PUBLIC_HOST="themes.${DOMAIN_NAME}"
ADMIN_PUBLIC_HOST="admin.${DOMAIN_NAME}"

# Construct URLs
COGNITO_HOST="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}"
COGNITO_JWKS_URL="${COGNITO_HOST}/.well-known/jwks.json"
COGNITO_ADMIN_HOST="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_ADMIN_POOL_ID}"

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
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
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
EOF

echo "✅ Saved to: deployment-outputs.env"

# Generate Helm values with placeholders replaced
echo ""
echo "📝 Generating Helm values file..."

# Get ACM certificate ARN from parameters.conf
ACM_CERTIFICATE_ARN=$(grep "^ACM_CERTIFICATE_ARN=" parameters.conf | cut -d'=' -f2- | tr -d '"')
if [ -z "$ACM_CERTIFICATE_ARN" ]; then
    echo "❌ Error: ACM_CERTIFICATE_ARN not found in parameters.conf"
    echo "   Please run: bash create-certificate.sh"
    exit 1
fi

# Export all variables for Python
export REGION VPC_ID EKS_CLUSTER_NAME ALB_CONTROLLER_ROLE_ARN CORE_ROLE_ARN
export DOMAIN_NAME DIAL_RELEASE_NAME DIAL_NAMESPACE DIAL_PUBLIC_HOST CHAT_PUBLIC_HOST THEMES_PUBLIC_HOST ADMIN_PUBLIC_HOST
export ALB_SG ACM_CERTIFICATE_ARN CORE_ENCRYPTION_KEY CORE_ENCRYPTION_SECRET
export S3_BUCKET REDIS_ENDPOINT REDIS_USER_ID REDIS_CLUSTER COGNITO_JWKS_URL COGNITO_LOGGING_SALT
export COGNITO_HOST COGNITO_CLIENT_ID COGNITO_CLIENT_SECRET CHAT_NEXTAUTH_SECRET DIAL_API_KEY
export BEDROCK_ROLE_ARN DB_ENDPOINT DB_PORT DB_NAME DB_USER DB_PASSWORD
export COGNITO_ADMIN_HOST COGNITO_ADMIN_CLIENT_ID COGNITO_ADMIN_CLIENT_SECRET ADMIN_NEXTAUTH_SECRET

# Create helm values using Python to avoid YAML issues
python3 << 'PYEOF'
import os
import yaml

release = os.environ.get('DIAL_RELEASE_NAME', 'dial')
namespace = os.environ.get('DIAL_NAMESPACE', 'dial')
core_service = f"{release}-core"
admin_backend_service = f"{release}-admin-backend"
core_service_url = f"http://{core_service}.{namespace}.svc.cluster.local"
admin_backend_url = f"http://{admin_backend_service}.{namespace}.svc.cluster.local/"

dial_public_host = os.environ['DIAL_PUBLIC_HOST']
chat_public_host = os.environ['CHAT_PUBLIC_HOST']
themes_public_host = os.environ['THEMES_PUBLIC_HOST']
admin_public_host = os.environ['ADMIN_PUBLIC_HOST']

helm_values = {
    'global': {
        'region': os.environ['REGION']
    },
    'albcontroller': {
        'enabled': True,
        'clusterName': os.environ['EKS_CLUSTER_NAME'],
        'region': os.environ['REGION'],
        'vpcId': os.environ['VPC_ID'],
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
            'env': {
                'aidial.storage.bucket': os.environ['S3_BUCKET'],
                'aidial.redis.singleServerConfig.address': os.environ['REDIS_ENDPOINT'],
                'aidial.redis.provider.userId': os.environ['REDIS_USER_ID'],
                'aidial.redis.provider.region': os.environ['REGION'],
                'aidial.redis.provider.clusterName': os.environ['REDIS_CLUSTER'],
                'aidial.identityProviders.cognito.jwksUrl': os.environ['COGNITO_JWKS_URL'],
                'aidial.identityProviders.cognito.issuerPattern': '^https:\\/\\/cognito-idp\\.' + os.environ['REGION'] + '\\.amazonaws\\.com.+$'
            },
            'secrets': {
                'aidial.identityProviders.cognito.loggingSalt': os.environ['COGNITO_LOGGING_SALT']
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
        'backend': {
            'env': {
                'CORE_CLIENT_URL': core_service_url
            },
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
                    'alb.ingress.kubernetes.io/security-groups': os.environ['ALB_SG'],
                    'alb.ingress.kubernetes.io/certificate-arn': os.environ['ACM_CERTIFICATE_ARN']
                }
            },
            'env': {
                'NEXTAUTH_URL': 'https://' + admin_public_host,
                'DIAL_ADMIN_API_URL': admin_backend_url,
                'AUTH_COGNITO_HOST': os.environ['COGNITO_ADMIN_HOST'],
                'AUTH_COGNITO_CLIENT_ID': os.environ['COGNITO_ADMIN_CLIENT_ID']
            },
            'secrets': {
                'AUTH_COGNITO_SECRET': os.environ['COGNITO_ADMIN_CLIENT_SECRET'],
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
    }
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

if helm list -n "$DIAL_NAMESPACE" 2>/dev/null | grep -q "$DIAL_RELEASE_NAME"; then
    echo "⚠️  Found existing DIAL installation - uninstalling..."
    helm uninstall "$DIAL_RELEASE_NAME" -n "$DIAL_NAMESPACE"
    echo "✅ Uninstalled existing DIAL"
    echo "⏳ Waiting for cleanup (30 seconds)..."
    sleep 30
elif helm list 2>/dev/null | grep -q "$DIAL_RELEASE_NAME"; then
    echo "⚠️  Found existing DIAL installation in default namespace - uninstalling..."
    helm uninstall "$DIAL_RELEASE_NAME"
    echo "✅ Uninstalled existing DIAL"
    echo "⏳ Waiting for cleanup (30 seconds)..."
    sleep 30
else
    echo "✅ No existing installation found"
fi

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

ALB_DNS=$(kubectl get ingress "${DIAL_RELEASE_NAME}-chat" -n "$DIAL_NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo "pending...")

if [ "$ALB_DNS" = "pending..." ] || [ -z "$ALB_DNS" ]; then
    echo "⏳ Load Balancer still provisioning..."
    echo "   Run this command in a few minutes to get the ALB DNS:"
    echo "   kubectl get ingress -n ${DIAL_NAMESPACE}"
    echo ""
    echo "Then add DNS records pointing to the ALB DNS name."
else
    echo "✅ Application Load Balancer DNS:"
    echo "   $ALB_DNS"
    echo ""
fi

echo "📝 Add these DNS records to your domain provider:"
echo "=========================================="
echo ""
echo "Record 1:"
echo "  Type:  CNAME"
echo "  Name:  ${CHAT_PUBLIC_HOST}"
echo "  Value: ${ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 2:"
echo "  Type:  CNAME"  
echo "  Name:  ${ADMIN_PUBLIC_HOST}"
echo "  Value: ${ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 3:"
echo "  Type:  CNAME"
echo "  Name:  ${THEMES_PUBLIC_HOST}"
echo "  Value: ${ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 4:"
echo "  Type:  CNAME"
echo "  Name:  ${DIAL_PUBLIC_HOST}"
echo "  Value: ${ALB_DNS}"
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
echo "  1. Add the 4 DNS records shown above"
echo "  2. Wait 5-30 minutes for DNS propagation"
echo "  3. Access DIAL:"
echo "     - Chat:  https://chat.${DOMAIN_NAME}"
echo "     - Admin: https://admin.${DOMAIN_NAME}"
echo ""
echo "🔍 Verify deployment:"
echo "  kubectl get pods -n ${DIAL_NAMESPACE}"
echo "  kubectl get ingress -n ${DIAL_NAMESPACE}"
echo ""
echo "⚠️  SECURITY: Keep deployment-outputs.env and helm-values.yaml secure!"
echo "   They contain passwords and secrets."
echo ""
