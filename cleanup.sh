#!/bin/bash

###############################################################################
# DIAL AWS Installation - Cleanup Script
# This script cleans up failed CloudFormation deployments
###############################################################################

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Load parameters if available
if [ -f "${SCRIPT_DIR}/parameters.conf" ]; then
    source "${SCRIPT_DIR}/parameters.conf"
fi

# Set defaults
AWS_REGION=${AWS_REGION:-us-east-1}
STACK_NAME=${STACK_NAME:-dial-production}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")

print_header "DIAL Cleanup Script"

echo "This script will clean up the following resources:"
echo "  - CloudFormation Stack: ${STACK_NAME}"
echo "  - S3 Buckets: ${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
echo "                ${STACK_NAME}-templates-${ACCOUNT_ID}"
echo "  - Cognito Test Pools (if any)"
echo "  - Region: ${AWS_REGION}"
echo ""
read -p "Continue with cleanup? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    print_warning "Cleanup cancelled"
    exit 0
fi

###############################################################################
# Step 1: Delete CloudFormation Stack
###############################################################################

print_header "Step 1: Deleting CloudFormation Stack"

if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    print_info "Stack ${STACK_NAME} exists, deleting..."
    
    aws cloudformation delete-stack \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION}
    
    print_info "Waiting for stack deletion (this may take 5-10 minutes)..."
    
    if aws cloudformation wait stack-delete-complete \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} 2>/dev/null; then
        print_success "Stack deleted successfully"
    else
        print_warning "Stack deletion may have failed or timed out"
        print_warning "Check AWS Console for details"
    fi
else
    print_info "Stack ${STACK_NAME} does not exist, skipping"
fi

###############################################################################
# Step 2: Delete S3 Buckets
###############################################################################

print_header "Step 2: Deleting S3 Buckets"

# Function to delete S3 bucket
delete_bucket() {
    local BUCKET_NAME=$1
    
    if aws s3 ls "s3://${BUCKET_NAME}" --region ${AWS_REGION} &> /dev/null; then
        print_info "Deleting bucket: ${BUCKET_NAME}"
        
        # Remove all objects first
        print_info "  Removing objects..."
        aws s3 rm "s3://${BUCKET_NAME}" --recursive --region ${AWS_REGION} 2>/dev/null || true
        
        # Remove all versions if versioning is enabled
        print_info "  Removing versions..."
        aws s3api delete-objects \
            --bucket ${BUCKET_NAME} \
            --delete "$(aws s3api list-object-versions \
                --bucket ${BUCKET_NAME} \
                --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
                --region ${AWS_REGION} 2>/dev/null)" \
            --region ${AWS_REGION} 2>/dev/null || true
        
        # Delete bucket
        print_info "  Deleting bucket..."
        if aws s3 rb "s3://${BUCKET_NAME}" --region ${AWS_REGION} 2>/dev/null; then
            print_success "Bucket ${BUCKET_NAME} deleted"
        else
            print_warning "Could not delete bucket ${BUCKET_NAME}"
        fi
    else
        print_info "Bucket ${BUCKET_NAME} does not exist, skipping"
    fi
}

# Delete template buckets
delete_bucket "${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
delete_bucket "${STACK_NAME}-templates-${ACCOUNT_ID}"

###############################################################################
# Step 3: Delete Test Cognito Pools
###############################################################################

print_header "Step 3: Cleaning Up Cognito Test Pools"

print_info "Checking for test user pools..."
TEST_POOLS=$(aws cognito-idp list-user-pools --max-results 60 --region ${AWS_REGION} \
    --query 'UserPools[?contains(Name, `test`) || contains(Name, `delete-me`)].Id' \
    --output text 2>/dev/null || echo "")

if [ -n "$TEST_POOLS" ]; then
    for POOL_ID in $TEST_POOLS; do
        print_info "Deleting test pool: ${POOL_ID}"
        aws cognito-idp delete-user-pool \
            --user-pool-id ${POOL_ID} \
            --region ${AWS_REGION} 2>/dev/null || true
        print_success "Deleted pool ${POOL_ID}"
    done
else
    print_info "No test pools found"
fi

###############################################################################
# Step 4: Summary
###############################################################################

print_header "Cleanup Complete"

print_success "Cleanup completed successfully!"
echo ""
echo "You can now run: bash deploy.sh"
echo ""
