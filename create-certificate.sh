#!/bin/bash

set -e

REGION="${AWS_REGION:-us-east-1}"

echo "=========================================="
echo "DIAL Certificate Setup"
echo "=========================================="
echo ""

# Load domain from parameters.conf
if [ ! -f "parameters.conf" ]; then
    echo "❌ Error: parameters.conf not found!"
    echo "Please run this script from the dial-aws-installation directory."
    exit 1
fi

DOMAIN_NAME=$(grep "^DOMAIN_NAME=" parameters.conf | cut -d'=' -f2- | tr -d '"')

if [ -z "$DOMAIN_NAME" ]; then
    echo "❌ Error: DOMAIN_NAME not set in parameters.conf"
    exit 1
fi

echo "📋 Configuration:"
echo "   Domain: $DOMAIN_NAME"
echo "   Region: $REGION"
echo ""

# Check if certificate already exists
EXISTING_CERT=$(grep "^ACM_CERTIFICATE_ARN=" parameters.conf | cut -d'=' -f2- | tr -d '"')

if [ -n "$EXISTING_CERT" ] && [ "$EXISTING_CERT" != "" ]; then
    echo "⚠️  Certificate ARN already configured:"
    echo "   $EXISTING_CERT"
    echo ""
    read -p "Do you want to create a new certificate anyway? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Skipping certificate creation."
        exit 0
    fi
fi

echo "🔐 Requesting ACM certificate..."
echo ""

# Request certificate
CERT_ARN=$(aws acm request-certificate \
    --domain-name "$DOMAIN_NAME" \
    --subject-alternative-names "*.$DOMAIN_NAME" \
    --validation-method DNS \
    --region "$REGION" \
    --query 'CertificateArn' \
    --output text)

if [ -z "$CERT_ARN" ]; then
    echo "❌ Failed to request certificate"
    exit 1
fi

echo "✅ Certificate requested successfully!"
echo "   ARN: $CERT_ARN"
echo ""

# Wait a moment for AWS to prepare validation records
echo "⏳ Waiting for validation records to be ready..."
sleep 5

# Get validation records
echo ""
echo "📝 DNS Validation Records:"
echo "=========================================="

aws acm describe-certificate \
    --certificate-arn "$CERT_ARN" \
    --region "$REGION" \
    --query 'Certificate.DomainValidationOptions[*].[DomainName,ResourceRecord.Name,ResourceRecord.Type,ResourceRecord.Value]' \
    --output table

echo ""
echo "=========================================="
echo ""

# Extract validation info for instructions
VALIDATION_INFO=$(aws acm describe-certificate \
    --certificate-arn "$CERT_ARN" \
    --region "$REGION" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord' \
    --output json)

RECORD_NAME=$(echo "$VALIDATION_INFO" | jq -r '.Name')
RECORD_VALUE=$(echo "$VALIDATION_INFO" | jq -r '.Value')
RECORD_TYPE=$(echo "$VALIDATION_INFO" | jq -r '.Type')

# Create detailed instructions
cat > certificate-dns-instructions.txt << EOF
========================================
ACM Certificate DNS Validation
========================================

Certificate ARN: $CERT_ARN
Domain: $DOMAIN_NAME (including *.$DOMAIN_NAME)
Status: Pending Validation

DNS RECORD TO ADD:
------------------
Type:  $RECORD_TYPE
Name:  $RECORD_NAME
Value: $RECORD_VALUE

INSTRUCTIONS:
-------------
1. Log in to your DNS provider (Route53, GoDaddy, Cloudflare, etc.)

2. Add the following DNS record:
   - Record Type: $RECORD_TYPE
   - Record Name: $RECORD_NAME
   - Record Value: $RECORD_VALUE
   - TTL: 300 (or default)

3. Wait for DNS propagation (5-30 minutes)

4. AWS will automatically validate and issue the certificate

5. Check certificate status:
   aws acm describe-certificate \\
     --certificate-arn $CERT_ARN \\
     --region $REGION \\
     --query 'Certificate.Status' \\
     --output text

   Status should change from "PENDING_VALIDATION" to "ISSUED"

ROUTE53 QUICK COMMAND (if using Route53):
------------------------------------------
# Get Hosted Zone ID
ZONE_ID=\$(aws route53 list-hosted-zones-by-name \\
  --query "HostedZones[?Name=='$DOMAIN_NAME.'].Id" \\
  --output text | cut -d'/' -f3)

# Create DNS record
aws route53 change-resource-record-sets \\
  --hosted-zone-id \$ZONE_ID \\
  --change-batch '{
    "Changes": [{
      "Action": "CREATE",
      "ResourceRecordSet": {
        "Name": "$RECORD_NAME",
        "Type": "$RECORD_TYPE",
        "TTL": 300,
        "ResourceRecords": [{"Value": "$RECORD_VALUE"}]
      }
    }]
  }'

