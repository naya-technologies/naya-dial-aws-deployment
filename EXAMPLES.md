# Configuration Examples

This file contains example configurations for different scenarios.

## Example 1: Basic Production Deployment

```bash
# parameters.conf
DOMAIN_NAME="mycompany.com"
DB_PASSWORD="MySecureP@ssw0rd2024!"
ADMIN_EMAIL="admin@mycompany.com"

AWS_REGION="il-central-1"  # Tel Aviv region
STACK_NAME="dial-production"
EKS_CLUSTER_NAME="dial-cluster"
CERTIFICATE_OPTION="auto"
ALLOW_SELF_REGISTRATION="no"

# Cognito pool names (optional, uses defaults)
COGNITO_USER_POOL_NAME="dial-users"
COGNITO_ADMIN_USER_POOL_NAME="dial-admins"

# Production instances (m5.large)
EKS_NODE_INSTANCE_TYPE="m5.large"

# Use defaults for everything else
```

**Result**: DIAL accessible at:
- https://chat.mycompany.com
- https://admin.mycompany.com

**Monthly Cost**: ~$500

---

## Example 2: Development/Testing Environment

```bash
# parameters.conf
DOMAIN_NAME="dev.mycompany.com"
DB_PASSWORD="DevP@ssw0rd2024!"
ADMIN_EMAIL="devteam@mycompany.com"

AWS_REGION="us-east-1"
STACK_NAME="dial-dev"
EKS_CLUSTER_NAME="dial-dev-cluster"
CERTIFICATE_OPTION="auto"
ALLOW_SELF_REGISTRATION="yes"  # Allow devs to self-register

# Smaller instances for dev
EKS_NODE_INSTANCE_TYPE="t3.medium"
EKS_NODE_MIN_SIZE="2"
EKS_NODE_DESIRED_SIZE="2"
RDS_INSTANCE_TYPE="db.t3.small"
RDS_STORAGE_GB="50"
```

**Monthly Cost**: ~$200 (smaller instances)

---

## Example 3: Using Existing SSL Certificate

```bash
# parameters.conf
DOMAIN_NAME="chat.enterprise.com"
DB_PASSWORD="EnterpriseP@ss2024!"
ADMIN_EMAIL="it@enterprise.com"

AWS_REGION="eu-west-1"
STACK_NAME="dial-prod-eu"
EKS_CLUSTER_NAME="dial-prod-eu"

# Use existing certificate
CERTIFICATE_OPTION="existing"
EXISTING_CERTIFICATE_ARN="arn:aws:acm:eu-west-1:123456789012:certificate/abcd1234-5678-90ab-cdef-1234567890ab"

ALLOW_SELF_REGISTRATION="no"
```

**Note**: Certificate must already exist in ACM in the same region.

---

## Example 4: High-Availability Production

```bash
# parameters.conf
DOMAIN_NAME="ai.bigcompany.com"
DB_PASSWORD="V3rySecur3P@ssw0rd!"
ADMIN_EMAIL="cloudops@bigcompany.com"

AWS_REGION="us-west-2"
STACK_NAME="dial-production-ha"
EKS_CLUSTER_NAME="dial-prod-ha"

# Larger instances for production load
EKS_NODE_INSTANCE_TYPE="t3.xlarge"
EKS_NODE_MIN_SIZE="3"
EKS_NODE_MAX_SIZE="20"
EKS_NODE_DESIRED_SIZE="5"

# Larger database
RDS_INSTANCE_TYPE="db.t3.large"
RDS_STORAGE_GB="500"

CERTIFICATE_OPTION="auto"
ALLOW_SELF_REGISTRATION="no"
```

**Monthly Cost**: ~$800-1000 (larger instances, more nodes)

---

## Example 5: Multi-Environment Setup

For multiple environments (dev, staging, prod), use different stack names:

### Development
```bash
STACK_NAME="dial-dev"
DOMAIN_NAME="dev-chat.mycompany.com"
EKS_CLUSTER_NAME="dial-dev"
EKS_NODE_DESIRED_SIZE="2"
```

### Staging
```bash
STACK_NAME="dial-staging"
DOMAIN_NAME="staging-chat.mycompany.com"
EKS_CLUSTER_NAME="dial-staging"
EKS_NODE_DESIRED_SIZE="3"
```

### Production
```bash
STACK_NAME="dial-production"
DOMAIN_NAME="chat.mycompany.com"
EKS_CLUSTER_NAME="dial-prod"
EKS_NODE_DESIRED_SIZE="5"
```

**Note**: Each environment is completely isolated with its own VPC, cluster, and databases.

---

## Example 6: Custom Network Configuration

```bash
# parameters.conf
DOMAIN_NAME="internal.corp.com"
DB_PASSWORD="C0rpP@ssw0rd2024!"
ADMIN_EMAIL="it@corp.com"

# Custom VPC CIDR (avoid conflicts with existing networks)
VPC_CIDR="172.16.0.0/16"
PUBLIC_SUBNET_1_CIDR="172.16.1.0/24"
PUBLIC_SUBNET_2_CIDR="172.16.2.0/24"
PRIVATE_SUBNET_1_CIDR="172.16.10.0/24"
PRIVATE_SUBNET_2_CIDR="172.16.11.0/24"

AWS_REGION="us-east-2"
STACK_NAME="dial-corp"
```

**Use Case**: When deploying in an environment with existing 10.x.x.x networks.

---

## Example 7: Cost-Optimized Setup

