#!/bin/bash

###############################################################################
# DIAL Pre-Installation Checker
# Run this script to verify your AWS account is ready for DIAL installation
###############################################################################

set -e

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

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

ERRORS=0
WARNINGS=0

print_header "DIAL Pre-Installation Checker"

echo "This script checks if your AWS account is ready for DIAL installation."
echo ""

###############################################################################
# Check 1: AWS CLI
###############################################################################

echo -e "${BLUE}[1/8]${NC} Checking AWS CLI..."
if command -v aws &> /dev/null; then
    AWS_VERSION=$(aws --version 2>&1)
    print_success "AWS CLI installed: $AWS_VERSION"
else
    print_error "AWS CLI not found"
    ((ERRORS++))
fi

###############################################################################
# Check 2: AWS Credentials
###############################################################################

echo -e "${BLUE}[2/8]${NC} Checking AWS credentials..."
if aws sts get-caller-identity &> /dev/null; then
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    USER_ARN=$(aws sts get-caller-identity --query Arn --output text)
    print_success "Authenticated as: $USER_ARN"
    print_success "Account ID: $ACCOUNT_ID"
else
    print_error "AWS credentials not configured or invalid"
    ((ERRORS++))
fi

###############################################################################
# Check 3: AWS Region
###############################################################################

echo -e "${BLUE}[3/8]${NC} Checking AWS region..."
AWS_REGION=$(aws configure get region 2>/dev/null || echo "")
if [ -z "$AWS_REGION" ]; then
    print_warning "No default region configured"
    print_warning "Set a region: aws configure set region us-east-2"
    ((WARNINGS++))
else
    print_success "Default region: $AWS_REGION"
fi

###############################################################################
# Check 4: IAM Permissions (basic check)
###############################################################################

echo -e "${BLUE}[4/8]${NC} Checking IAM permissions..."

# Check if user can create resources
PERM_ERRORS=0

if ! aws iam list-roles --max-items 1 &> /dev/null; then
    print_warning "Cannot list IAM roles - you may not have sufficient permissions"
    ((WARNINGS++))
    ((PERM_ERRORS++))
fi

if ! aws ec2 describe-vpcs --max-results 1 &> /dev/null; then
    print_warning "Cannot describe VPCs - you may not have EC2 permissions"
    ((WARNINGS++))
    ((PERM_ERRORS++))
fi

if ! aws eks list-clusters &> /dev/null; then
    print_warning "Cannot list EKS clusters - you may not have EKS permissions"
    ((WARNINGS++))
    ((PERM_ERRORS++))
fi

if [ $PERM_ERRORS -eq 0 ]; then
    print_success "Basic IAM permissions OK"
else
    print_warning "Some permissions checks failed - ensure you have admin-level access"
fi

###############################################################################
# Check 5: Service Quotas
###############################################################################

echo -e "${BLUE}[5/8]${NC} Checking AWS service quotas..."

# Check VPC quota
VPC_COUNT=$(aws ec2 describe-vpcs --query 'Vpcs | length(@)' --output text 2>/dev/null || echo "0")
if [ "$VPC_COUNT" -ge 5 ]; then
    print_warning "You have $VPC_COUNT VPCs (quota is usually 5)"
    print_warning "You may need to delete unused VPCs or request quota increase"
    ((WARNINGS++))
else
    print_success "VPC quota OK ($VPC_COUNT/5 used)"
fi

# Check EIP quota
EIP_COUNT=$(aws ec2 describe-addresses --query 'Addresses | length(@)' --output text 2>/dev/null || echo "0")
if [ "$EIP_COUNT" -ge 5 ]; then
    print_warning "You have $EIP_COUNT Elastic IPs (quota is usually 5)"
    ((WARNINGS++))
else
    print_success "Elastic IP quota OK ($EIP_COUNT/5 used)"
fi

###############################################################################
# Check 6: Parameters File
###############################################################################

echo -e "${BLUE}[6/8]${NC} Checking parameters.conf..."

if [ -f "parameters.conf" ]; then
    source parameters.conf
    
    if [ -z "$DOMAIN_NAME" ]; then
        print_error "DOMAIN_NAME not set in parameters.conf"
        ((ERRORS++))
    else
        print_success "DOMAIN_NAME: $DOMAIN_NAME"
    fi
    
    if [ -z "$DB_PASSWORD" ]; then
        print_error "DB_PASSWORD not set in parameters.conf"
        ((ERRORS++))
    elif [ ${#DB_PASSWORD} -lt 8 ]; then
        print_error "DB_PASSWORD is too short (minimum 8 characters)"
        ((ERRORS++))
    else
        print_success "DB_PASSWORD is set and meets length requirement"
    fi
    
    if [ -z "$ADMIN_EMAIL" ]; then
        print_error "ADMIN_EMAIL not set in parameters.conf"
        ((ERRORS++))
    else
        print_success "ADMIN_EMAIL: $ADMIN_EMAIL"
    fi
else
    print_error "parameters.conf not found"
    print_error "Please create parameters.conf from the template"
    ((ERRORS++))
fi

###############################################################################
# Check 7: Required Tools
###############################################################################

echo -e "${BLUE}[7/8]${NC} Checking required tools..."

if command -v git &> /dev/null; then
    print_success "git installed"
else
    print_error "git not installed"
    ((ERRORS++))
fi

if command -v openssl &> /dev/null; then
    print_success "openssl installed"
else
    print_error "openssl not installed"
    ((ERRORS++))
fi

###############################################################################
# Check 8: Estimated Costs
###############################################################################

echo -e "${BLUE}[8/8]${NC} Cost Estimate..."

print_warning "Estimated monthly AWS costs: \$300-500"
echo "  Main costs:"
echo "  - EKS cluster control plane: ~\$73/month"
echo "  - EC2 nodes (3x t3.large): ~\$150/month"
echo "  - RDS PostgreSQL (db.t3.medium): ~\$60/month"
echo "  - ElastiCache Redis: Variable"
echo "  - Data transfer: Variable"

###############################################################################
# Summary
###############################################################################

print_header "Pre-Check Summary"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed!${NC}"
    echo ""
    echo "You are ready to install DIAL."
    echo "Run: bash install.sh"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}⚠ Checks completed with $WARNINGS warnings${NC}"
    echo ""
    echo "You can proceed with installation, but please review the warnings above."
    echo "Run: bash install.sh"
else
    echo -e "${RED}✗ Checks failed with $ERRORS errors and $WARNINGS warnings${NC}"
    echo ""
    echo "Please fix the errors above before installing DIAL."
    exit 1
fi

echo ""
echo "Next steps:"
echo "1. Review your parameters.conf file"
echo "2. Run: bash install.sh"
echo "3. Wait ~30 minutes for installation to complete"
echo ""
