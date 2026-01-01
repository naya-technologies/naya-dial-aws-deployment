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
COGNITO_POOL_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoUserPoolId") | .OutputValue')
COGNITO_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoClientId") | .OutputValue')
COGNITO_ADMIN_POOL_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoAdminUserPoolId") | .OutputValue')
COGNITO_ADMIN_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CognitoAdminClientId") | .OutputValue')
CORE_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="CoreRoleArn") | .OutputValue')
BEDROCK_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="BedrockRoleArn") | .OutputValue')
ALB_CONTROLLER_ROLE_ARN=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="ALBControllerRoleArn") | .OutputValue')

# Get additional info from AWS
echo "🔍 Fetching additional AWS information..."

# Get ALB Security Group
ALB_SG=$(aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=*ALBSecurityGroup*" \
    --query 'SecurityGroups[0].GroupId' \
    --output text)

# Get Cognito Domain
COGNITO_DOMAIN=$(aws cognito-idp describe-user-pool \
    --user-pool-id "$COGNITO_POOL_ID" \
    --region "$REGION" \
    --query 'UserPool.Domain' \
    --output text 2>/dev/null || echo "")

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
DIAL_PUBLIC_HOST="chat.${DOMAIN_NAME}"
THEMES_PUBLIC_HOST="themes.${DOMAIN_NAME}"
ADMIN_PUBLIC_HOST="admin.${DOMAIN_NAME}"
EOF

echo "✅ Saved to: deployment-outputs.env"

# Generate Helm values with placeholders replaced
echo ""
echo "📝 Generating Helm values file..."

# Note: You'll need to create ACM certificate manually
# Get ACM certificate ARN from parameters.conf
ACM_CERTIFICATE_ARN=$(grep "^ACM_CERTIFICATE_ARN=" parameters.conf | cut -d'=' -f2- | tr -d '"')
if [ -z "$ACM_CERTIFICATE_ARN" ]; then
    echo "❌ Error: ACM_CERTIFICATE_ARN not found in parameters.conf"
    echo "   Please run: bash create-certificate.sh"
    exit 1
fi

cat > helm-values.yaml << EOF
# DIAL Helm Values
# Auto-generated from CloudFormation deployment
# Generated: $(date)

global:
  region: ${REGION}

albcontroller:
  enabled: true
  clusterName: ${EKS_CLUSTER_NAME}
  region: ${REGION}
  vpcId: ${VPC_ID}
  serviceAccount:
    create: true
    name: aws-load-balancer-controller
    annotations:
      eks.amazonaws.com/role-arn: ${ALB_CONTROLLER_ROLE_ARN}

dial:
  core:
    serviceAccount:
      create: true
      name: dial-core
      annotations:
        eks.amazonaws.com/role-arn: ${CORE_SERVICE_ROLE_ARN}
    
    ingress:
      enabled: true
      className: alb
      hosts:
        - chat.${DOMAIN_NAME}
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/security-groups: ${ALB_SG}
        alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERTIFICATE_ARN}
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
    
    configuration:
      encryption:
        key: ${CORE_ENCRYPTION_KEY}
        secret: ${CORE_ENCRYPTION_SECRET}
    
    env:
      aidial:
        storage:
          bucket: ${S3_BUCKET}
        redis:
          singleServerConfig:
            address: redis://${REDIS_ENDPOINT}
          provider:
            userId: ${REDIS_USER_ID}
            clusterName: ${REDIS_CLUSTER}
        identityProviders:
          cognito:
            jwksUrl: ${COGNITO_JWKS_URL}
    
    secrets:
      aidial:
        identityProviders:
          cognito:
            loggingSalt: ${COGNITO_LOGGING_SALT}

  chat:
    ingress:
      enabled: true
      className: alb
      hosts:
        - chat.${DOMAIN_NAME}
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/security-groups: ${ALB_SG}
        alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERTIFICATE_ARN}
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
    
    env:
      NEXTAUTH_URL: https://chat.${DOMAIN_NAME}
      THEMES_CONFIG_HOST: https://themes.${DOMAIN_NAME}
    
    secrets:
      AUTH_COGNITO_HOST: ${COGNITO_HOST}
      AUTH_COGNITO_CLIENT_ID: ${COGNITO_CLIENT_ID}
      AUTH_COGNITO_SECRET: ${COGNITO_CLIENT_SECRET}
      NEXTAUTH_SECRET: ${CHAT_NEXTAUTH_SECRET}
      DIAL_API_KEY: ${DIAL_API_KEY}

  themes:
    ingress:
      enabled: true
      className: alb
      hosts:
        - themes.${DOMAIN_NAME}
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/security-groups: ${ALB_SG}
        alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERTIFICATE_ARN}

  bedrock:
    serviceAccount:
      create: true
      name: dial-bedrock
      annotations:
        eks.amazonaws.com/role-arn: ${BEDROCK_SERVICE_ROLE_ARN}

