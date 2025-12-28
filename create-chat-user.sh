#!/bin/bash

###############################################################################
# Create Chat User Script
# Creates users in the Chat User Pool (not Admin pool)
###############################################################################

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Load configuration
if [ ! -f "parameters.conf" ]; then
    echo -e "${RED}Error: parameters.conf not found${NC}"
    exit 1
fi

source parameters.conf

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Create DIAL Chat User${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Get Chat User Pool ID
echo "Getting Chat User Pool ID..."
CHAT_USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --region ${AWS_REGION} \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

if [ -z "$CHAT_USER_POOL_ID" ]; then
    echo -e "${RED}Error: Could not find Chat User Pool ID${NC}"
    echo "Make sure the stack is deployed and STACK_NAME is correct in parameters.conf"
    exit 1
fi

echo -e "${GREEN}✓ Chat User Pool ID: ${CHAT_USER_POOL_ID}${NC}"
echo ""

# Prompt for user details
read -p "Enter user email address: " USER_EMAIL
if [ -z "$USER_EMAIL" ]; then
    echo -e "${RED}Error: Email is required${NC}"
    exit 1
fi

read -p "Enter user full name: " USER_NAME
if [ -z "$USER_NAME" ]; then
    echo -e "${RED}Error: Name is required${NC}"
    exit 1
fi

# Optional: Send welcome email
read -p "Send welcome email with temporary password? (yes/no) [yes]: " SEND_EMAIL
SEND_EMAIL=${SEND_EMAIL:-yes}

if [ "$SEND_EMAIL" == "no" ]; then
    MESSAGE_ACTION="SUPPRESS"
else
    MESSAGE_ACTION="RESEND"
fi

echo ""
echo -e "${BLUE}Creating user...${NC}"
echo "  Email: $USER_EMAIL"
echo "  Name: $USER_NAME"
echo "  Pool: Chat Users (for https://chat.${DOMAIN_NAME})"
echo ""

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id ${CHAT_USER_POOL_ID} \
  --username ${USER_EMAIL} \
  --user-attributes \
    Name=email,Value=${USER_EMAIL} \
    Name=name,Value="${USER_NAME}" \
    Name=email_verified,Value=true \
  --message-action ${MESSAGE_ACTION} \
  --region ${AWS_REGION}

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ Chat user created successfully!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "User Details:"
    echo "  Email: ${USER_EMAIL}"
    echo "  Name: ${USER_NAME}"
    echo "  Pool: Chat Users"
    echo ""
    
    if [ "$SEND_EMAIL" == "yes" ]; then
        echo -e "${YELLOW}A temporary password has been sent to: ${USER_EMAIL}${NC}"
        echo "The user will be asked to change it on first login."
    else
        echo -e "${YELLOW}No email sent. Set password manually with:${NC}"
        echo "  aws cognito-idp admin-set-user-password \\"
        echo "    --user-pool-id ${CHAT_USER_POOL_ID} \\"
        echo "    --username ${USER_EMAIL} \\"
        echo "    --password 'NewPassword123!' \\"
        echo "    --permanent"
    fi
    
    echo ""
    echo "Access URL: https://chat.${DOMAIN_NAME}"
    echo ""
    echo -e "${BLUE}Note: This is a CHAT user, not an admin user.${NC}"
    echo "To create admin users, use: bash create-admin-user.sh"
else
    echo -e "${RED}Failed to create user${NC}"
    exit 1
fi
