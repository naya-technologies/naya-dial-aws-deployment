#!/bin/bash

# DIAL AWS Deployment - Launch Script
# This script prepares and launches the CloudFormation stack, then exits
# Use monitor.sh to track progress

set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Helper functions
print_header() {
    echo ""
    echo "========================================"
    echo "$1"
    echo "========================================"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} Warning: $1"
}

print_error() {
    echo -e "${RED}✗${NC} Error: $1"
}

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Load configuration
if [ ! -f "parameters.conf" ]; then
    print_error "parameters.conf not found!"
    exit 1
fi

source parameters.conf

print_header "DIAL AWS Deployment - Launch"
echo "This script will:"
echo "1. Check prerequisites"
echo "2. Prepare CloudFormation templates"
echo "3. Launch the stack"
echo "4. Exit (use monitor.sh to track progress)"
echo ""

# Step 1: Check prerequisites
print_header "Step 1: Checking Prerequisites"

print_info "Checking AWS CLI..."
if ! command -v aws &> /dev/null; then
    print_error "AWS CLI not found. Please install it first."
    exit 1
fi
print_success "AWS CLI found: $(aws --version)"

print_info "Checking AWS credentials..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
    print_error "AWS credentials not configured"
    exit 1
fi
print_success "AWS Account ID: $ACCOUNT_ID"

# Step 2: Prepare templates
print_header "Step 2: Preparing CloudFormation Templates"

TEMPLATES_BUCKET="${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
print_info "S3 bucket: ${TEMPLATES_BUCKET}"

if aws s3 ls "s3://${TEMPLATES_BUCKET}" --region ${AWS_REGION} 2>&1 | grep -q 'NoSuchBucket'; then
    aws s3 mb "s3://${TEMPLATES_BUCKET}" --region ${AWS_REGION}
    print_success "Bucket created"
else
    print_success "Bucket already exists"
fi

print_info "Uploading CloudFormation templates..."
for template in dial-vpc.yaml dial-eks.yaml dial-iam.yaml dial-storage.yaml dial-cache.yaml dial-rds.yaml dial-cognito.yaml; do
    if [ -f "${SCRIPT_DIR}/cloudformation/${template}" ]; then
        aws s3 cp "${SCRIPT_DIR}/cloudformation/${template}" "s3://${TEMPLATES_BUCKET}/" --region ${AWS_REGION}
        print_success "Uploaded ${template}"
    else
        print_error "${template} not found"
        exit 1
    fi
done

# Update main template
sed "s|\${TemplatesBucket}|${TEMPLATES_BUCKET}|g" "${SCRIPT_DIR}/cloudformation/dial-main.yaml" > "/tmp/dial-main-updated.yaml"
print_success "Templates prepared"

# Step 3: Launch stack
print_header "Step 3: Launching CloudFormation Stack"

print_info "Stack Name: ${STACK_NAME}"
print_info "Region: ${AWS_REGION}"

# Build parameters
CREATE_CERT="true"
if [ "$CERTIFICATE_OPTION" == "existing" ]; then
    CREATE_CERT="false"
fi

DISABLE_SELF_REG="true"
if [ "$ALLOW_SELF_REGISTRATION" == "yes" ]; then
    DISABLE_SELF_REG="false"
fi

# Show configuration and ask for confirmation
echo ""
echo "Configuration Summary:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Stack Name:      ${STACK_NAME}"
echo "Region:          ${AWS_REGION}"
echo "Domain:          ${DOMAIN_NAME}"
echo "EKS Cluster:     ${EKS_CLUSTER_NAME}"
echo "EKS Instance:    ${EKS_NODE_INSTANCE_TYPE:-m5.large}"
echo "EKS Nodes:       ${EKS_NODE_DESIRED_SIZE:-3} (min: ${EKS_NODE_MIN_SIZE:-2}, max: ${EKS_NODE_MAX_SIZE:-10})"
echo "Certificate:     ${CERTIFICATE_OPTION}"
echo "Self-Register:   ${ALLOW_SELF_REGISTRATION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
read -p "Continue with deployment? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    print_warning "Deployment cancelled"
    exit 0
fi
echo ""

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
PARAMS="${PARAMS} ParameterKey=EKSNodeInstanceType,ParameterValue=${EKS_NODE_INSTANCE_TYPE:-m5.large}"
PARAMS="${PARAMS} ParameterKey=EKSNodeMinSize,ParameterValue=${EKS_NODE_MIN_SIZE:-2}"
PARAMS="${PARAMS} ParameterKey=EKSNodeMaxSize,ParameterValue=${EKS_NODE_MAX_SIZE:-10}"
PARAMS="${PARAMS} ParameterKey=EKSNodeDesiredSize,ParameterValue=${EKS_NODE_DESIRED_SIZE:-3}"

if [ "$CERTIFICATE_OPTION" == "existing" ] && [ -n "$EXISTING_CERTIFICATE_ARN" ]; then
    PARAMS="${PARAMS} ParameterKey=ExistingCertificateArn,ParameterValue=${EXISTING_CERTIFICATE_ARN}"
fi

# Check if stack exists
if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    STACK_STATUS=$(aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} --query 'Stacks[0].StackStatus' --output text)
    
    if [ "$STACK_STATUS" == "ROLLBACK_COMPLETE" ] || [ "$STACK_STATUS" == "ROLLBACK_FAILED" ]; then
        print_warning "Stack is in ${STACK_STATUS} state"
        print_info "Deleting failed stack..."
        
        aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${AWS_REGION}
        print_info "Waiting for deletion..."
        aws cloudformation wait stack-delete-complete --stack-name ${STACK_NAME} --region ${AWS_REGION} 2>/dev/null || true
        print_success "Stack deleted"
        
        print_info "Creating new stack..."
        aws cloudformation create-stack \
            --stack-name ${STACK_NAME} \
            --template-body file:///tmp/dial-main-updated.yaml \
            --parameters ${PARAMS} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region ${AWS_REGION}
        
        print_success "Stack creation initiated"
    else
        print_warning "Stack already exists (Status: ${STACK_STATUS})"
        echo ""
        echo "Options:"
        echo "1. Delete and recreate: bash cleanup.sh && bash deploy.sh"
        echo "2. Monitor existing: bash monitor.sh"
        exit 0
    fi
else
    print_info "Creating new stack..."
    aws cloudformation create-stack \
        --stack-name ${STACK_NAME} \
        --template-body file:///tmp/dial-main-updated.yaml \
        --parameters ${PARAMS} \
        --capabilities CAPABILITY_NAMED_IAM \
        --region ${AWS_REGION}
    
    print_success "Stack creation initiated"
fi

# Done
print_header "Deployment Launched!"
echo ""
echo -e "${GREEN}✓${NC} CloudFormation stack creation initiated"
echo -e "${BLUE}ℹ${NC} Stack Name: ${STACK_NAME}"
echo -e "${BLUE}ℹ${NC} Region: ${AWS_REGION}"
echo ""
echo "Next steps:"
echo -e "1. Run: ${GREEN}bash monitor.sh${NC} to track progress"
echo -e "2. Or check AWS Console: CloudFormation → ${STACK_NAME}"
echo ""
echo "Expected deployment time: 25-35 minutes"
echo ""
