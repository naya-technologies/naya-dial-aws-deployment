# DIAL for AWS - Marketplace Listing

## Product Description

**DIAL (Distributed Intelligence Application Layer)** is an enterprise-ready AI assistant platform that runs entirely in your AWS account. Get a production-ready deployment of DIAL with secure authentication, scalable infrastructure, and enterprise features in just 30 minutes.

### What You Get

- 🚀 **Complete Infrastructure**: EKS, RDS, Redis, S3, VPC, and all networking configured
- 🔐 **Secure by Default**: AWS Cognito authentication, encrypted storage, private subnets
- 📈 **Production-Ready**: Auto-scaling, high availability, automated backups
- 💬 **AI Chat Interface**: Modern web interface for end users
- ⚙️ **Admin Dashboard**: User and system management portal
- 🎨 **Customizable**: Theme support and configuration options

### Key Features

- **One-Click Deployment**: Automated CloudFormation deployment
- **Secure Architecture**: Private subnets, security groups, IAM roles with least privilege
- **Scalable Infrastructure**: Kubernetes-based with auto-scaling nodes
- **Enterprise Authentication**: AWS Cognito with OIDC support
- **Data Residency**: All data stays in your AWS account
- **Cost-Effective**: Pay only for AWS resources you use (~$300-500/month)

## How It Works

1. **Download** installation package from GitHub
2. **Configure** basic parameters (domain, password, email)
3. **Run** automated installation script in AWS CloudShell
4. **Wait** 30 minutes while infrastructure deploys
5. **Access** your DIAL instance at your custom domain

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Internet                             │
└────────────────────────┬────────────────────────────────────┘
                         │
                    ┌────▼─────┐
                    │   Route  │
                    │   53/DNS │
                    └────┬─────┘
                         │
                ┌────────▼─────────┐
                │  Application     │
                │  Load Balancer   │
                │  (ALB)           │
                └────────┬─────────┘
                         │
            ┌────────────┴────────────┐
            │        VPC              │
            │  ┌──────────────────┐   │
            │  │   Public Subnet  │   │
            │  │   (NAT, ALB)     │   │
            │  └────────┬─────────┘   │
            │           │             │
            │  ┌────────▼─────────┐   │
            │  │  Private Subnet  │   │
            │  │                  │   │
            │  │  ┌───────────┐   │   │
            │  │  │    EKS    │   │   │
            │  │  │  Cluster  │   │   │
            │  │  │           │   │   │
            │  │  │  ┌─────┐  │   │   │
            │  │  │  │DIAL │  │   │   │
            │  │  │  │Core │  │   │   │
            │  │  │  └─────┘  │   │   │
            │  │  │  ┌─────┐  │   │   │
            │  │  │  │DIAL │  │   │   │
            │  │  │  │Chat │  │   │   │
            │  │  │  └─────┘  │   │   │
            │  │  └───────────┘   │   │
            │  │                  │   │
            │  │  ┌─────────┐     │   │
            │  │  │   RDS   │     │   │
            │  │  │PostgreSQL│    │   │
            │  │  └─────────┘     │   │
            │  │                  │   │
            │  │  ┌─────────┐     │   │
            │  │  │  Redis  │     │   │
            │  │  │ElastiCache│   │   │
            │  │  └─────────┘     │   │
            │  └──────────────────┘   │
            └─────────────────────────┘
                         │
                    ┌────▼─────┐
                    │    S3    │
                    │  Storage │
                    └──────────┘
