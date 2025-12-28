#!/bin/bash

###############################################################################
# DIAL Installation Script
# This script will deploy DIAL on your AWS account
# 
# Prerequisites:
# 1. You must be logged into AWS CloudShell with admin permissions
# 2. You must have edited the parameters.conf file with your values
#
# Usage:
#   bash install.sh
###############################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

###############################################################################
# Helper Functions
###############################################################################

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ Error: $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ Warning: $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

check_command() {
    if ! command -v $1 &> /dev/null; then
        print_error "$1 is not installed or not in PATH"
        exit 1
    fi
}

###############################################################################
# Step 0: Run Prerequisites Check
###############################################################################

print_header "Step 0: Running Prerequisites Check"

if [ -f "${SCRIPT_DIR}/check-prerequisites.sh" ]; then
    print_info "Running pre-installation checks..."
    bash "${SCRIPT_DIR}/check-prerequisites.sh"
    
    if [ $? -ne 0 ]; then
        print_error "Prerequisites check failed. Please fix the issues above and try again."
        exit 1
    fi
    
    echo ""
    read -p "Prerequisites check passed. Continue with installation? (yes/no): " PREREQ_CONFIRM
    if [ "$PREREQ_CONFIRM" != "yes" ]; then
        print_warning "Installation cancelled by user"
        exit 0
    fi
else
    print_warning "check-prerequisites.sh not found, skipping pre-checks"
fi

###############################################################################
# Step 1: Load and Validate Parameters
###############################################################################

print_header "Step 1: Loading Configuration"

if [ ! -f "${SCRIPT_DIR}/parameters.conf" ]; then
    print_error "parameters.conf file not found!"
    echo "Please make sure parameters.conf exists in the same directory as this script."
    exit 1
fi

# Load parameters
source "${SCRIPT_DIR}/parameters.conf"

print_info "Validating required parameters..."

# Validate required parameters
if [ -z "$DOMAIN_NAME" ]; then
    print_error "DOMAIN_NAME is not set in parameters.conf"
    exit 1
fi

if [ -z "$DB_PASSWORD" ]; then
    print_error "DB_PASSWORD is not set in parameters.conf"
    exit 1
fi

