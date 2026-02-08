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
# Step 0: Empty DIAL storage bucket (if any)
###############################################################################

print_header "Step 0: Emptying DIAL storage bucket (if any)"

if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    STORAGE_BUCKET_NAME=$(aws cloudformation describe-stacks \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --query "Stacks[0].Outputs[?OutputKey=='S3BucketName'].OutputValue" \
        --output text 2>/dev/null || echo "")

    if [ -n "$STORAGE_BUCKET_NAME" ] && [ "$STORAGE_BUCKET_NAME" != "None" ]; then
        print_info "Emptying storage bucket: ${STORAGE_BUCKET_NAME}"
        aws s3 rm "s3://${STORAGE_BUCKET_NAME}" --recursive --region ${AWS_REGION} 2>/dev/null || true
    else
        print_info "Storage bucket output not found, skipping"
    fi
else
    print_info "Stack ${STACK_NAME} does not exist, skipping"
fi

###############################################################################
# Step 1: Disable RDS deletion protection (if present)
###############################################################################

print_header "Step 1: Disabling RDS deletion protection (if any)"

if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    DB_STACK_NAME=$(aws cloudformation describe-stack-resources \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --query "StackResources[?LogicalResourceId=='DatabaseStack' && ResourceType=='AWS::CloudFormation::Stack'].PhysicalResourceId" \
        --output text 2>/dev/null || echo "")

    if [ -n "$DB_STACK_NAME" ] && [ "$DB_STACK_NAME" != "None" ]; then
        print_info "Found database stack: ${DB_STACK_NAME}"

        DB_CLUSTERS=$(aws cloudformation describe-stack-resources \
            --stack-name ${DB_STACK_NAME} \
            --region ${AWS_REGION} \
            --query "StackResources[?ResourceType=='AWS::RDS::DBCluster'].PhysicalResourceId" \
            --output text 2>/dev/null || echo "")

        if [ -n "$DB_CLUSTERS" ] && [ "$DB_CLUSTERS" != "None" ]; then
            for DB_CLUSTER in $DB_CLUSTERS; do
                print_info "Disabling deletion protection for DB cluster: ${DB_CLUSTER}"
                aws rds modify-db-cluster \
                    --db-cluster-identifier ${DB_CLUSTER} \
                    --no-deletion-protection \
                    --apply-immediately \
                    --region ${AWS_REGION} 2>/dev/null || true
            done
        else
            print_info "No DB clusters found in database stack"
        fi
    else
        print_info "Database stack not found, skipping"
    fi
else
    print_info "Stack ${STACK_NAME} does not exist, skipping"
fi

###############################################################################
# Step 2: Delete ALBs in the stack VPC (if any)
###############################################################################

print_header "Step 2: Deleting ALBs in VPC (if any)"

VPC_ID_FROM_STACK=$(aws cloudformation describe-stacks \
    --stack-name ${STACK_NAME} \
    --region ${AWS_REGION} \
    --query "Stacks[0].Outputs[?OutputKey=='VPCId'].OutputValue" \
    --output text 2>/dev/null || echo "")

if [ -n "$VPC_ID_FROM_STACK" ] && [ "$VPC_ID_FROM_STACK" != "None" ]; then
    print_info "VPC detected: ${VPC_ID_FROM_STACK}"

    ALB_ARNS=$(aws elbv2 describe-load-balancers \
        --region ${AWS_REGION} \
        --query "LoadBalancers[?VpcId=='${VPC_ID_FROM_STACK}'].LoadBalancerArn" \
        --output text 2>/dev/null || echo "")

    if [ -n "$ALB_ARNS" ] && [ "$ALB_ARNS" != "None" ]; then
        print_info "Deleting ALBs in VPC..."
        for ALB_ARN in $ALB_ARNS; do
            print_info "  Deleting: ${ALB_ARN}"
            aws elbv2 delete-load-balancer \
                --load-balancer-arn ${ALB_ARN} \
                --region ${AWS_REGION} 2>/dev/null || true
        done

        print_info "Waiting for ALB deletion..."
        aws elbv2 wait load-balancers-deleted \
            --load-balancer-arns ${ALB_ARNS} \
            --region ${AWS_REGION} 2>/dev/null || print_warning "ALB deletion wait timed out"
    else
        print_info "No ALBs found in VPC"
    fi
else
    print_info "VPC ID not found, skipping ALB cleanup"
fi

###############################################################################
# Step 3: Delete EKS security groups in VPC (if any)
###############################################################################

print_header "Step 3: Deleting EKS security groups in VPC (if any)"

EKS_CLUSTER_NAME=$(aws cloudformation describe-stacks \
    --stack-name ${STACK_NAME} \
    --region ${AWS_REGION} \
    --query "Stacks[0].Outputs[?OutputKey=='EKSClusterName'].OutputValue" \
    --output text 2>/dev/null || echo "")

if [ -n "$VPC_ID_FROM_STACK" ] && [ "$VPC_ID_FROM_STACK" != "None" ] && [ -n "$EKS_CLUSTER_NAME" ] && [ "$EKS_CLUSTER_NAME" != "None" ]; then
    SG_IDS_1=$(aws ec2 describe-security-groups \
        --filters Name=vpc-id,Values=${VPC_ID_FROM_STACK} Name=tag:aws:eks:cluster-name,Values=${EKS_CLUSTER_NAME} \
        --region ${AWS_REGION} \
        --query "SecurityGroups[?GroupName!='default'].GroupId" \
        --output text 2>/dev/null || echo "")

    SG_IDS_2=$(aws ec2 describe-security-groups \
        --filters Name=vpc-id,Values=${VPC_ID_FROM_STACK} Name=tag:kubernetes.io/cluster/${EKS_CLUSTER_NAME},Values=owned,shared \
        --region ${AWS_REGION} \
        --query "SecurityGroups[?GroupName!='default'].GroupId" \
        --output text 2>/dev/null || echo "")

    SG_IDS=$(printf "%s\n%s\n" "$SG_IDS_1" "$SG_IDS_2" | tr ' ' '\n' | sort -u | sed '/^$/d')

    if [ -n "$SG_IDS" ]; then
        for SG_ID in $SG_IDS; do
            print_info "Deleting security group: ${SG_ID}"
            aws ec2 delete-security-group \
                --group-id ${SG_ID} \
                --region ${AWS_REGION} 2>/dev/null || true
        done
    else
        print_info "No EKS security groups found in VPC"
    fi
else
    print_info "VPC or EKS cluster name not found, skipping"
fi

###############################################################################
# Step 4: Delete CloudFormation Stack
###############################################################################

print_header "Step 4: Deleting CloudFormation Stack"

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
# Step 5: Delete S3 Buckets
###############################################################################

print_header "Step 5: Deleting S3 Buckets"

# Function to delete S3 bucket
delete_bucket() {
    local BUCKET_NAME=$1
    
    if aws s3 ls "s3://${BUCKET_NAME}" --region ${AWS_REGION} &> /dev/null; then
        print_info "Deleting bucket: ${BUCKET_NAME}"

        print_info "  Emptying bucket..."
        aws s3 rm "s3://${BUCKET_NAME}" --recursive --region ${AWS_REGION} 2>/dev/null || true
        
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
# Step 5: Delete Test Cognito Pools
###############################################################################

print_header "Step 5: Cleaning Up Cognito Test Pools"

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
# Step 6: Summary
###############################################################################

print_header "Cleanup Complete"

print_success "Cleanup completed successfully!"
echo ""
echo "You can now run: bash deploy.sh"
echo ""