dialadmin:
  backend:
    externalDatabase:
      enabled: true
      host: ${DB_ENDPOINT}
      port: ${DB_PORT}
      database: ${DB_NAME}
      user: ${DB_USER}
      password: ${DB_PASSWORD}
  
  frontend:
    ingress:
      enabled: true
      className: alb
      hosts:
        - admin.${DOMAIN_NAME}
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/security-groups: ${ALB_SG}
        alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERTIFICATE_ARN}
    
    env:
      NEXTAUTH_URL: https://admin.${DOMAIN_NAME}
      AUTH_COGNITO_HOST: ${COGNITO_ADMIN_HOST}
      AUTH_COGNITO_CLIENT_ID: ${COGNITO_ADMIN_CLIENT_ID}
    
    secrets:
      AUTH_COGNITO_SECRET: ${COGNITO_ADMIN_CLIENT_SECRET}
      NEXTAUTH_SECRET: ${ADMIN_NEXTAUTH_SECRET}
EOF

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
echo "📚 Adding DIAL Helm repository..."
echo "=========================================="

helm repo add epam https://charts.epam.com 2>/dev/null || echo "Repository already exists"
helm repo update

echo "✅ Helm repository ready"

# Deploy DIAL
echo ""
echo "=========================================="
echo "🚀 Deploying DIAL with Helm..."
echo "=========================================="
echo "This will take 5-10 minutes..."
echo ""

helm install dial epam/dial \
  --namespace dial \
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

ALB_DNS=$(kubectl get ingress dial-chat -n dial -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo "pending...")

if [ "$ALB_DNS" = "pending..." ] || [ -z "$ALB_DNS" ]; then
    echo "⏳ Load Balancer still provisioning..."
    echo "   Run this command in a few minutes to get the ALB DNS:"
    echo "   kubectl get ingress -n dial"
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
echo "  Name:  chat.${DOMAIN_NAME}"
echo "  Value: ${ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 2:"
echo "  Type:  CNAME"  
echo "  Name:  admin.${DOMAIN_NAME}"
echo "  Value: ${ALB_DNS}"
echo "  TTL:   300"
echo ""
echo "Record 3:"
echo "  Type:  CNAME"
echo "  Name:  themes.${DOMAIN_NAME}"
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
echo "  1. Add the 3 DNS records shown above"
echo "  2. Wait 5-30 minutes for DNS propagation"
echo "  3. Access DIAL:"
echo "     - Chat:  https://chat.${DOMAIN_NAME}"
echo "     - Admin: https://admin.${DOMAIN_NAME}"
echo ""
echo "🔍 Verify deployment:"
echo "  kubectl get pods -n dial"
echo "  kubectl get ingress -n dial"
echo ""
echo "⚠️  SECURITY: Keep deployment-outputs.env and helm-values.yaml secure!"
echo "   They contain passwords and secrets."
echo ""