========================================
EOF

echo "📄 DNS instructions saved to: certificate-dns-instructions.txt"
echo ""

# Update parameters.conf
echo "📝 Updating parameters.conf with certificate ARN..."

# Check if ACM_CERTIFICATE_ARN line exists
if grep -q "^ACM_CERTIFICATE_ARN=" parameters.conf; then
    # Replace existing line
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s|^ACM_CERTIFICATE_ARN=.*|ACM_CERTIFICATE_ARN=\"$CERT_ARN\"|" parameters.conf
    else
        # Linux
        sed -i "s|^ACM_CERTIFICATE_ARN=.*|ACM_CERTIFICATE_ARN=\"$CERT_ARN\"|" parameters.conf
    fi
else
    # Add new line
    echo "" >> parameters.conf
    echo "# ACM Certificate (auto-configured)" >> parameters.conf
    echo "ACM_CERTIFICATE_ARN=\"$CERT_ARN\"" >> parameters.conf
fi

echo "✅ Certificate ARN saved to parameters.conf"
echo ""

# Summary
echo "=========================================="
echo "✅ Certificate Setup Complete!"
echo "=========================================="
echo ""
echo "📋 Summary:"
echo "   Certificate ARN: $CERT_ARN"
echo "   Status: PENDING_VALIDATION"
echo "   Domain: $DOMAIN_NAME, *.$DOMAIN_NAME"
echo ""
echo "⚠️  NEXT STEPS:"
echo "   1. Add the DNS record shown above to your domain"
echo "   2. Wait 5-30 minutes for validation"
echo "   3. Verify certificate status:"
echo "      aws acm describe-certificate \\"
echo "        --certificate-arn $CERT_ARN \\"
echo "        --region $REGION \\"
echo "        --query 'Certificate.Status'"
echo ""
echo "   4. Once status is 'ISSUED', proceed with deployment:"
echo "      bash deploy.sh"
echo ""
echo "📄 Detailed instructions: certificate-dns-instructions.txt"
echo ""

# Create verification script
cat > verify-certificate.sh << 'VERIFYEOF'
#!/bin/bash

REGION="${AWS_REGION:-us-east-1}"

# Get ARN from parameters.conf
CERT_ARN=$(grep "^ACM_CERTIFICATE_ARN=" parameters.conf | cut -d'=' -f2- | tr -d '"')

if [ -z "$CERT_ARN" ]; then
    echo "❌ No certificate ARN found in parameters.conf"
    exit 1
fi

echo "Checking certificate status..."
STATUS=$(aws acm describe-certificate \
    --certificate-arn "$CERT_ARN" \
    --region "$REGION" \
    --query 'Certificate.Status' \
    --output text)

echo ""
echo "Certificate ARN: $CERT_ARN"
echo "Status: $STATUS"
echo ""

if [ "$STATUS" = "ISSUED" ]; then
    echo "✅ Certificate is ISSUED and ready to use!"
    echo ""
    echo "You can now proceed with deployment:"
    echo "  bash deploy.sh"
elif [ "$STATUS" = "PENDING_VALIDATION" ]; then
    echo "⏳ Certificate is pending DNS validation"
    echo ""
    echo "Please ensure you have added the DNS validation record."
    echo "See: certificate-dns-instructions.txt"
    echo ""
    echo "Run this script again to check status."
else
    echo "⚠️  Certificate status: $STATUS"
fi
VERIFYEOF

chmod +x verify-certificate.sh

echo "💡 Created verify-certificate.sh - run this to check certificate status"
echo ""
