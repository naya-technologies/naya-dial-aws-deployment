#!/bin/bash

# DIAL AWS Deployment - Monitor Script
# Tracks CloudFormation stack progress and shows detailed status

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Load configuration
if [ ! -f "${SCRIPT_DIR}/parameters.conf" ]; then
    echo -e "${RED}✗${NC} parameters.conf not found!"
    exit 1
fi

source "${SCRIPT_DIR}/parameters.conf"

# Helper functions
get_stack_status() {
    aws cloudformation describe-stacks \
        --stack-name "$1" \
        --region ${AWS_REGION} \
        --query 'Stacks[0].StackStatus' \
        --output text 2>/dev/null || echo "NOT_FOUND"
}

get_nested_stacks() {
    aws cloudformation list-stack-resources \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --query 'StackResourceSummaries[?ResourceType==`AWS::CloudFormation::Stack`].[LogicalResourceId,PhysicalResourceId,ResourceStatus]' \
        --output text 2>/dev/null
}

get_recent_events() {
    aws cloudformation describe-stack-events \
        --stack-name ${STACK_NAME} \
        --region ${AWS_REGION} \
        --max-items 20 \
        --query 'StackEvents[].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
        --output text 2>/dev/null | head -20
}

# Main monitoring function
monitor_deployment() {
    local start_time=$(date +%s)
    local check_count=0
    local last_status=""
    
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}   DIAL Deployment Monitor${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
    echo -e "Stack: ${CYAN}${STACK_NAME}${NC}"
    echo -e "Region: ${CYAN}${AWS_REGION}${NC}"
    echo -e "Started: ${CYAN}$(date '+%Y-%m-%d %H:%M:%S')${NC}"
    echo ""
    
    while true; do
        check_count=$((check_count + 1))
        clear
        
        echo -e "${BLUE}========================================${NC}"
        echo -e "${BLUE}   DIAL Deployment Monitor${NC}"
        echo -e "${BLUE}========================================${NC}"
        echo ""
        echo -e "Stack: ${CYAN}${STACK_NAME}${NC}"
        echo -e "Region: ${CYAN}${AWS_REGION}${NC}"
        echo -e "Check #${check_count} | Elapsed: $(($(date +%s) - start_time))s"
        echo ""
        
        # Get main stack status
        MAIN_STATUS=$(get_stack_status ${STACK_NAME})
        
        if [ "$MAIN_STATUS" == "NOT_FOUND" ]; then
            echo -e "${YELLOW}⚠${NC} Stack not found. Have you run deploy.sh?"
            echo ""
            echo "To start deployment: ${GREEN}bash deploy.sh${NC}"
            exit 1
        fi
        
        # Display main stack status
        case "$MAIN_STATUS" in
            CREATE_IN_PROGRESS)
                echo -e "Main Stack: ${YELLOW}⏳ CREATING${NC}"
                ;;
            CREATE_COMPLETE)
                echo -e "Main Stack: ${GREEN}✓ COMPLETE${NC}"
                ;;
            CREATE_FAILED|ROLLBACK_IN_PROGRESS|ROLLBACK_COMPLETE|ROLLBACK_FAILED)
                echo -e "Main Stack: ${RED}✗ FAILED (${MAIN_STATUS})${NC}"
                ;;
            UPDATE_IN_PROGRESS)
                echo -e "Main Stack: ${YELLOW}⏳ UPDATING${NC}"
                ;;
            UPDATE_COMPLETE)
                echo -e "Main Stack: ${GREEN}✓ UPDATED${NC}"
                ;;
            DELETE_IN_PROGRESS)
                echo -e "Main Stack: ${YELLOW}⏳ DELETING${NC}"
                ;;
            *)
                echo -e "Main Stack: ${CYAN}${MAIN_STATUS}${NC}"
                ;;
        esac
        echo ""
        
        # Display nested stacks
        echo -e "${CYAN}Nested Stacks:${NC}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        
        NESTED=$(get_nested_stacks)
        if [ -n "$NESTED" ]; then
            while IFS=$'\t' read -r logical physical status; do
                case "$status" in
                    CREATE_IN_PROGRESS)
                        echo -e "  ${YELLOW}⏳${NC} ${logical}"
                        ;;
                    CREATE_COMPLETE)
                        echo -e "  ${GREEN}✓${NC} ${logical}"
                        ;;
                    CREATE_FAILED|ROLLBACK_*|DELETE_FAILED)
                        echo -e "  ${RED}✗${NC} ${logical} (${status})"
                        ;;
                    *)
                        echo -e "  ${CYAN}◦${NC} ${logical} (${status})"
                        ;;
                esac
            done <<< "$NESTED"
        else
            echo "  No nested stacks yet..."
        fi
        echo ""
        
        # Display recent events
        echo -e "${CYAN}Recent Events (last 10):${NC}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        
        EVENTS=$(get_recent_events)
        if [ -n "$EVENTS" ]; then
            echo "$EVENTS" | head -10 | while IFS=$'\t' read -r timestamp resource status reason; do
                # Format timestamp
                time_only=$(echo "$timestamp" | cut -d'T' -f2 | cut -d'.' -f1)
                
                # Color code status
                case "$status" in
                    *COMPLETE)
                        echo -e "${time_only} ${GREEN}✓${NC} ${resource}"
                        ;;
                    *IN_PROGRESS)
                        echo -e "${time_only} ${YELLOW}⏳${NC} ${resource}"
                        ;;
                    *FAILED)
                        echo -e "${time_only} ${RED}✗${NC} ${resource}"
                        if [ -n "$reason" ] && [ "$reason" != "None" ]; then
                            echo -e "   ${RED}↳${NC} ${reason}" | cut -c1-100
                        fi
                        ;;
                    *)
                        echo -e "${time_only} ${CYAN}◦${NC} ${resource}"
                        ;;
                esac
            done
        fi
        echo ""
        
        # Check if deployment is done
        case "$MAIN_STATUS" in
            CREATE_COMPLETE)
                echo -e "${GREEN}========================================${NC}"
                echo -e "${GREEN}   ✓ DEPLOYMENT SUCCESSFUL!${NC}"
                echo -e "${GREEN}========================================${NC}"
                echo ""
                echo "Next steps:"
                echo "1. Configure DNS for your domain"
                echo "2. Deploy DIAL application: bash deploy-dial-app.sh"
                echo ""
                exit 0
                ;;
            CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_FAILED)
                echo -e "${RED}========================================${NC}"
                echo -e "${RED}   ✗ DEPLOYMENT FAILED${NC}"
                echo -e "${RED}========================================${NC}"
                echo ""
                echo "Troubleshooting:"
                echo "1. Check events above for error details"
                echo "2. View full logs: aws cloudformation describe-stack-events --stack-name ${STACK_NAME}"
                echo "3. Clean up: bash cleanup.sh"
                echo "4. Try again: bash deploy.sh"
                echo ""
                exit 1
                ;;
            DELETE_COMPLETE)
                echo -e "${CYAN}Stack has been deleted${NC}"
                exit 0
                ;;
        esac
        
        # Wait before next check
        echo -e "${YELLOW}Checking again in 30 seconds...${NC} (Press Ctrl+C to exit)"
        sleep 30
    done
}

# Start monitoring
monitor_deployment