if [ ${#DB_PASSWORD} -lt 8 ]; then
    print_error "DB_PASSWORD must be at least 8 characters long"
    exit 1
fi

if [ -z "$ADMIN_EMAIL" ]; then
    print_error "ADMIN_EMAIL is not set in parameters.conf"
    exit 1
fi

# Validate email format
if [[ ! "$ADMIN_EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
    print_error "ADMIN_EMAIL is not a valid email address"
    exit 1
fi

# Set defaults for optional parameters
AWS_REGION=${AWS_REGION:-us-east-2}
STACK_NAME=${STACK_NAME:-dial-production}
EKS_CLUSTER_NAME=${EKS_CLUSTER_NAME:-dial-cluster}
CERTIFICATE_OPTION=${CERTIFICATE_OPTION:-auto}
COGNITO_USER_POOL_NAME=${COGNITO_USER_POOL_NAME:-dial-users}
ALLOW_SELF_REGISTRATION=${ALLOW_SELF_REGISTRATION:-no}

print_success "Configuration loaded and validated"

# Display configuration summary
echo ""
echo "Configuration Summary:"
echo "  Domain: ${DOMAIN_NAME}"
echo "  Region: ${AWS_REGION}"
echo "  Stack Name: ${STACK_NAME}"
echo "  EKS Cluster: ${EKS_CLUSTER_NAME}"
echo "  Admin Email: ${ADMIN_EMAIL}"
echo "  Certificate: ${CERTIFICATE_OPTION}"
echo ""

read -p "Continue with installation? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    print_warning "Installation cancelled by user"
    exit 0
fi

###############################################################################
# Step 2: Check Prerequisites
###############################################################################

print_header "Step 2: Checking Prerequisites"

print_info "Checking AWS CLI..."
check_command aws
AWS_CLI_VERSION=$(aws --version)
print_success "AWS CLI found: ${AWS_CLI_VERSION}"

print_info "Checking AWS credentials..."
if ! aws sts get-caller-identity &> /dev/null; then
    print_error "AWS credentials not configured or invalid"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
print_success "AWS Account ID: ${ACCOUNT_ID}"

print_info "Checking kubectl..."
if ! command -v kubectl &> /dev/null; then
    print_warning "kubectl not found, installing..."
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    chmod +x kubectl
    sudo mv kubectl /usr/local/bin/ 2>/dev/null || mv kubectl ~/bin/
    print_success "kubectl installed"
else
    print_success "kubectl found"
fi

print_info "Checking helm..."
if ! command -v helm &> /dev/null; then
    print_warning "helm not found, installing..."
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    print_success "helm installed"
else
    print_success "helm found"
fi

###############################################################################
# Step 3: Prepare CloudFormation Templates
###############################################################################

print_header "Step 3: Preparing CloudFormation Templates"

# Create S3 bucket for templates
TEMPLATES_BUCKET="${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
print_info "Creating S3 bucket for templates: ${TEMPLATES_BUCKET}"

if aws s3 ls "s3://${TEMPLATES_BUCKET}" --region ${AWS_REGION} 2>&1 | grep -q 'NoSuchBucket'; then
    aws s3 mb "s3://${TEMPLATES_BUCKET}" --region ${AWS_REGION}
    print_success "Bucket created"
else
    print_success "Bucket already exists"
fi

# Upload templates to S3
print_info "Uploading CloudFormation templates to S3..."
for template in dial-vpc.yaml dial-eks.yaml dial-iam.yaml dial-storage.yaml dial-cache.yaml dial-rds.yaml dial-cognito.yaml; do
    if [ -f "${SCRIPT_DIR}/cloudformation/${template}" ]; then
        aws s3 cp "${SCRIPT_DIR}/cloudformation/${template}" "s3://${TEMPLATES_BUCKET}/"
        print_success "Uploaded ${template}"
    else
        print_error "${template} not found in cloudformation/ directory"
        exit 1
    fi
done

# Update main template with bucket name
sed "s|\${TemplatesBucket}|${TEMPLATES_BUCKET}|g" "${SCRIPT_DIR}/cloudformation/dial-main.yaml" > "/tmp/dial-main-updated.yaml"
print_success "Templates prepared"

###############################################################################
# Step 4: Deploy CloudFormation Stack
###############################################################################

print_header "Step 4: Deploying CloudFormation Stack"

print_info "This will take approximately 25-35 minutes..."
print_info "Stack Name: ${STACK_NAME}"

# Build parameters
CREATE_CERT="true"
if [ "$CERTIFICATE_OPTION" == "existing" ]; then
    CREATE_CERT="false"
fi

DISABLE_SELF_REG="true"
if [ "$ALLOW_SELF_REGISTRATION" == "yes" ]; then
    DISABLE_SELF_REG="false"
fi

PARAMS="ParameterKey=DomainName,ParameterValue=${DOMAIN_NAME}"
PARAMS="${PARAMS} ParameterKey=EKSClusterName,ParameterValue=${EKS_CLUSTER_NAME}"
PARAMS="${PARAMS} ParameterKey=DBMasterPassword,ParameterValue=${DB_PASSWORD}"
PARAMS="${PARAMS} ParameterKey=CreateACMCertificate,ParameterValue=${CREATE_CERT}"
PARAMS="${PARAMS} ParameterKey=CognitoUserPoolName,ParameterValue=${COGNITO_USER_POOL_NAME}"
PARAMS="${PARAMS} ParameterKey=CognitoAdminUserPoolName,ParameterValue=${COGNITO_ADMIN_USER_POOL_NAME:-dial-admins}"
PARAMS="${PARAMS} ParameterKey=DisableSelfRegistration,ParameterValue=${DISABLE_SELF_REG}"
PARAMS="${PARAMS} ParameterKey=VPCCidr,ParameterValue=${VPC_CIDR:-10.0.0.0/16}"
PARAMS="${PARAMS} ParameterKey=PublicSubnet1Cidr,ParameterValue=${PUBLIC_SUBNET_1_CIDR:-10.0.1.0/24}"
PARAMS="${PARAMS} ParameterKey=PublicSubnet2Cidr,ParameterValue=${PUBLIC_SUBNET_2_CIDR:-10.0.2.0/24}"
PARAMS="${PARAMS} ParameterKey=PrivateSubnet1Cidr,ParameterValue=${PRIVATE_SUBNET_1_CIDR:-10.0.10.0/24}"
PARAMS="${PARAMS} ParameterKey=PrivateSubnet2Cidr,ParameterValue=${PRIVATE_SUBNET_2_CIDR:-10.0.11.0/24}"

if [ "$CERTIFICATE_OPTION" == "existing" ] && [ -n "$EXISTING_CERTIFICATE_ARN" ]; then
    PARAMS="${PARAMS} ParameterKey=ExistingCertificateArn,ParameterValue=${EXISTING_CERTIFICATE_ARN}"
fi

# Check if stack already exists
if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    print_warning "Stack ${STACK_NAME} already exists"
    read -p "Do you want to update it? (yes/no): " UPDATE_CONFIRM
    if [ "$UPDATE_CONFIRM" == "yes" ]; then
        print_info "Updating stack..."
        aws cloudformation update-stack \
            --stack-name ${STACK_NAME} \
            --template-body file:///tmp/dial-main-updated.yaml \
            --parameters ${PARAMS} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region ${AWS_REGION}
        
        print_info "Waiting for stack update to complete..."
        aws cloudformation wait stack-update-complete \
            --stack-name ${STACK_NAME} \
            --region ${AWS_REGION}
    else
        print_error "Cannot proceed - stack already exists"
        exit 1
    fi
else
    # Create new stack
    print_info "Creating stack..."
    aws cloudformation create-stack \
        --stack-name ${STACK_NAME} \
        --template-body file:///tmp/dial-main-updated.yaml \
        --parameters ${PARAMS} \
        --capabilities CAPABILITY_NAMED_IAM \
        --region ${AWS_REGION}

    print_success "Stack creation initiated"
    print_info "Waiting for stack creation to complete (this will take 25-35 minutes)..."
    
    # Wait with progress indicator
    aws cloudformation wait stack-create-complete \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION}
fi

print_success "CloudFormation stack deployed successfully!"

###############################################################################
# Step 5: Configure kubectl for EKS
###############################################################################

print_header "Step 5: Configuring kubectl"

print_info "Updating kubeconfig for EKS cluster..."
aws eks update-kubeconfig --name ${EKS_CLUSTER_NAME} --region ${AWS_REGION}
print_success "kubectl configured"

# Test connection
print_info "Testing connection to EKS cluster..."
if kubectl get nodes &> /dev/null; then
    print_success "Successfully connected to EKS cluster"
    kubectl get nodes
else
    print_error "Failed to connect to EKS cluster"
    exit 1
fi

###############################################################################
# Step 6: Install AWS Load Balancer Controller CRDs
###############################################################################

print_header "Step 6: Installing AWS Load Balancer Controller"

print_info "Installing CRDs..."
kubectl apply -f https://raw.githubusercontent.com/aws/eks-charts/master/stable/aws-load-balancer-controller/crds/crds.yaml
print_success "CRDs installed"

###############################################################################
# Step 7: Generate Helm Values
###############################################################################

print_header "Step 7: Generating Helm Configuration"

print_info "Extracting CloudFormation outputs..."

# Get stack outputs
get_output() {
    aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region ${AWS_REGION} \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
        --output text
}

# Get Cognito Client Secret (Chat Users)
USER_POOL_ID=$(get_output "CognitoUserPoolId")
CLIENT_ID=$(get_output "CognitoClientId")
CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id "$USER_POOL_ID" \
    --client-id "$CLIENT_ID" \
    --region ${AWS_REGION} \
    --query 'UserPoolClient.ClientSecret' \
    --output text)

# Get Admin Cognito Client Secret
ADMIN_USER_POOL_ID=$(get_output "AdminCognitoUserPoolId")
ADMIN_CLIENT_ID=$(get_output "AdminCognitoClientId")
ADMIN_CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id "$ADMIN_USER_POOL_ID" \
    --client-id "$ADMIN_CLIENT_ID" \
    --region ${AWS_REGION} \
    --query 'UserPoolClient.ClientSecret' \
    --output text)

# Generate encryption keys
print_info "Generating encryption keys..."
DIAL_API_KEY=$(openssl rand -base64 48)
CORE_ENCRYPTION_SECRET=$(openssl rand -hex 16)
CORE_ENCRYPTION_KEY=$(openssl rand -hex 16)
CHAT_NEXTAUTH_SECRET=$(openssl rand -base64 64)
COGNITO_LOGGING_SALT=$(openssl rand -hex 32)
ADMIN_NEXTAUTH_SECRET=$(openssl rand -base64 64)

# Extract Redis endpoint
REDIS_ENDPOINT=$(get_output "RedisEndpoint")

# Generate Helm values file
print_info "Creating Helm values file..."
cat > "${SCRIPT_DIR}/helm-values.yaml" <<EOF
# Auto-generated Helm values for DIAL
# Generated on: $(date)
# Stack: ${STACK_NAME}

albcontroller:
  enabled: true
  clusterName: $(get_output "EKSClusterName")
  region: ${AWS_REGION}
  vpcId: $(get_output "VPCId")
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: $(get_output "ALBControllerRoleArn")

dial:
  core:
    serviceAccount:
      annotations:
        eks.amazonaws.com/role-arn: $(get_output "CoreRoleArn")
    
    ingress:
      enabled: true
      className: alb
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/certificate-arn: $(get_output "CertificateArn")
        alb.ingress.kubernetes.io/security-groups: $(get_output "ALBSecurityGroupId")
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
      hosts:
        - core.${DOMAIN_NAME}
    
    configuration:
      encryption:
        key: ${CORE_ENCRYPTION_KEY}
        secret: ${CORE_ENCRYPTION_SECRET}
    
    env:
      aidial:
        storage:
          bucket: $(get_output "S3BucketName")
          region: ${AWS_REGION}
        
        redis:
          singleServerConfig:
            address: ${REDIS_ENDPOINT}
          provider:
            userId: $(get_output "RedisUserId")
            clusterName: $(get_output "RedisClusterName")
        
        identityProviders:
          cognito:
            jwksUrl: https://cognito-idp.${AWS_REGION}.amazonaws.com/${USER_POOL_ID}/.well-known/jwks.json
    
    secrets:
      aidial:
        identityProviders:
          cognito:
            loggingSalt: ${COGNITO_LOGGING_SALT}
  
  bedrock:
    enabled: true
    serviceAccount:
      annotations:
        eks.amazonaws.com/role-arn: $(get_output "BedrockRoleArn")
  
  chat:
    ingress:
      enabled: true
      className: alb
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/certificate-arn: $(get_output "CertificateArn")
        alb.ingress.kubernetes.io/security-groups: $(get_output "ALBSecurityGroupId")
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
      hosts:
        - chat.${DOMAIN_NAME}
    
    env:
      NEXTAUTH_URL: https://chat.${DOMAIN_NAME}
      THEMES_CONFIG_HOST: https://themes.${DOMAIN_NAME}
    
    secrets:
      AUTH_COGNITO_HOST: https://cognito-idp.${AWS_REGION}.amazonaws.com/${USER_POOL_ID}
      AUTH_COGNITO_CLIENT_ID: ${CLIENT_ID}
      AUTH_COGNITO_SECRET: ${CLIENT_SECRET}
      NEXTAUTH_SECRET: ${CHAT_NEXTAUTH_SECRET}
      DIAL_API_KEY: ${DIAL_API_KEY}
  
  themes:
    ingress:
      enabled: true
      className: alb
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/certificate-arn: $(get_output "CertificateArn")
        alb.ingress.kubernetes.io/security-groups: $(get_output "ALBSecurityGroupId")
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
      hosts:
        - themes.${DOMAIN_NAME}

dialadmin:
  backend:
    externalDatabase:
      enabled: true
      host: $(get_output "RDSEndpoint")
      port: $(get_output "RDSPort")
      database: dialadmin
      user: postgres
      password: ${DB_PASSWORD}
  
  frontend:
    ingress:
      enabled: true
      className: alb
      annotations:
        alb.ingress.kubernetes.io/scheme: internet-facing
        alb.ingress.kubernetes.io/target-type: ip
        alb.ingress.kubernetes.io/certificate-arn: $(get_output "CertificateArn")
        alb.ingress.kubernetes.io/security-groups: $(get_output "ALBSecurityGroupId")
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
        alb.ingress.kubernetes.io/ssl-redirect: '443'
      hosts:
        - admin.${DOMAIN_NAME}
    
    env:
      NEXTAUTH_URL: https://admin.${DOMAIN_NAME}
      AUTH_COGNITO_HOST: https://cognito-idp.${AWS_REGION}.amazonaws.com/${ADMIN_USER_POOL_ID}
      AUTH_COGNITO_CLIENT_ID: ${ADMIN_CLIENT_ID}
    
    secrets:
      NEXTAUTH_SECRET: ${ADMIN_NEXTAUTH_SECRET}
      AUTH_COGNITO_SECRET: ${ADMIN_CLIENT_SECRET}
EOF

print_success "Helm values file created: helm-values.yaml"

###############################################################################
# Step 8: Save Installation Info
###############################################################################

print_info "Saving installation information..."

cat > "${SCRIPT_DIR}/installation-info.txt" <<EOF
DIAL Installation Information
========================================
Installation Date: $(date)
Stack Name: ${STACK_NAME}
AWS Region: ${AWS_REGION}
AWS Account: ${ACCOUNT_ID}
Domain: ${DOMAIN_NAME}

CloudFormation Stack Outputs:
$(aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} --query 'Stacks[0].Outputs' --output table)