```bash
# parameters.conf
DOMAIN_NAME="ai.startup.com"
DB_PASSWORD="Startup2024P@ss!"
ADMIN_EMAIL="founder@startup.com"

AWS_REGION="us-east-2"  # Often cheaper than us-east-1

# Minimal but functional
EKS_NODE_INSTANCE_TYPE="t3.medium"
EKS_NODE_MIN_SIZE="2"
EKS_NODE_MAX_SIZE="5"
EKS_NODE_DESIRED_SIZE="2"

RDS_INSTANCE_TYPE="db.t3.micro"  # Smallest RDS
RDS_STORAGE_GB="50"

CERTIFICATE_OPTION="auto"
ALLOW_SELF_REGISTRATION="no"
```

**Monthly Cost**: ~$150-200 (minimal viable setup)

**Warning**: t3.micro RDS not recommended for production loads.

---

## Example 8: Maximum Security

```bash
# parameters.conf
DOMAIN_NAME="secure.healthcare.com"
DB_PASSWORD="C0mpl3x!Secur3#P@ssw0rd2024$"
ADMIN_EMAIL="security@healthcare.com"

AWS_REGION="us-east-1"
STACK_NAME="dial-hipaa"
EKS_CLUSTER_NAME="dial-hipaa-cluster"

# Disable self-registration
ALLOW_SELF_REGISTRATION="no"

# Production-grade instances
EKS_NODE_INSTANCE_TYPE="t3.large"
RDS_INSTANCE_TYPE="db.t3.medium"

CERTIFICATE_OPTION="auto"
```

**Additional Steps for Healthcare/HIPAA**:
1. Enable AWS CloudTrail
2. Enable AWS Config
3. Enable VPC Flow Logs
4. Sign BAA with AWS
5. Enable RDS encryption (done by default)
6. Enable MFA in Cognito

---

## Example 9: European Region

```bash
# parameters.conf
DOMAIN_NAME="chat.company.eu"
DB_PASSWORD="Eur0pe@nP@ss2024!"
ADMIN_EMAIL="admin@company.eu"

# EU region for GDPR compliance
AWS_REGION="eu-central-1"

STACK_NAME="dial-eu-prod"
EKS_CLUSTER_NAME="dial-eu-cluster"
CERTIFICATE_OPTION="auto"
ALLOW_SELF_REGISTRATION="no"

# Standard production config
EKS_NODE_INSTANCE_TYPE="t3.large"
EKS_NODE_DESIRED_SIZE="3"
```

**GDPR Compliance Notes**:
- All data stays in EU region
- Enable CloudTrail for audit logs
- Configure Cognito for EU data residency
- Review AWS GDPR documentation

---

## Example 10: Rapid Prototype/Demo

```bash
# parameters.conf
DOMAIN_NAME="demo.startup.io"
DB_PASSWORD="DemoP@ss123!"
ADMIN_EMAIL="demo@startup.io"

AWS_REGION="us-east-2"
STACK_NAME="dial-demo"
EKS_CLUSTER_NAME="dial-demo"

# Minimal config for quick demo
EKS_NODE_INSTANCE_TYPE="t3.medium"
EKS_NODE_MIN_SIZE="2"
EKS_NODE_DESIRED_SIZE="2"
RDS_INSTANCE_TYPE="db.t3.small"

# Allow self-registration for demo
ALLOW_SELF_REGISTRATION="yes"

CERTIFICATE_OPTION="auto"
```

**Use Case**: Quick demo setup, not for production data.

---

## Password Requirements

Your `DB_PASSWORD` must meet these criteria:
- Minimum 8 characters
- At least one uppercase letter (A-Z)
- At least one lowercase letter (a-z)
- At least one number (0-9)
- At least one special character (!@#$%^&*)

**Good Examples**:
- `MySecure2024!`
- `P@ssw0rd#2024`
- `Compl3x!Secure#`

**Bad Examples** (will fail):
- `password` (no uppercase, no numbers, no special chars)
- `Password` (no numbers, no special chars)
- `Pass123` (no special chars, too short)

---

## Domain Name Guidelines

### Valid Domain Names
- `mycompany.com` ✅
- `chat.enterprise.io` ✅
- `ai.startup.co` ✅
- `internal.corp.local` ✅ (if you control DNS)

### Subdomains Created
For `DOMAIN_NAME="example.com"`, these subdomains are created:
- `chat.example.com` - Chat UI
- `admin.example.com` - Admin UI
- `core.example.com` - Core API
- `themes.example.com` - Themes service

**Important**: You must be able to create DNS records for these subdomains.

---

## Region Selection

### Popular Regions

| Region | Code | Benefits |
|--------|------|----------|
| N. Virginia | us-east-1 | Largest service selection, sometimes cheaper |
| Ohio | us-east-2 | Often better availability, good pricing |
| Oregon | us-west-2 | West coast, good for Asia-Pacific |
| Ireland | eu-west-1 | EU data residency |
| Frankfurt | eu-central-1 | EU data residency, central Europe |
| London | eu-west-2 | UK data residency |
| Sydney | ap-southeast-2 | Australia/NZ |

**Tip**: Choose region closest to your users for best performance.

---

## Troubleshooting Common Configuration Issues

### Issue: "Domain name already in use"
**Solution**: Each domain can only be used once per region. Use different subdomains or regions.

### Issue: "VPC CIDR overlaps"
**Solution**: If you have existing VPCs, change `VPC_CIDR` to avoid overlap.

### Issue: "Certificate validation timeout"
**Solution**: Make sure you can create DNS records for domain validation when using `CERTIFICATE_OPTION="auto"`.

### Issue: "Insufficient permissions"
**Solution**: Use an IAM user/role with admin permissions.

---

## Next Steps

1. Copy one of these examples to your `parameters.conf`
2. Customize the values for your needs
3. Run `bash check-prerequisites.sh` to validate
4. Run `bash install.sh` to deploy