```

## AWS Resources Created

### Compute & Networking
- 1x VPC with public and private subnets (2 AZs)
- 1x Internet Gateway
- 2x NAT Gateways (optional, can be reduced to 1)
- 1x Application Load Balancer
- 1x EKS Cluster (Kubernetes 1.31)
- 3x EC2 Instances (t3.large) - auto-scaling 2-10

### Databases & Storage
- 1x RDS PostgreSQL (db.t3.medium, 100GB)
- 1x ElastiCache Redis (Serverless)
- 1x S3 Bucket (versioning enabled)

### Security & Access
- 1x Cognito User Pool
- 5x Security Groups
- 3x IAM Roles (IRSA for pods)
- 1x ACM Certificate (if auto-created)
- 10x VPC Endpoints (private AWS service access)

### Monitoring & Logs
- CloudWatch Logs for EKS
- RDS automated backups (7 days)
- CloudFormation stack events

## Pricing Estimate

| Service | Configuration | Monthly Cost |
|---------|---------------|--------------|
| EKS Control Plane | 1 cluster | $73 |
| EC2 Nodes | 3x t3.large | ~$150 |
| RDS PostgreSQL | db.t3.medium | ~$60 |
| ElastiCache | Serverless | ~$20-50 |
| ALB | 1 load balancer | ~$20 |
| VPC Endpoints | 10 endpoints | ~$70 |
| S3 Storage | Variable | ~$5-20 |
| Data Transfer | Variable | ~$10-50 |
| **Total** | | **~$300-500/month** |

*Prices are estimates based on us-east-2 region and typical usage. Actual costs may vary.*

## Prerequisites

### Required
- AWS account with admin permissions
- Valid domain name (for SSL/DNS)
- Email address (for admin account)

### Recommended
- Basic familiarity with AWS Console
- Domain hosted on Route53 (or ability to create DNS records)

### Technical Requirements
- No special software needed - runs entirely in AWS CloudShell
- Installation uses: AWS CLI, kubectl, helm (all auto-installed if missing)

## Installation Time

- **Total**: 30-40 minutes
- **Active time**: 5 minutes (rest is automated)
- CloudFormation deployment: ~25 minutes
- DNS propagation: 5-60 minutes (varies by provider)

## Support & Documentation

### Included
- Comprehensive README with step-by-step instructions
- Quick start guide
- Troubleshooting documentation
- Pre-installation checker script
- CloudFormation templates
- Automated installation scripts

### Community Support
- GitHub Issues
- Documentation wiki
- Example configurations

### Professional Support
- Available through DIAL support channels
- AWS Premium Support (separate)

## Security & Compliance

### Security Features
- ✅ All data encrypted at rest (RDS, S3, EBS)
- ✅ Data encrypted in transit (TLS/HTTPS)
- ✅ Private subnets for compute and data
- ✅ IAM roles with least privilege (IRSA)
- ✅ VPC endpoints (no internet for AWS API calls)
- ✅ Security groups with strict rules
- ✅ CloudTrail logging (if enabled in account)
- ✅ Automated backups enabled

### Compliance
- Infrastructure follows AWS Well-Architected Framework
- GDPR-ready (data stays in your account and region)
- HIPAA-eligible services used (when configured properly)
- Supports AWS Organizations and Service Control Policies

### Data Privacy
- No data leaves your AWS account
- No telemetry or phone-home
- Full control over all resources
- Can be deployed in isolated VPC

## Customization Options

### Easy Customization (via parameters.conf)
- AWS region selection
- Domain name
- Instance sizes
- Auto-scaling limits
- Database size
- Network CIDR blocks

### Advanced Customization (via CloudFormation)
- Custom VPC configuration
- Additional security groups
- Custom IAM policies
- Backup retention periods
- Multi-region deployment

## Upgrade & Maintenance

### Updates
- CloudFormation templates on GitHub (always latest)
- Stack updates preserve data
- Rolling updates for EKS nodes
- Zero-downtime deployments

### Backup & Recovery
- RDS automated backups (7 days)
- RDS snapshots (manual)
- S3 versioning enabled
- CloudFormation stack exports

## Uninstallation

Simple cleanup process:
1. Delete Helm deployment
2. Delete CloudFormation stack
3. Clean up S3 buckets

All resources removed - no orphaned charges.

## What Makes This Different

### vs. Manual Setup
- ✅ Save 8-10 hours of configuration
- ✅ No AWS expertise required
- ✅ Best practices built-in
- ✅ Production-ready from day one

### vs. Other Solutions
- ✅ Run in YOUR AWS account (data sovereignty)
- ✅ Full control and customization
- ✅ No vendor lock-in
- ✅ Transparent pricing (AWS only)
- ✅ Open architecture

### vs. SaaS
- ✅ Lower long-term costs
- ✅ Complete data control
- ✅ Compliance-friendly
- ✅ Unlimited customization
- ✅ No per-user fees

## Use Cases

### Enterprise AI Assistant
Deploy a private AI assistant for your organization with:
- Secure authentication via Cognito
- Integration with AWS Bedrock
- Custom model deployment
- Team collaboration features

### Customer Support Platform
Build a customer-facing AI support system:
- Multi-tenant architecture ready
- Scalable infrastructure
- Secure data handling
- Easy integration with existing systems

### Development & Testing
Create development environments for:
- AI application testing
- LLM integration development
- Proof of concepts
- Training and demonstrations

## Getting Started

### Step 1: Prepare
- Have your AWS account ready
- Choose your domain name
- Decide on AWS region

### Step 2: Download
```bash
git clone https://github.com/YOUR-ORG/dial-aws-installation.git
cd dial-aws-installation
```

### Step 3: Configure
```bash
nano parameters.conf
# Fill in DOMAIN_NAME, DB_PASSWORD, ADMIN_EMAIL
```

### Step 4: Install
```bash
bash install.sh
# Wait ~30 minutes
```

### Step 5: Access
Visit `https://chat.yourdomain.com`

## Frequently Asked Questions

**Q: Do I need AWS expertise?**
A: No. The installation is automated. You just need an AWS account and basic ability to follow instructions.

**Q: How long does installation take?**
A: About 30-40 minutes total, with only 5 minutes of your active time.

**Q: Can I customize the installation?**
A: Yes. Edit CloudFormation templates for advanced customization, or use parameters.conf for basic changes.

**Q: What if something goes wrong?**
A: The installation script includes validation and error checking. If issues occur, CloudFormation automatically rolls back.

**Q: Can I use my existing VPC?**
A: The templates create a new VPC by default. For existing VPC, you'll need to modify the templates.

**Q: Is this production-ready?**
A: Yes. The architecture follows AWS best practices with HA, auto-scaling, backups, and security.

**Q: Can I scale up or down?**
A: Yes. Edit parameters and update the CloudFormation stack.

**Q: What about updates?**
A: Pull latest templates from GitHub and update your CloudFormation stack.

**Q: How do I delete everything?**
A: Run the uninstall commands to delete all resources. S3 buckets need manual deletion if not empty.

**Q: Is support included?**
A: Community support via GitHub. Professional support available separately.

## License

MIT License - Free to use and modify

## Links

- [GitHub Repository](https://github.com/YOUR-ORG/dial-aws-installation)
- [Documentation](https://github.com/YOUR-ORG/dial-aws-installation/wiki)
- [Issue Tracker](https://github.com/YOUR-ORG/dial-aws-installation/issues)
- [DIAL Official Site](https://your-dial-site.com)

## Tags

`ai`, `chatbot`, `aws`, `eks`, `kubernetes`, `cloudformation`, `infrastructure-as-code`, `llm`, `enterprise`, `self-hosted`