Next Steps:
1. Install DIAL using Helm (see instructions below)
2. Configure DNS records
3. Create admin user in Cognito
4. Access DIAL at https://chat.${DOMAIN_NAME}

Files Created:
- helm-values.yaml (Helm configuration - DO NOT SHARE, contains secrets)
- installation-info.txt (this file)
EOF

print_success "Installation information saved to installation-info.txt"

###############################################################################
# Done!
###############################################################################

print_header "Installation Complete!"

echo ""
echo -e "${GREEN}✓ CloudFormation infrastructure deployed${NC}"
echo -e "${GREEN}✓ EKS cluster configured${NC}"
echo -e "${GREEN}✓ Helm values generated${NC}"
echo ""
echo -e "${YELLOW}⚠ IMPORTANT: The helm-values.yaml file contains sensitive information.${NC}"
echo -e "${YELLOW}   Keep it secure and do not share it publicly.${NC}"
echo ""
echo -e "${BLUE}Next steps are saved in: next-steps.sh${NC}"
echo -e "${BLUE}Run: bash next-steps.sh${NC}"
echo ""

# Create next-steps script
cat > "${SCRIPT_DIR}/next-steps.sh" <<'NEXTSCRIPT'
#!/bin/bash
source parameters.conf
echo "Remaining manual steps:"
echo ""
echo "The infrastructure has been created. Now you need to:"
echo ""
echo "1. Install DIAL using Helm:"
echo "   You need to obtain the DIAL Helm chart from your DIAL provider"
echo "   Then run: helm install dial <CHART_PATH> -f helm-values.yaml -n dial --create-namespace"
echo ""
echo "2. Wait for Load Balancer to be created (5-10 minutes):"
echo "   kubectl get ingress -n dial -w"
echo ""
echo "3. Get the Load Balancer DNS name:"
echo "   ALB_DNS=\$(kubectl get ingress -n dial dial-chat-ingress -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
echo "   echo \"Load Balancer DNS: \${ALB_DNS}\""
echo ""
echo "4. Configure DNS records in your domain provider:"
echo "   See DNS-CONFIGURATION.md for detailed instructions"
echo "   Create CNAME records pointing to the ALB DNS:"
echo "   - chat.${DOMAIN_NAME} -> \${ALB_DNS}"
echo "   - admin.${DOMAIN_NAME} -> \${ALB_DNS}"
echo "   - core.${DOMAIN_NAME} -> \${ALB_DNS}"
echo "   - themes.${DOMAIN_NAME} -> \${ALB_DNS}"
echo ""
echo "   Note: Your domain does NOT need to be in Route53!"
echo "   Works with any DNS provider (GoDaddy, Cloudflare, etc.)"
echo ""
echo "5. Create users:"
echo ""
echo "   For Admin users (Admin portal):"
echo "   bash create-admin-user.sh"
echo ""
echo "   For Chat users (Chat interface):"
echo "   bash create-chat-user.sh"
echo ""
echo "   DIAL uses TWO separate Cognito User Pools:"
echo "   - Admin Pool: For admins (MFA required, stronger passwords)"
echo "   - Chat Pool: For end users (flexible policies)"
echo ""
echo "6. Access DIAL:"
echo "   Chat UI: https://chat.${DOMAIN_NAME}"
echo "   Admin UI: https://admin.${DOMAIN_NAME}"
echo ""
NEXTSCRIPT

