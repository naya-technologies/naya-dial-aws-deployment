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

helm_release_exists() {
    local RELEASE_NAME="$1"
    local NAMESPACE="$2"
    helm list -n "$NAMESPACE" -q 2>/dev/null | grep -Fxq "$RELEASE_NAME"
}

wait_for_helm_release_cleanup() {
    local RELEASE_NAME="$1"
    local NAMESPACE="$2"
    local TIMEOUT_SECONDS=900

    print_info "Waiting for Helm/Kubernetes resources to be deleted (up to 15 minutes)..."

    local TGB_SUPPORTED="false"
    if kubectl api-resources --api-group elbv2.k8s.aws -o name 2>/dev/null | grep -Fxq "targetgroupbindings"; then
        TGB_SUPPORTED="true"
    fi

    local DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
    while [ "$SECONDS" -lt "$DEADLINE" ]; do
        local INGRESS_COUNT
        local WORKLOAD_COUNT
        local TGB_COUNT="0"

        INGRESS_COUNT=$(kubectl -n "$NAMESPACE" get ingress -l "app.kubernetes.io/instance=${RELEASE_NAME}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        WORKLOAD_COUNT=$(kubectl -n "$NAMESPACE" get deploy,sts,svc,job -l "app.kubernetes.io/instance=${RELEASE_NAME}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")

        if [ "$TGB_SUPPORTED" = "true" ]; then
            TGB_COUNT=$(kubectl -n "$NAMESPACE" get targetgroupbinding -l "app.kubernetes.io/instance=${RELEASE_NAME}" --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        fi

        if [ "$INGRESS_COUNT" = "0" ] && [ "$WORKLOAD_COUNT" = "0" ] && [ "$TGB_COUNT" = "0" ]; then
            print_success "Helm release resources cleaned up"
            return 0
        fi

        sleep 5
    done

    print_warning "Timed out waiting for Helm resource cleanup; continuing with AWS cleanup"
}

run_helm_precleanup() {
    local RELEASE_NAME="$1"
    local NAMESPACE="$2"

    if ! command -v helm >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1; then
        print_info "helm/kubectl not found, skipping Helm pre-cleanup"
        return 0
    fi

    # In CloudShell, kubeconfig is expected to be configured beforehand.
    if ! kubectl version --request-timeout=5s >/dev/null 2>&1; then
        print_warning "kubectl context is not ready, skipping Helm pre-cleanup"
        return 0
    fi

    if ! kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
        print_info "Namespace ${NAMESPACE} not found, skipping Helm pre-cleanup"
        return 0
    fi

    if helm_release_exists "${RELEASE_NAME}" "${NAMESPACE}"; then
        print_info "Helm release found: ${RELEASE_NAME} (namespace: ${NAMESPACE})"
        if helm uninstall "${RELEASE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
            print_success "Helm release uninstall requested"
        else
            print_warning "Helm uninstall failed or release already removing; continuing"
        fi
        wait_for_helm_release_cleanup "${RELEASE_NAME}" "${NAMESPACE}"
    else
        print_info "Helm release not found: ${RELEASE_NAME} (namespace: ${NAMESPACE})"
    fi
}

# Empty S3 bucket including versioned objects and delete markers.
empty_bucket() {
    local BUCKET_NAME=$1
    if [ -z "$BUCKET_NAME" ] || [ "$BUCKET_NAME" = "None" ]; then
        return 0
    fi

    # Delete current objects
    aws s3 rm "s3://${BUCKET_NAME}" --recursive --region ${AWS_REGION} 2>/dev/null || true

    # Delete object versions
    local VERSIONS_JSON
    VERSIONS_JSON=$(aws s3api list-object-versions \
        --bucket "${BUCKET_NAME}" \
        --region ${AWS_REGION} \
        --query 'Versions[].{Key:Key,VersionId:VersionId}' \
        --output json 2>/dev/null || echo "[]")

    if [ "$VERSIONS_JSON" != "[]" ]; then
        aws s3api delete-objects \
            --bucket "${BUCKET_NAME}" \
            --region ${AWS_REGION} \
            --delete "{\"Objects\": ${VERSIONS_JSON}}" 2>/dev/null || true
    fi

    # Delete delete-markers
    local MARKERS_JSON
    MARKERS_JSON=$(aws s3api list-object-versions \
        --bucket "${BUCKET_NAME}" \
        --region ${AWS_REGION} \
        --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' \
        --output json 2>/dev/null || echo "[]")

    if [ "$MARKERS_JSON" != "[]" ]; then
        aws s3api delete-objects \
            --bucket "${BUCKET_NAME}" \
            --region ${AWS_REGION} \
            --delete "{\"Objects\": ${MARKERS_JSON}}" 2>/dev/null || true
    fi
}

# Delete orphan target groups that belong to this EKS cluster.
# These are not CloudFormation-managed and can remain after ALB deletion.
delete_orphan_target_groups() {
    local CLUSTER_NAME="$1"
    local RELEASE_NAME="$2"

    if [ -z "$CLUSTER_NAME" ] || [ "$CLUSTER_NAME" = "None" ]; then
        print_info "EKS cluster name not found, skipping orphan target group cleanup"
        return 0
    fi

    print_info "Checking for orphan target groups tagged for cluster ${CLUSTER_NAME}..."

    local TG_ARNS
    TG_ARNS=$(aws elbv2 describe-target-groups \
        --region ${AWS_REGION} \
        --query "TargetGroups[].TargetGroupArn" \
        --output text 2>/dev/null || echo "")

    if [ -z "$TG_ARNS" ] || [ "$TG_ARNS" = "None" ]; then
        print_info "No target groups found"
        return 0
    fi

    local DELETED_COUNT=0
    for TG_ARN in $TG_ARNS; do
        # Skip target groups that are still attached to a load balancer.
        local LB_ARNS
        LB_ARNS=$(aws elbv2 describe-target-groups \
            --target-group-arns ${TG_ARN} \
            --region ${AWS_REGION} \
            --query "TargetGroups[0].LoadBalancerArns" \
            --output text 2>/dev/null || echo "")
        if [ -n "$LB_ARNS" ] && [ "$LB_ARNS" != "None" ]; then
            continue
        fi

        # Delete only project target groups for this release.
        local TG_NAME
        TG_NAME=$(aws elbv2 describe-target-groups \
            --target-group-arns ${TG_ARN} \
            --region ${AWS_REGION} \
            --query "TargetGroups[0].TargetGroupName" \
            --output text 2>/dev/null || echo "")
        if [[ ! "$TG_NAME" =~ ^k8s-${RELEASE_NAME}- ]]; then
            continue
        fi

        # Delete only if this target group is tagged for the current cluster.
        local MATCH_COUNT
        MATCH_COUNT=$(aws elbv2 describe-tags \
            --resource-arns ${TG_ARN} \
            --region ${AWS_REGION} \
            --query "TagDescriptions[0].Tags[?((Key=='elbv2.k8s.aws/cluster' && Value=='${CLUSTER_NAME}') || Key=='kubernetes.io/cluster/${CLUSTER_NAME}')]| length(@)" \
            --output text 2>/dev/null || echo "0")

        if [ "$MATCH_COUNT" != "0" ] && [ "$MATCH_COUNT" != "None" ]; then
            print_info "Deleting orphan target group: ${TG_ARN}"
            aws elbv2 delete-target-group \
                --target-group-arn ${TG_ARN} \
                --region ${AWS_REGION} 2>/dev/null || true
            DELETED_COUNT=$((DELETED_COUNT + 1))
        fi
    done

    if [ "$DELETED_COUNT" -gt 0 ]; then
        print_success "Deleted ${DELETED_COUNT} orphan target group(s)"
    else
        print_info "No orphan target groups found for cluster ${CLUSTER_NAME}"
    fi
}

# Best-effort cleanup for VPC deletion blockers that can appear after EKS teardown:
# - "aws-K8S-*" ENIs left behind after node termination
# - EKS cluster security group (eks-cluster-sg-<cluster>-*) that becomes deletable only after control-plane ENIs are gone
cleanup_vpc_leftovers() {
    local VPC_ID="$1"
    local CLUSTER_NAME="$2"

    if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
        return 0
    fi

    print_info "Checking for interface VPC endpoints in VPC ${VPC_ID}..."
    local VPCE_IDS=""
    local NESTED_ID
    for NESTED_ID in VPCStack EKSStack; do
        local NESTED_STACK_ARN
        NESTED_STACK_ARN=$(aws cloudformation describe-stack-resources \
            --stack-name ${STACK_NAME} \
            --region ${AWS_REGION} \
            --query "StackResources[?LogicalResourceId=='${NESTED_ID}' && ResourceType=='AWS::CloudFormation::Stack'].PhysicalResourceId" \
            --output text 2>/dev/null || echo "")

        if [ -z "$NESTED_STACK_ARN" ] || [ "$NESTED_STACK_ARN" = "None" ]; then
            continue
        fi

        local IDS_FROM_NESTED
        IDS_FROM_NESTED=$(aws cloudformation describe-stack-resources \
            --stack-name ${NESTED_STACK_ARN} \
            --region ${AWS_REGION} \
            --query "StackResources[?ResourceType=='AWS::EC2::VPCEndpoint'].PhysicalResourceId" \
            --output text 2>/dev/null || echo "")

        if [ -n "$IDS_FROM_NESTED" ] && [ "$IDS_FROM_NESTED" != "None" ]; then
            VPCE_IDS="${VPCE_IDS} ${IDS_FROM_NESTED}"
        fi
    done
    VPCE_IDS=$(printf "%s\n" "$VPCE_IDS" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')

    if [ -n "$VPCE_IDS" ] && [ "$VPCE_IDS" != "None" ]; then
        print_info "Deleting stack-owned interface VPC endpoints..."
        aws ec2 delete-vpc-endpoints \
            --vpc-endpoint-ids ${VPCE_IDS} \
            --region ${AWS_REGION} 2>/dev/null || true

        print_info "Waiting for interface VPC endpoints to be deleted..."
        local vpce_deadline=$((SECONDS + 300))
        while [ "$SECONDS" -lt "$vpce_deadline" ]; do
            local remaining_vpce
            remaining_vpce=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids ${VPCE_IDS} \
                --region ${AWS_REGION} \
                --query 'length(VpcEndpoints[?State!=`deleted`])' \
                --output text 2>/dev/null || echo "0")
            if [ "$remaining_vpce" = "0" ]; then
                break
            fi
            sleep 10
        done
    else
        print_info "No interface VPC endpoints found"
    fi

    print_info "Checking for EKS-related available ENIs in VPC ${VPC_ID}..."
    local ENI_IDS
    ENI_IDS=$(aws ec2 describe-network-interfaces \
        --filters Name=vpc-id,Values="${VPC_ID}" Name=status,Values=available Name=description,Values="Amazon EKS *${CLUSTER_NAME}*","aws-K8S*" \
        --region ${AWS_REGION} \
        --query 'NetworkInterfaces[].NetworkInterfaceId' \
        --output text 2>/dev/null || echo "")

    if [ -n "$ENI_IDS" ] && [ "$ENI_IDS" != "None" ]; then
        for ENI_ID in $ENI_IDS; do
            print_info "Deleting ENI: ${ENI_ID}"
            aws ec2 delete-network-interface \
                --network-interface-id ${ENI_ID} \
                --region ${AWS_REGION} 2>/dev/null || true
        done
    else
        print_info "No available ENIs found"
    fi

    if [ -n "$CLUSTER_NAME" ] && [ "$CLUSTER_NAME" != "None" ]; then
        print_info "Checking for leftover EKS cluster security groups..."
        local CLUSTER_SG_IDS
        CLUSTER_SG_IDS=$(aws ec2 describe-security-groups \
            --filters Name=vpc-id,Values="${VPC_ID}" \
            --region ${AWS_REGION} \
            --query "SecurityGroups[?GroupName!='default' && starts_with(GroupName, 'eks-cluster-sg-${CLUSTER_NAME}-')].GroupId" \
            --output text 2>/dev/null || echo "")

        if [ -n "$CLUSTER_SG_IDS" ] && [ "$CLUSTER_SG_IDS" != "None" ]; then
            for SG_ID in $CLUSTER_SG_IDS; do
                print_info "Deleting security group: ${SG_ID}"
                aws ec2 delete-security-group \
                    --group-id ${SG_ID} \
                    --region ${AWS_REGION} 2>/dev/null || true
            done
        else
            print_info "No EKS cluster security groups found"
        fi
    fi
}

ensure_nested_stack_deleted() {
    local PARENT_STACK="$1"
    local LOGICAL_ID="$2"

    local NESTED_STACK_ARN
    NESTED_STACK_ARN=$(aws cloudformation describe-stack-resources \
        --stack-name ${PARENT_STACK} \
        --region ${AWS_REGION} \
        --query "StackResources[?LogicalResourceId=='${LOGICAL_ID}' && ResourceType=='AWS::CloudFormation::Stack'].PhysicalResourceId" \
        --output text 2>/dev/null || echo "")

    if [ -z "$NESTED_STACK_ARN" ] || [ "$NESTED_STACK_ARN" = "None" ]; then
        return 0
    fi

    local NESTED_STATUS
    NESTED_STATUS=$(aws cloudformation describe-stacks \
        --stack-name ${NESTED_STACK_ARN} \
        --region ${AWS_REGION} \
        --query 'Stacks[0].StackStatus' \
        --output text 2>/dev/null || echo "MISSING")

    if [ "$NESTED_STATUS" = "DELETE_COMPLETE" ] || [ "$NESTED_STATUS" = "MISSING" ]; then
        return 0
    fi

    if [ "$NESTED_STATUS" != "DELETE_IN_PROGRESS" ]; then
        print_info "Deleting nested stack ${LOGICAL_ID}: ${NESTED_STACK_ARN}"
        aws cloudformation delete-stack \
            --stack-name ${NESTED_STACK_ARN} \
            --region ${AWS_REGION} 2>/dev/null || true
    else
        print_info "Nested stack ${LOGICAL_ID} already deleting: ${NESTED_STACK_ARN}"
    fi

    if aws cloudformation wait stack-delete-complete \
        --stack-name ${NESTED_STACK_ARN} \
        --region ${AWS_REGION} 2>/dev/null; then
        print_success "Nested stack deleted: ${LOGICAL_ID}"
    else
        print_warning "Nested stack deletion timed out/failed: ${LOGICAL_ID}"
    fi
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
DIAL_RELEASE_NAME=${DIAL_RELEASE_NAME:-dial}
DIAL_NAMESPACE=${DIAL_NAMESPACE:-dial}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")

print_header "DIAL Cleanup Script"

echo "This script will clean up the following resources:"
echo "  - CloudFormation Stack: ${STACK_NAME}"
echo "  - Helm release prefix: k8s-${DIAL_RELEASE_NAME}-*"
echo "  - Helm release: ${DIAL_RELEASE_NAME} (namespace: ${DIAL_NAMESPACE})"
echo "  - S3 Buckets: ${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
echo "                ${STACK_NAME}-templates-${ACCOUNT_ID}"
echo "  - ALBs tagged for this EKS cluster (if any)"
echo "  - Cognito user pool(s) from this stack outputs (if any remain)"
echo "  - Region: ${AWS_REGION}"
echo ""
read -p "Continue with cleanup? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    print_warning "Cleanup cancelled"
    exit 0
fi

# Capture Cognito pool IDs created by this stack (so we can delete only those, even after stack deletion)
STACK_COGNITO_POOL_IDS=""
EKS_CLUSTER_NAME_FROM_STACK=""
if aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${AWS_REGION} &> /dev/null; then
    STACK_COGNITO_POOL_IDS_RAW=$(aws cloudformation describe-stacks \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId' || OutputKey=='AdminCognitoUserPoolId'].OutputValue" \
        --output text 2>/dev/null || echo "")
    # De-duplicate and filter empty/None values
    STACK_COGNITO_POOL_IDS=$(printf "%s\n" "$STACK_COGNITO_POOL_IDS_RAW" | tr ' ' '\n' | sed '/^$/d;/^None$/d' | sort -u | tr '\n' ' ')

    EKS_CLUSTER_NAME_FROM_STACK=$(aws cloudformation describe-stacks \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --query "Stacks[0].Outputs[?OutputKey=='EKSClusterName'].OutputValue" \
        --output text 2>/dev/null || echo "")
fi

###############################################################################
# Step -1: Helm release uninstall (if any)
###############################################################################

print_header "Step -1: Helm release uninstall (if any)"
run_helm_precleanup "${DIAL_RELEASE_NAME}" "${DIAL_NAMESPACE}"

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
        empty_bucket "${STORAGE_BUCKET_NAME}"
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

    if [ -z "$EKS_CLUSTER_NAME_FROM_STACK" ] || [ "$EKS_CLUSTER_NAME_FROM_STACK" = "None" ]; then
        print_info "EKS cluster name not found, skipping ALB cleanup"
    else
        print_info "EKS cluster detected: ${EKS_CLUSTER_NAME_FROM_STACK}"

    ALB_ARNS=$(aws elbv2 describe-load-balancers \
        --region ${AWS_REGION} \
        --query "LoadBalancers[?VpcId=='${VPC_ID_FROM_STACK}'].LoadBalancerArn" \
        --output text 2>/dev/null || echo "")

    if [ -n "$ALB_ARNS" ] && [ "$ALB_ARNS" != "None" ]; then
        print_info "Deleting ALBs tagged for this EKS cluster and release prefix k8s-${DIAL_RELEASE_NAME}-..."
        DELETED_ALB_ARNS=""
        for ALB_ARN in $ALB_ARNS; do
            ALB_NAME=""
            ALB_NAME=$(aws elbv2 describe-load-balancers \
                --load-balancer-arns ${ALB_ARN} \
                --region ${AWS_REGION} \
                --query "LoadBalancers[0].LoadBalancerName" \
                --output text 2>/dev/null || echo "")
            if [[ ! "$ALB_NAME" =~ ^k8s-${DIAL_RELEASE_NAME}- ]]; then
                continue
            fi

            # Only delete load balancers created/managed by this cluster.
            # AWS Load Balancer Controller tags include:
            # - elbv2.k8s.aws/cluster = <cluster-name>
            # - kubernetes.io/cluster/<cluster-name> = owned/shared
            MATCH_COUNT=$(aws elbv2 describe-tags \
                --resource-arns ${ALB_ARN} \
                --region ${AWS_REGION} \
                --query "TagDescriptions[0].Tags[?((Key=='elbv2.k8s.aws/cluster' && Value=='${EKS_CLUSTER_NAME_FROM_STACK}') || Key=='kubernetes.io/cluster/${EKS_CLUSTER_NAME_FROM_STACK}')]| length(@)" \
                --output text 2>/dev/null || echo "0")

            if [ "$MATCH_COUNT" != "0" ] && [ "$MATCH_COUNT" != "None" ]; then
                print_info "  Deleting: ${ALB_ARN}"
                aws elbv2 delete-load-balancer \
                    --load-balancer-arn ${ALB_ARN} \
                    --region ${AWS_REGION} 2>/dev/null || true
                DELETED_ALB_ARNS="${DELETED_ALB_ARNS} ${ALB_ARN}"
            fi
        done

        DELETED_ALB_ARNS="${DELETED_ALB_ARNS# }"
        if [ -n "$DELETED_ALB_ARNS" ]; then
            print_info "Waiting for ALB deletion..."
            aws elbv2 wait load-balancers-deleted \
                --load-balancer-arns ${DELETED_ALB_ARNS} \
                --region ${AWS_REGION} 2>/dev/null || print_warning "ALB deletion wait timed out"
        else
            print_info "No cluster-tagged load balancers found in VPC"
        fi
    else
        print_info "No ALBs found in VPC"
    fi
    fi
else
    print_info "VPC ID not found, skipping ALB cleanup"
fi

print_header "Step 2.1: Deleting orphan target groups (if any)"
if [ -n "$EKS_CLUSTER_NAME_FROM_STACK" ] && [ "$EKS_CLUSTER_NAME_FROM_STACK" != "None" ]; then
    delete_orphan_target_groups "${EKS_CLUSTER_NAME_FROM_STACK}" "${DIAL_RELEASE_NAME}"
else
    print_info "EKS cluster name not available, skipping orphan target group cleanup"
fi

###############################################################################
# Step 3: Delete EKS / AWS Load Balancer Controller security groups in VPC (if any)
###############################################################################

print_header "Step 3: Deleting EKS/LB security groups in VPC (if any)"

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

    # AWS Load Balancer Controller managed security groups (can remain after deleting LBs)
    SG_IDS_3=$(aws ec2 describe-security-groups \
        --filters Name=vpc-id,Values=${VPC_ID_FROM_STACK} Name=tag:elbv2.k8s.aws/cluster,Values=${EKS_CLUSTER_NAME} \
        --region ${AWS_REGION} \
        --query "SecurityGroups[?GroupName!='default'].GroupId" \
        --output text 2>/dev/null || echo "")

    SG_IDS=$(printf "%s\n%s\n%s\n" "$SG_IDS_1" "$SG_IDS_2" "$SG_IDS_3" | tr ' ' '\n' | sort -u | sed '/^$/d')

    if [ -n "$SG_IDS" ]; then
        for SG_ID in $SG_IDS; do
            SG_NAME=""
            SG_NAME=$(aws ec2 describe-security-groups \
                --group-ids ${SG_ID} \
                --region ${AWS_REGION} \
                --query "SecurityGroups[0].GroupName" \
                --output text 2>/dev/null || echo "")

            # Safety: only delete k8s/EKS-generated SGs.
            # For k8s LB SGs, require release marker in the SG name.
            if [[ "$SG_NAME" == k8s-* && "$SG_NAME" != *${DIAL_RELEASE_NAME}* ]]; then
                print_info "Skipping k8s SG from another release: ${SG_ID} (${SG_NAME})"
                continue
            fi
            if [[ "$SG_NAME" != k8s-* && "$SG_NAME" != eks-cluster-sg-${EKS_CLUSTER_NAME}-* ]]; then
                print_info "Skipping non-k8s security group: ${SG_ID} (${SG_NAME})"
                continue
            fi

            print_info "Deleting security group: ${SG_ID}"
            aws ec2 delete-security-group \
                --group-id ${SG_ID} \
                --region ${AWS_REGION} 2>/dev/null || true
        done
    else
        print_info "No EKS/LB security groups found in VPC"
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
        STACK_STATUS=$(aws cloudformation describe-stacks \
            --stack-name ${STACK_NAME} \
            --region ${AWS_REGION} \
            --query 'Stacks[0].StackStatus' \
            --output text 2>/dev/null || echo "MISSING")

        if [ "$STACK_STATUS" = "DELETE_FAILED" ]; then
            print_warning "Stack deletion failed. Attempting to clean up common VPC blockers and retry..."

            # Ensure nested stacks that create private-subnet ENIs are fully deleted first.
            # Otherwise VPCStack can fail deleting subnets with "network interface in use".
            ensure_nested_stack_deleted "${STACK_NAME}" "IAMStack"
            ensure_nested_stack_deleted "${STACK_NAME}" "EKSStack"
            ensure_nested_stack_deleted "${STACK_NAME}" "CacheStack"
            ensure_nested_stack_deleted "${STACK_NAME}" "DatabaseStack"
            ensure_nested_stack_deleted "${STACK_NAME}" "CognitoStack"
            ensure_nested_stack_deleted "${STACK_NAME}" "StorageStack"

            # Retry cleanup of VPC leftovers (these often become deletable only after EKS resources are gone).
            cleanup_vpc_leftovers "${VPC_ID_FROM_STACK}" "${EKS_CLUSTER_NAME}"

            # If a nested VPC stack still exists, retry deleting it explicitly.
            VPC_STACK_ARN=$(aws cloudformation describe-stack-resources \
                --stack-name ${STACK_NAME} \
                --region ${AWS_REGION} \
                --query "StackResources[?LogicalResourceId=='VPCStack' && ResourceType=='AWS::CloudFormation::Stack'].PhysicalResourceId" \
                --output text 2>/dev/null || echo "")

            if [ -n "$VPC_STACK_ARN" ] && [ "$VPC_STACK_ARN" != "None" ]; then
                print_info "Retrying VPC nested stack deletion: ${VPC_STACK_ARN}"
                aws cloudformation delete-stack \
                    --stack-name ${VPC_STACK_ARN} \
                    --region ${AWS_REGION} 2>/dev/null || true
                aws cloudformation wait stack-delete-complete \
                    --stack-name ${VPC_STACK_ARN} \
                    --region ${AWS_REGION} 2>/dev/null || print_warning "VPC stack deletion wait timed out"
            fi

            # Retry main stack deletion once more
            print_info "Retrying main stack deletion: ${STACK_NAME}"
            aws cloudformation delete-stack \
                --stack-name ${STACK_NAME} \
                --region ${AWS_REGION} 2>/dev/null || true
            if aws cloudformation wait stack-delete-complete \
                --stack-name ${STACK_NAME} \
                --region ${AWS_REGION} 2>/dev/null; then
                print_success "Stack deleted successfully (after retry)"
            else
                print_warning "Stack deletion still failed or timed out. Check AWS Console for details"
            fi
        else
            print_warning "Stack deletion may have failed or timed out (status: ${STACK_STATUS})"
            print_warning "Check AWS Console for details"
        fi
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
        empty_bucket "${BUCKET_NAME}"
        
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
# Step 6: Delete Cognito User Pool(s) created by this stack (if any remain)
###############################################################################

print_header "Step 6: Cleaning Up Cognito User Pool(s) from this Stack"

if [ -n "$STACK_COGNITO_POOL_IDS" ]; then
    for POOL_ID in $STACK_COGNITO_POOL_IDS; do
        if aws cognito-idp describe-user-pool --user-pool-id ${POOL_ID} --region ${AWS_REGION} &> /dev/null; then
            print_info "Deleting user pool: ${POOL_ID}"
            aws cognito-idp delete-user-pool \
                --user-pool-id ${POOL_ID} \
                --region ${AWS_REGION} 2>/dev/null || true
            print_success "Delete requested for user pool ${POOL_ID}"
        else
            print_info "User pool already deleted (or not found): ${POOL_ID}"
        fi
    done
else
    print_info "No Cognito user pool IDs captured from stack outputs; skipping"
fi

###############################################################################
# Step 7: Summary
###############################################################################

print_header "Cleanup Complete"

print_success "Cleanup completed successfully!"
echo ""
echo "You can now run: bash deploy.sh"
echo ""