chmod +x "${SCRIPT_DIR}/next-steps.sh"

# Create admin user creation script
cat > "${SCRIPT_DIR}/create-admin-user.sh" <<ADMINSCRIPT
#!/bin/bash
source parameters.conf

# Get Admin User Pool ID (not the regular user pool)
ADMIN_USER_POOL_ID=\$(aws cloudformation describe-stacks \\
  --stack-name ${STACK_NAME} \\
  --region ${AWS_REGION} \\
  --query "Stacks[0].Outputs[?OutputKey=='AdminCognitoUserPoolId'].OutputValue" \\
  --output text)

echo "Creating admin user in Admin User Pool: ${ADMIN_EMAIL}"
echo "User Pool ID: \${ADMIN_USER_POOL_ID}"

aws cognito-idp admin-create-user \\
  --user-pool-id \${ADMIN_USER_POOL_ID} \\
  --username ${ADMIN_EMAIL} \\
  --user-attributes Name=email,Value=${ADMIN_EMAIL} Name=name,Value="Admin User" Name=custom:role,Value="admin" \\
  --region ${AWS_REGION}

echo ""
echo "Admin user created successfully in the Admin User Pool!"
echo "A temporary password has been sent to: ${ADMIN_EMAIL}"
echo "You will be asked to change it on first login."
echo ""
echo "IMPORTANT: This user is for the Admin portal at https://admin.${DOMAIN_NAME}"
echo "To create regular chat users, use the Chat User Pool (UserPoolId output)"
ADMINSCRIPT

chmod +x "${SCRIPT_DIR}/create-admin-user.sh"

print_success "All done! Check next-steps.sh for remaining manual steps."
